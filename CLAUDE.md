# Photo importer — project context

## Goal
Put an SD card into the NUC, open a web page (phone or PC), press **Import**, and
the page reports when every photo is confirmed in Immich. Nothing runs, polls, or
writes when not importing. Low barrier to entry + confidence are the whole point.

This is a from-scratch rewrite (2026-10-11). A previous NAS-based pipeline
(host poll script + watcher container) was scrapped: it polled 24/7 and a failed
job retried and logged for months. Its code lives only in git history — do not
resurrect its design.

## Requirements
1. User-triggered only. No schedulers, polling loops, or background watchers.
2. Idle = zero CPU and zero disk writes (a server blocked on a socket is fine).
3. No infinite retries. One button press = one attempt; failures stop and show
   which files are missing, with a "retry missing" action.
4. The card is mounted/read read-only. The app never writes to it.
5. "Done" = independent verification: sha1 every media file on the card and
   confirm each is in Immich. Never trust the uploader's exit code.
6. RAW+JPEG stacked, JPEG as cover; original filenames preserved.
7. Bounded history (e.g. last 50 imports). No unbounded logs.
8. Runs at low priority (nice/ionice) — Plex shares the NUC and must win.

## Environment
- **NUC** (Intel Hades Canyon, Ubuntu, Docker, no Portainer): runs this app.
  Also runs Plex. Wired Ethernet to the NAS. Has SD slot(s); easy to reach.
- **NAS** (Synology DS923+): runs Immich as a MANAGED library. Do not change
  Immich's config or storage layout; this app only feeds it via its API.
- Camera: Sony, RAW+JPEG, e.g. `DCIM/100MSDCF/GSH07714.ARW` + `.JPG` (uppercase).
- Timezone: America/Vancouver.
- The NAS is currently the only copy of the photos. The UI says "in Immich",
  not "backed up".

## Design
- One container on the NUC: Python (FastAPI) web app + bundled immich-go.
  LAN-only.
- Card access: host udev rule mounts SD cards read-only at `/media/cards/*`;
  the container binds `/media/cards:/cards:ro,rslave`.
- Card reads are slow (~40 MB/s), so: hash every file once → ask Immich which
  are present → upload only what's missing → re-check using the
  already-computed hashes (no second full read for the verify).
- Flow: page lists inserted cards (count/size/date range) → Import → immich-go
  uploads with `--manage-raw-jpeg=StackCoverJPG` → verify every file by hash →
  show "N of N confirmed" or the missing list.
- Deploy: image published to `ghcr.io/dhamma-dev/*` by GitHub Actions; on the
  NUC, plain `docker compose pull && docker compose up -d`. GHCR packages
  default to private.

## Verified facts worth keeping
- Verify via `POST /api/assets/bulk-upload-check` with
  `{assets:[{id, checksum: <sha1>}]}`. A file is in Immich iff the result is
  `action:"reject"` + `reason:"duplicate"`; `action:"accept"` means missing.
- immich-go returns exit 0 even when some files failed — hence the hash verify.
- immich-go takes the Immich base URL (no `/api`).
- Immich server is on v3; immich-go 0.32.0 was compatible.

## Status / order of work
0. Feasibility check on the NUC — DONE 2026-10-11. Snap Docker (its sandbox
   blocked DNS) was replaced with docker-ce. Built-in slot = `mmcblk0p1`
   (exFAT). `host/99-photo-card.rules` (udev → systemd-mount) mounts cards
   read-only at `/media/cards/<kernel name>` on insert, and they're unmounted on
   removal. A container with `/media/cards:/cards:ro,rslave` sees hot-plugged
   cards. The built-in reader manages only ~40 MB/s, so read the card as few
   times as possible.
1. Minimal app: detect card → import → verify → result.
2. Polish: history, phone-friendly UI, live progress.
3. Later, separately: Lightroom-edited JPEG exports replacing the stacked JPEG;
   voice-memo captioning (Whisper + LLM → XMP sidecars); offsite backup.

## Conventions
- Python for the app; bash with `set -uo pipefail` for any scripts.
- Keep the user in the loop for anything on the NUC/NAS — I can't reach them.
