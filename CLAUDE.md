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
1. Minimal app: detect card → import → verify → result. BUILT 2026-10-11,
   tested locally against a fake Immich/immich-go; awaiting first NUC deploy.
2. Round 2 BUILT 2026-10-11 (tested locally, not yet deployed): cancel/resume,
   real upload progress + estimates + tab title, remembered cards, sessions by
   date with partial import, album + tags per import, Open in Immich.
   Ideas not built yet: "Immich still processing" status, per-import detail
   page, settings page, health panel, USB readers, AVCHD video.
3. Later, separately: Lightroom-edited JPEG exports replacing the stacked JPEG;
   voice-memo captioning (Whisper + LLM → XMP sidecars); offsite backup.

## Files
- `app/main.py` — FastAPI routes; sets low CPU/IO priority at startup.
- `app/media.py` — card scan, capture time (EXIF DateTimeOriginal for JPG/ARW,
  Sony clip XML for MP4, mtime fallback), shots (RAW+JPEG by path stem),
  sessions (calendar days of camera-local time).
- `app/store.py` — SQLite fingerprint store `/data/fingerprints.db`: sha1 +
  capture time keyed by (SD card hardware CID, path, size, mtime_ns). Capped
  (300k rows / 365 days). Re-inserting a known card needs no re-read.
- `app/importer.py` — the job: fingerprint → check → upload missing shots →
  verify → album/tags. Cancel. Bounded history in `/data/imports/<id>/`.
- `app/immich.py` — Immich API (users/me, bulk-upload-check, albums, tags).
- `app/static/index.html` — the whole UI (vanilla JS; polls only while an
  import runs and the tab is visible).
- `Dockerfile`, `docker-compose.yml` (for the NUC), `.env.example`,
  `.github/workflows/image.yml` (→ `ghcr.io/dhamma-dev/photo-importer`).
- `host/99-photo-card.rules` — udev rule installed on the NUC.

## How an import works
- The user picks sessions (days) on the card plus an optional album and tags.
- Only files without a remembered fingerprint are read. Fingerprints are saved
  every 25 files, so a cancelled import keeps its progress.
- immich-go is pointed at a tmpfs folder of symlinks to exactly the selected
  shots that are missing (whole shots, so RAW+JPEG still stack). It follows the
  symlinks for content, size and date.
- Verify = bulk-upload-check again with the same hashes. Album/tags are applied
  by the app (not immich-go) to every confirmed asset id, including ones that
  were already in Immich, and only on full success. Status flips to success
  only after that step, because the page stops following the job when the
  status changes.

## immich-go gotchas
- Always pass `--log-file`: by default it writes a new log in `~/.cache/immich-go/`
  on every run and never cleans them up.
- `--pause-immich-jobs` defaults to true (pauses Immich's job queue). We pass
  false: we don't touch Immich's state.
- `--no-ui` prints progress every 500 ms as `\r`-terminated
  `Assets found: N, Upload errors: E, Uploaded U` lines. We parse these for the
  progress bar and keep them out of the saved output.
- Server/key are passed via `IMMICH_GO_UPLOAD_SERVER` / `IMMICH_GO_UPLOAD_API_KEY`
  env vars so the key isn't in the process list.
- Sony video lives in `PRIVATE/M4ROOT/CLIP`; `THMBNL`/`SUB` are thumbnails or
  proxies, so only `DCIM` + `CLIP` are uploaded.

## Conventions
- Python for the app; bash with `set -uo pipefail` for any scripts.
- Keep the user in the loop for anything on the NUC/NAS — I can't reach them.
