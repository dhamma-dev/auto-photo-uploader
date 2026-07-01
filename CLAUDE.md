# Photo import pipeline — project context

## What this is
An automated pipeline that imports DSLR photos off an SD card into a self-hosted
Immich instance, so a card can be inserted, imported, organized, and safely wiped
with no manual work. Solo homelab project. The real goal is *trust*: the human
should be confident enough that photos are safely in Immich to reuse the card.

## Environment (fixed constraints)
- NAS: Synology (DSM), Docker via Container Manager. Existing services live under
  `/volume1/docker/projects/`.
- Immich: already running as a MANAGED library (not external). Its storage
  template files photos into `upload/library/admin/YYYY/YYYY-MM-DD/`.
  DO NOT change how Immich stores or organizes files. The pipeline FEEDS Immich;
  it never restructures the existing library.
- Camera: Sony, shooting RAW+JPEG. Files look like `GSH05100.arw` + `GSH05100.jpg`.
- Timezone: America/Vancouver.

## Architecture (chosen: all-Docker)
Card in → watcher container copies to staging (checksum-verified) →
`immich upload` into the existing managed library (server-side hash dedupe) →
auto-stacker collapses each RAW+JPEG pair → push notification "safe to wipe".

- `watcher` container: privileged, bind-mounts the Synology USB parent with
  `rshared` propagation, polls for an inserted card, checksum-copies to staging,
  runs `immich upload`, then pings ntfy.
- `stacker` container: `ghcr.io/tenekev/immich-auto-stack` on a cron. Its default
  rule (group by base filename, promote `.jpg`) already handles `.arw`+`.jpg` —
  no custom criteria needed.

## Key decisions & rationale
- Managed library, not external: keep Immich exactly as it already works. Managed
  upload also dedupes by hash, which hands us idempotency for free.
- Idempotency comes from two layers: incremental/verified copy off the card, plus
  Immich hash dedupe on upload. Re-inserting the same card is a no-op. The watcher
  additionally skips a card whose filename+size signature it has already seen
  (stored under `staging/.state`).
- TWO notifications, and only the second authorizes wiping: a quiet "copied to
  staging" one, then the load-bearing "in Immich — safe to wipe" one. NEVER fire
  the safe-to-wipe signal before `immich upload` returns success.
- Nothing auto-formats the card. The human wipes it after the push arrives.

## Known gotchas
- USB passthrough is the fragile link. Synology's Docker won't pass a hot-plugged
  card in without privileged + `rshared` propagation, and the card mounts AFTER
  the container starts. VALIDATE THIS FIRST, in isolation: insert a card, then
  `docker exec photo-watcher ls /mnt/usb/usbshare`. Empty = propagation failed.
  Fallback: use Synology USB Copy for just the card→staging copy and point the
  watcher at the staging folder instead (everything downstream is identical).
- Confirm the USB mount path with `ls /volumeUSB*` (port 1 = `/volumeUSB1`).
- Verify immich-auto-stack's env var names against the image's current README
  (some forks use `RUN_MODE`/`CRON_INTERVAL` instead of `CRON_EXPRESSION`).

## Files
- `docker-compose.yml` — the two services. Fill in NAS LAN IP, Immich API key, and
  an ntfy topic before running.
- `Dockerfile` — watcher image (node + `@immich/cli` + rsync + curl).
- `watch-import.sh` — the watcher loop: detect → verified copy → upload → notify,
  with per-card signature idempotency.

## Current status
Phase 1 drafted, NOT yet deployed. Next action: validate USB passthrough in
isolation before wiring up or debugging the upload path.

## Phase 2 (not started): voice-memo metadata
Record voice memos while shooting; transcribe locally (Whisper); align memo
timestamps to photo capture times; use an LLM to generate captions/tags/album
names; write them as `.xmp` sidecars next to the photos, then trigger Immich's
sidecar sync. Hooks in right after a successful `immich upload` in the watcher.
Crux: camera-vs-recorder clock drift — plan to shoot a "photo of the phone clock"
at the start of each session so the offset can be computed and corrected.

## Conventions
- Keep everything under `/volume1/docker/projects/photo-import/`.
- Do not modify the existing Immich stack or its config.
- Shell: bash with `set -uo pipefail`; prefer `rsync --checksum` for verified copies.
