#!/usr/bin/env bash
set -uo pipefail

# ---------------------------------------------------------------------------
# Watches for an inserted SD card, copies it off, hands it to Immich, and
# pings you when the card is safe to wipe. Idempotent: re-inserting the same
# card does nothing, and Immich dedupes by hash on top of that.
# ---------------------------------------------------------------------------

USB_MOUNT="${USB_MOUNT:-/mnt/usb}"
STAGING="${STAGING:-/staging}"
POLL_SECONDS="${POLL_SECONDS:-5}"
NTFY_URL="${NTFY_URL:-}"
STATE_DIR="${STATE_DIR:-$STAGING/.state}"

mkdir -p "$STAGING" "$STATE_DIR"

notify() { # notify "Title" "message body" [priority]
  [ -z "$NTFY_URL" ] && return 0
  curl -fsS -H "Title: ${1}" ${3:+-H "Priority: ${3}"} \
       -d "${2}" "$NTFY_URL" >/dev/null 2>&1 || true
}

# A card's "signature" is a hash of its media filenames + sizes. If the same
# untouched card is re-inserted, the signature matches and we skip it.
card_signature() {
  find "$1" -type f \( \
      -iname '*.arw' -o -iname '*.jpg'  -o -iname '*.jpeg' -o -iname '*.cr2' \
   -o -iname '*.cr3' -o -iname '*.nef'  -o -iname '*.dng'  -o -iname '*.raf' \
   -o -iname '*.mp4' -o -iname '*.mov' \) -printf '%p\t%s\n' 2>/dev/null \
    | sort | sha256sum | awk '{print $1}'
}

process_card() {
  local src="$1"
  local sig; sig="$(card_signature "$src")"
  [ -z "$sig" ] && return 0                    # no media on this mount, ignore
  [ -f "$STATE_DIR/$sig" ] && return 0         # already imported this exact card

  local count; count="$(find "$src" -type f | wc -l | tr -d ' ')"
  notify "Import started" "Copying ${count} files off the card..."

  # 1) Verified copy: card -> staging (--checksum compares content, not just time)
  local dest="$STAGING/incoming"
  mkdir -p "$dest"
  if ! rsync -a --checksum --no-perms --no-owner --no-group "$src"/ "$dest"/; then
    notify "Import FAILED" "Copy step errored. Card is NOT safe to wipe." "high"
    return 1
  fi

  # 2) Hand staging to Immich. Server-side hash dedupe makes this safe to re-run.
  if ! immich upload --recursive --concurrency 4 "$dest"; then
    notify "Import FAILED" "Immich upload errored. Card is NOT safe to wipe." "high"
    return 1
  fi

  # 3) Success. THIS ping is the one that authorises wiping the card.
  touch "$STATE_DIR/$sig"
  notify "Safe to wipe" "${count} files are in Immich. This card can be erased." "default"

  # 4) Clear staging (Immich owns the originals now).
  rm -rf "${dest:?}/"
  return 0
}

echo "watch-import: polling ${USB_MOUNT} every ${POLL_SECONDS}s"
while true; do
  if [ -d "$USB_MOUNT" ]; then
    # Synology mounts cards as usbshare, usbshare2-1, ... under the port parent.
    for mp in "$USB_MOUNT"/usbshare*; do
      [ -d "$mp" ] || continue
      process_card "$mp" || true
    done
  fi
  sleep "$POLL_SECONDS"
done
