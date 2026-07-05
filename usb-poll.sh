#!/bin/bash
# ---------------------------------------------------------------------------
# usb-poll.sh — host-side ingest for the photo-import pipeline.
#
# Runs on the Synology HOST via DSM Task Scheduler (user: root, every ~2 min).
# Detects any inserted camera card mounted under /volumeUSB* and does a
# CHECKSUM-VERIFIED copy of it into incoming/ for the (unprivileged) watcher
# container to upload + hash-verify.
#
# Why host-side: a container can't see hot-plugged USB on this NAS (Docker rejects
#   the rshared bind — host / isn't a shared mount).
# Why polling: Synology USB Copy's on-connect task binds to a card's UUID, so it
#   stops firing for any reformatted or different card. Polling keys off *content*
#   (a DCIM/ tree), so it handles any card.
#
# Idempotent: a card left inserted is copied once (content-signature marker); a
# reformatted or different card has a new signature and is copied. The copy is
# staged in a hidden dir and atomically renamed in, so the watcher only ever sees
# complete batches.
#
# DSM Task Scheduler command (user-defined script, user = root, every 2 min):
#   bash /volume1/docker/projects/photo-import/usb-poll.sh >> /volume1/docker/projects/photo-import/usb-poll.log 2>&1
# ---------------------------------------------------------------------------
set -uo pipefail

ROOT="${PHOTO_IMPORT_ROOT:-/volume1/docker/projects/photo-import}"
INCOMING="${INCOMING:-$ROOT/incoming}"
STATE="${STATE:-$ROOT/.usb-poll-state}"
LOCK="${LOCK:-$ROOT/.usb-poll.lock}"

log() { printf '%s usb-poll: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }

mkdir -p "$INCOMING" "$STATE"

# Single instance: if a previous (long) copy is still running, bail quietly.
exec 9>"$LOCK"
if ! flock -n 9; then
  exit 0
fi

# Content signature of a card: sorted (relative path, size) of every file, ignoring
# Synology @-dirs. Stable per card content, independent of the mount path.
card_signature() {
  find "$1" -type f -not -path '*/@*' -printf '%P\t%s\n' 2>/dev/null \
    | sort | sha256sum | awk '{print $1}'
}

copy_card() {
  local card="$1"                        # e.g. /volumeUSB1/usbshare1-1
  [ -d "$card" ] || return 0

  # Camera-card gate: must contain a DCIM/ tree.
  [ -n "$(find "$card" -maxdepth 2 -type d -iname DCIM -not -path '*/@*' 2>/dev/null | head -1)" ] || return 0

  local sig; sig="$(card_signature "$card")"
  [ -n "$sig" ] || return 0              # empty card
  [ -f "$STATE/$sig" ] && return 0       # already copied this exact card content

  local ts tmp dest count
  ts="$(date +%Y%m%d_%H%M%S)"
  tmp="$INCOMING/.tmp_$ts"
  dest="$INCOMING/card_$ts"
  count="$(find "$card" -type f -not -path '*/@*' 2>/dev/null | wc -l | tr -d ' ')"

  log "new card at $card (${sig:0:12}, ~${count} files) -> $dest"
  rm -rf "$tmp"; mkdir -p "$tmp"

  # Verified copy: rsync whole-file-checksums every transferred file.
  if ! rsync -a --checksum --no-perms --no-owner --no-group "$card"/ "$tmp"/; then
    log "rsync FAILED for $card — nothing handed to the watcher"
    rm -rf "$tmp"
    return 1
  fi

  # Atomic hand-off: the watcher's incoming/*/ glob ignores dot-dirs, so it only
  # sees the batch after this rename completes.
  mv "$tmp" "$dest"
  : > "$STATE/$sig"
  log "copied $card -> $dest (verified); watcher will import it"
}

shopt -s nullglob
for card in /volumeUSB*/usbshare*; do
  [ -d "$card" ] || continue
  copy_card "$card" || true
done

# Bound the marker dir: forget cards not seen in 30 days (re-insert re-copies; Immich dedupes).
find "$STATE" -type f -mtime +30 -delete 2>/dev/null || true

exit 0
