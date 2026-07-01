#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# photo-import watcher (folder-watch design)
#
# Synology USB Copy drops each inserted card into a timestamped subfolder under
# $INCOMING, e.g. "USB Copy_2026-07-01_154145/DCIM/100MSDCF/GSH07714.ARW".
# For each such batch this watcher:
#   settle -> DCIM gate -> immich-go upload -> INDEPENDENT hash re-query verify
#   -> "safe to wipe" push -> delete the staging copy.
#
# LOAD-BEARING RULE: the "safe to wipe" notification fires ONLY after every media
# file in the batch is confirmed present in Immich by content hash — never on an
# uploader exit code alone. Failures never authorize a wipe.
#
# No USB passthrough: the container is unprivileged and only watches a folder.
# ---------------------------------------------------------------------------
set -uo pipefail

INCOMING="${INCOMING:-/data/incoming}"
STATE_DIR="${STATE_DIR:-/data/state}"
POLL_SECONDS="${POLL_SECONDS:-15}"
SETTLE_SECONDS="${SETTLE_SECONDS:-20}"
FAIL_RENOTIFY_SECONDS="${FAIL_RENOTIFY_SECONDS:-1800}"
UPLOAD_TIMEOUT="${UPLOAD_TIMEOUT:-7200}"
NTFY_URL="${NTFY_URL:-}"

# immich-go wants the base URL (no /api); our verify calls append /api themselves.
BASE="${IMMICH_INSTANCE_URL:-}"; BASE="${BASE%/}"; BASE="${BASE%/api}"
IMMICH_API_KEY="${IMMICH_API_KEY:-}"

mkdir -p "$STATE_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }

notify() { # notify TITLE BODY [priority]
  [ -z "$NTFY_URL" ] && return 0
  curl -fsS --max-time 20 -H "Title: ${1}" -H "Priority: ${3:-default}" \
       -d "${2}" "$NTFY_URL" >/dev/null 2>&1 || true
}

is_media() {
  case "${1,,}" in
    *.arw|*.jpg|*.jpeg|*.cr2|*.cr3|*.nef|*.dng|*.raf|*.orf|*.rw2|*.heic|*.heif|*.mp4|*.mov|*.m4v) return 0 ;;
    *) return 1 ;;
  esac
}

# "count<TAB>bytes" of real files, ignoring Synology @-dirs (@tmp, @eaDir, ...).
measure() {
  find "$1" -type f -not -path '*/@*' -printf '%s\n' 2>/dev/null \
    | awk 'BEGIN{n=0;b=0}{n++;b+=$1}END{print n"\t"b}'
}

has_dcim() {
  [ -n "$(find "$1" -maxdepth 4 -type d -iname DCIM -not -path '*/@*' 2>/dev/null | head -1)" ]
}

# Confirm EVERY media file is already on the server, by sha1. A file counts as
# present iff bulk-upload-check returns action:reject + reason:duplicate for it.
# Returns 0 only if nothing is missing AND every query succeeded (fail closed).
verify_all_present() {
  local dir="$1"
  local api="${BASE}/api/assets/bulk-upload-check"
  local list; list="$(mktemp)"

  find "$dir" -type f -not -path '*/@*' 2>/dev/null | while IFS= read -r f; do
    is_media "$f" || continue
    printf '%s\t%s\n' "$(sha1sum "$f" | awk '{print $1}')" "${f#"$dir"/}"
  done > "$list"

  if [ ! -s "$list" ]; then rm -f "$list"; log "verify: no media files in batch"; return 1; fi

  local total missing=0 rc=0 chunkdir c
  total="$(wc -l < "$list" | tr -d ' ')"
  chunkdir="$(mktemp -d)"
  split -l 500 "$list" "$chunkdir/c."

  for c in "$chunkdir"/c.*; do
    local payload resp m
    payload="$(jq -Rn '[inputs|split("\t")|{id:.[1],checksum:.[0]}]|{assets:.}' < "$c")"
    resp="$(curl -fsS --max-time 120 -X POST "$api" \
              -H "x-api-key: ${IMMICH_API_KEY}" \
              -H 'Content-Type: application/json' \
              -d "$payload" 2>/dev/null)" || { rc=1; break; }
    m="$(printf '%s' "$resp" | jq '[.results[]|select(.action!="reject" or .reason!="duplicate")]|length' 2>/dev/null)"
    [ -n "$m" ] || { rc=1; break; }
    missing=$(( missing + m ))
  done
  rm -rf "$list" "$chunkdir"

  [ "$rc" -eq 0 ] || { log "verify: server query failed"; return 1; }
  log "verify: ${total} media files, ${missing} NOT confirmed in Immich"
  [ "$missing" -eq 0 ]
}

fail_notify() { # fail_notify NAME MESSAGE   (debounced to 1 push / FAIL_RENOTIFY_SECONDS)
  local name="$1" msg="$2" lastfail="$STATE_DIR/$1.lastfail" now last=0
  now="$(date +%s)"
  [ -f "$lastfail" ] && last="$(cat "$lastfail" 2>/dev/null || echo 0)"
  if [ $(( now - last )) -ge "$FAIL_RENOTIFY_SECONDS" ]; then
    notify "⚠️ Import FAILED" "$msg (retrying)" "urgent"
    echo "$now" > "$lastfail"
  fi
  log "FAILURE [$name]: $msg"
}

process_batch() {
  local dir="$1" name; name="$(basename "$dir")"
  local seen="$STATE_DIR/${name}.seen"

  has_dcim "$dir" || return 0            # not a camera-card copy -> ignore

  # Settle: only proceed once file count + bytes are stable (copy finished).
  local m1 m2 cnt
  m1="$(measure "$dir")"; sleep "$SETTLE_SECONDS"; m2="$(measure "$dir")"
  if [ "$m1" != "$m2" ]; then log "[$name] still copying, will re-check"; return 0; fi
  cnt="${m2%%$'\t'*}"
  [ "${cnt:-0}" -gt 0 ] || return 0

  if [ ! -f "$seen" ]; then
    notify "Copy done — uploading" "Sending ${cnt} files from ${name} to Immich..." "low"
    : > "$seen"
  fi

  log "[$name] uploading ${cnt} files via immich-go"
  if ! timeout "$UPLOAD_TIMEOUT" immich-go upload from-folder \
        --server="$BASE" --api-key="$IMMICH_API_KEY" \
        --no-ui --on-errors=continue \
        --manage-raw-jpeg=StackCoverJPG \
        "$dir"; then
    fail_notify "$name" "immich-go upload errored on ${name}. Card is NOT safe to wipe."
    return 1
  fi

  if ! verify_all_present "$dir"; then
    fail_notify "$name" "Not all files from ${name} are confirmed in Immich. NOT safe to wipe."
    return 1
  fi

  notify "✅ Safe to wipe" "${cnt} files from ${name} are verified in Immich. This card can be erased." "high"
  rm -rf -- "$dir"
  rm -f "$seen" "$STATE_DIR/${name}.lastfail"
  log "[$name] verified + staging copy removed"
  return 0
}

# --- startup sanity ---
[ -n "$BASE" ]           || log "WARNING: IMMICH_INSTANCE_URL is empty — uploads will fail until set"
[ -n "$IMMICH_API_KEY" ] || log "WARNING: IMMICH_API_KEY is empty — uploads will fail until set"
[ -n "$NTFY_URL" ]       || log "NOTE: NTFY_URL is empty — notifications disabled"
log "photo-import watcher: watching ${INCOMING} every ${POLL_SECONDS}s (settle ${SETTLE_SECONDS}s)"

while true; do
  if [ -d "$INCOMING" ]; then
    for d in "$INCOMING"/*/; do
      [ -d "$d" ] || continue
      d="${d%/}"
      case "$(basename "$d")" in @*) continue ;; esac   # skip Synology @-dirs
      process_batch "$d" || true
    done
  fi
  sleep "$POLL_SECONDS"
done
