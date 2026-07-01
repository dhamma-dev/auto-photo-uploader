# Photo import pipeline — project context

## What this is
An automated pipeline that imports DSLR photos off an SD card into a self-hosted
Immich instance, so a card can be inserted, imported, organized, and safely wiped
with no manual work. Solo homelab project. The real goal is *trust*: the human
should be confident enough that photos are safely in Immich to reuse the card.

## Environment (fixed constraints)
- NAS: Synology DS923+ (DSM), Docker via Container Manager / Portainer. Existing
  services live under `/volume1/docker/projects/`.
- Immich: already running as a MANAGED library (not external). Its storage
  template files photos into `upload/library/admin/YYYY/YYYY-MM-DD/`.
  DO NOT change how Immich stores or organizes files. The pipeline FEEDS Immich;
  it never restructures the existing library.
- Camera: Sony, shooting RAW+JPEG. Files look like `GSH07714.ARW` + `GSH07714.JPG`
  under `DCIM/100MSDCF/` (extensions uppercase).
- Timezone: America/Vancouver.

## Architecture (chosen: Synology USB Copy → unprivileged Docker watcher)
Card in → **Synology USB Copy** copies it (Data-copy mode, on connect) to a
timestamped batch folder under `incoming/` → **watcher** container detects the
finished copy → **immich-go** uploads into the existing managed library
(server-side hash dedupe) and stacks each RAW+JPEG pair at upload → watcher
**verifies every file is in Immich by content hash** → push "safe to wipe" →
watcher deletes its staging copy.

- Ingest = **Synology USB Copy** (DSM package), NOT container USB passthrough
  (passthrough was tested and rejected — see gotchas). Task: Data copy, source =
  the card, destination = `/volume1/docker/projects/photo-import/incoming`, run on
  connect. It creates a per-copy subfolder like `USB Copy_<date>_<time>/`,
  preserving the `DCIM/` tree. It does NOT verify the copy — the card→staging leg
  is unverified; we accept that residual risk (the card stays un-wiped as a backup
  until the human wipes it).
- `watcher` container: UNPRIVILEGED. Bind-mounts
  `/volume1/docker/projects/photo-import` at `/data`, polls `incoming/` for batch
  folders, waits for each to settle, gates on a `DCIM/` dir, uploads with
  immich-go, verifies by hash, pings ntfy, then deletes the batch. Image:
  `ghcr.io/dhamma-dev/photo-import-watcher`.
- Stacker: RETIRED. immich-go stacks RAW+JPEG at upload
  (`--manage-raw-jpeg=StackCoverJPG`, JPG as cover); no separate stacker needed.

## Key decisions & rationale
- Managed library, not external: keep Immich exactly as it already works. Managed
  upload dedupes by hash, which hands us idempotency for free.
- Uploader = **immich-go** (proven on the user's ~46k initial import), not the
  official `@immich/cli`. Single static binary (no node), stacks RAW+JPEG at
  upload, continues past per-file errors.
- The "safe to wipe" gate is an **independent server-side hash re-query**, not any
  uploader exit code. After upload, sha1 every media file and POST
  `/api/assets/bulk-upload-check`; a file is present iff the result is
  `action:reject` + `reason:duplicate`. Fire safe-to-wipe (and delete staging)
  ONLY when every file is confirmed; otherwise it's a failure. Neither the CLI nor
  immich-go has a reliable partial-failure exit code — hence the re-query.
- Idempotency: the batch folder's existence = "not done"; success deletes it.
  Re-inserting a card makes a new batch; immich-go + Immich hash-dedupe make the
  re-upload a no-op and the verify re-confirms "safe to wipe".
- TWO notifications, only the second authorizes wiping: a quiet "copy done —
  uploading" one, then the load-bearing "safe to wipe" one — fired only after the
  hash verify passes.
- Trigger scope: only batches containing a `DCIM/` tree are processed (camera
  cards, not any folder). Every outcome notifies (uploading / safe / failed);
  failures are debounced so an Immich outage doesn't spam pushes.
- Nothing auto-formats the card. The human wipes it after the push arrives.

## Known gotchas
- USB passthrough is DEAD on this NAS. Tested 2026-07-01: Docker rejects the
  rshared bind with `path /volumeUSB1 is mounted on / but it is not a shared
  mount` (host `/` isn't shared; the fix `mount --make-rshared /` doesn't persist
  across reboot, and `/volumeUSB1` doesn't exist without a card). Hence the USB
  Copy design. Do not revisit passthrough.
- USB Copy gives no "copy complete" signal and no post-script hook, so the watcher
  waits for the batch to be size/count-stable (`SETTLE_SECONDS`) before touching
  it.
- USB Copy does not verify its copy — the card→staging leg is unverified; the hash
  re-query only proves staging→Immich.
- immich-go wants the Immich BASE url (no `/api`); the verify call adds `/api`
  itself. Version is pinned in the Dockerfile; DS923+ is x86_64
  (`immich-go_Linux_x86_64.tar.gz`).

## Files
- `Dockerfile` — watcher image: debian + pinned immich-go + curl/jq.
- `watch-import.sh` — the watcher loop: settle → DCIM gate → immich-go upload →
  hash-verify → notify → delete batch.
- `docker-compose.yml` — the single watcher service; deploy as a Portainer stack.
  Fill in NAS IP, Immich API key, ntfy topic.
- `.github/workflows/watcher.yml` — builds & publishes the image to GHCR on push.
- `usb-passthrough-test/` — throwaway probe used to (dis)prove passthrough; kept as
  a record, NOT part of the running pipeline.

## Deploy
Images publish to GHCR (`ghcr.io/dhamma-dev/*`) via GitHub Actions on push to
`main`; deploy on the NAS by pulling in a Portainer stack. GHCR packages default
to private — make the package public or add a Portainer registry credential.

## Current status
DEPLOYED and working end-to-end (2026-07-01): USB Copy → unprivileged
folder-watch watcher → immich-go upload → hash-verified "safe to wipe". The full
Phase 1 trust pipeline is live. Maintenance knobs: immich-go version is pinned in
the Dockerfile (bump the ARG + push if an Immich server upgrade breaks uploads);
raise `SETTLE_SECONDS` if a large card ever gets grabbed mid-copy. Next up is
Phase 2 (voice-memo metadata), not started.

## Phase 2 (not started): voice-memo metadata
Record voice memos while shooting; transcribe locally (Whisper); align memo
timestamps to photo capture times; use an LLM to generate captions/tags/album
names; write them as `.xmp` sidecars next to the photos, then trigger Immich's
sidecar sync. Hooks in right after a successful verify in the watcher. Crux:
camera-vs-recorder clock drift — plan to shoot a "photo of the phone clock" at the
start of each session so the offset can be computed and corrected.

## Conventions
- Keep everything under `/volume1/docker/projects/photo-import/`.
- Do not modify the existing Immich stack or its config.
- Shell: bash with `set -uo pipefail`.
- Ship NAS components as published GHCR images + a Portainer stack (not
  build-on-NAS).
