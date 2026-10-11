"""Cards and the import job: fingerprint -> check -> upload missing -> verify -> album/tags.

Nothing here runs unless the user presses Import. One import at a time, one attempt
per press, no background retries.
"""
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from immich import Immich, ImmichError
from media import MEDIA_EXTS, MediaFile, Shot, capture_time, date_source, group_shots, scan_card, summarize_sessions
from store import Store

HASH_BLOCK = 4 * 1024 * 1024
SAVE_EVERY = 25  # files; so a cancelled import keeps the fingerprints it already paid for
LOG_TAIL_BYTES = 256 * 1024
SYSFS_BLOCK = Path(os.environ.get("SYSFS_BLOCK", "/sys/class/block"))
# immich-go --no-ui prints this every 500 ms (as \r-terminated updates).
UPLOAD_PROGRESS = re.compile(r"Assets found: (\d+), Upload errors: (\d+), Uploaded (\d+)")


class ImportFailed(Exception):
    """A failure with a message that's already fit to show on the page."""


def card_id(name: str) -> str | None:
    """The SD card's hardware CID (unique per physical card) from sysfs, e.g. mmcblk0p1 -> mmcblk0."""
    disk = re.sub(r"p\d+$", "", name)
    for attr in ("cid", "serial"):
        try:
            v = (SYSFS_BLOCK / disk / "device" / attr).read_text().strip()
        except OSError:
            continue
        if v:
            return f"{attr}:{v}"
    return None


@dataclass
class CardView:
    name: str
    root: Path
    key: str
    store: Store
    files: list[MediaFile]
    shots: list[Shot]


@dataclass
class Job:
    id: str
    card: str
    started: float
    request: dict
    status: str = "running"  # running | success | failed | cancelled
    cancelling: bool = False
    phase: str = "scanning"  # scanning | hashing | checking | uploading | verifying | organizing | done
    message: str = ""
    sessions: list[str] = field(default_factory=list)
    shots: int = 0
    files: int = 0
    bytes: int = 0
    hash_bytes: int = 0  # bytes that actually needed reading (not remembered)
    hashed_bytes: int = 0
    already: int = 0
    to_upload: int = 0
    upload_files: int = 0  # handed to immich-go (whole shots, for stacking)
    uploaded: int = 0
    upload_errors: int = 0
    phase_started: float = 0
    confirmed: int = 0
    missing: list[str] = field(default_factory=list)
    album: str = ""
    album_url: str = ""
    tags: list[str] = field(default_factory=list)
    organize_error: str = ""
    uploader_exit: int | None = None
    uploader_output: list[str] = field(default_factory=list)
    finished: float | None = None


class Importer:
    def __init__(self, cards_dir: Path, data_dir: Path, immich: Immich, immich_url: str, api_key: str,
                 public_url: str, history_limit: int, upload_concurrency: int, immich_go: str = "immich-go"):
        self.cards_dir = cards_dir
        self.history_dir = data_dir / "imports"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(str(data_dir / "fingerprints.db"))
        self.mem_store = Store(":memory:")  # for a card whose hardware id can't be read
        self.immich = immich
        self.immich_url = immich_url
        self.api_key = api_key
        self.public_url = public_url.rstrip("/")
        self.history_limit = history_limit
        self.upload_concurrency = upload_concurrency
        self.immich_go = immich_go
        self.lock = threading.Lock()
        self.job: Job | None = None
        self._cancel = threading.Event()

    # ---- cards ----

    def mounted(self) -> list[Path]:
        if not self.cards_dir.is_dir():
            return []
        # Empty leftover mount-point dirs stay behind after a card is pulled.
        return [p for p in sorted(self.cards_dir.iterdir()) if os.path.ismount(p) and (p / "DCIM").is_dir()]

    def load_card(self, name: str) -> CardView:
        root = self.cards_dir / name
        files = scan_card(root)
        cid = card_id(name)
        store, key = (self.store, cid) if cid else (self.mem_store, f"slot:{name}")
        known = store.lookup(key)
        for f in files:
            f.sha1 = known.get((f.rel, f.size, f.mtime_ns), (None, None))[0]
        shots = group_shots(files)
        learned = []
        for s in shots:
            src = date_source(s)
            s.taken = known.get((src.rel, src.size, src.mtime_ns), (None, None))[1]
            if s.taken is None:
                s.taken = capture_time(src)
                learned.append((src.rel, src.size, src.mtime_ns, None, s.taken))
        store.save(key, learned)
        return CardView(name, root, key, store, files, shots)

    def cards(self) -> list[dict]:
        out = []
        for root in self.mounted():
            busy = self.running() and self.job.card == root.name
            if busy:  # don't compete with the running import for the slow card
                out.append({"name": root.name, "busy": True})
                continue
            cv = self.load_card(root.name)
            hashes = {f.rel: f.sha1 for f in cv.files if f.sha1}
            error = ""
            try:
                present = self.immich.present(hashes) if hashes else {}
            except ImmichError as e:
                present, error = {}, str(e)
            sessions = summarize_sessions(cv.shots, set(present))
            out.append({
                "name": root.name,
                "busy": False,
                "remembered": bool(hashes),
                "files": len(cv.files),
                "bytes": sum(f.size for f in cv.files),
                "shots": len(cv.shots),
                "all_in_immich": bool(cv.shots) and all(s["in_immich"] == s["shots"] for s in sessions),
                "sessions": sessions,
                "error": error,
            })
        return out

    # ---- job control ----

    def running(self) -> bool:
        return bool(self.job and self.job.status == "running")

    def start(self, req: dict) -> Job:
        with self.lock:
            if self.running():
                raise RuntimeError("An import is already running")
            card = req.get("card", "")
            if "/" in card or card in ("", ".", "..") or not any(p.name == card for p in self.mounted()):
                raise FileNotFoundError(f"No card named {card!r} is inserted")
            self._cancel.clear()
            self.job = Job(id=datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
                           card=card, started=time.time(), request=req,
                           album=(req.get("album") or "").strip(),
                           tags=[t.strip() for t in req.get("tags") or [] if t.strip()])
            threading.Thread(target=self._run, args=(self.job,), daemon=True).start()
            return self.job

    def cancel(self) -> bool:
        if not self.running():
            return False
        self._cancel.set()
        self.job.cancelling = True
        return True

    def current(self) -> dict | None:
        return asdict(self.job) if self.job else None

    def history(self, limit: int = 10) -> list[dict]:
        out = []
        for d in sorted(self.history_dir.iterdir(), reverse=True)[:limit]:
            try:
                rec = json.loads((d / "record.json").read_text())
            except (OSError, ValueError):
                continue
            rec["missing_count"] = len(rec.pop("missing", []))
            rec.pop("uploader_output", None)
            out.append(rec)
        return out

    # ---- the job ----

    def _run(self, job: Job) -> None:
        job_dir = self.history_dir / job.id
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._import(job, job_dir)
        except (ImportFailed, ImmichError) as e:
            self._fail(job, str(e))
        except OSError as e:
            self._fail(job, f"Couldn't read the card (was it removed?): {e}")
        except Exception as e:  # noqa: BLE001 — surface anything unexpected on the page
            self._fail(job, f"Unexpected error: {e!r}")
        finally:
            job.finished = time.time()
            job.phase = "done"
            (job_dir / "record.json").write_text(json.dumps(asdict(job), indent=1))
            self._prune()
            self.store.prune()

    def _set_phase(self, job: Job, phase: str, message: str) -> None:
        job.phase, job.message, job.phase_started = phase, message, time.time()

    def _import(self, job: Job, job_dir: Path) -> None:
        self._set_phase(job, "scanning", "Reading the card's file list")
        cv = self.load_card(job.card)
        wanted = job.request.get("sessions")  # None = everything on the card
        shots = [s for s in cv.shots if wanted is None or s.session in wanted]
        if not shots:
            raise ImportFailed("Nothing selected to import")
        files = [f for s in shots for f in s.files]
        if job.request.get("reread"):
            for f in files:
                f.sha1 = None
        job.sessions = sorted({s.session for s in shots})
        job.shots, job.files, job.bytes = len(shots), len(files), sum(f.size for f in files)
        job.hash_bytes = sum(f.size for f in files if not f.sha1)

        self._set_phase(job, "hashing", "Reading files to fingerprint them")
        self._hash(job, cv, files)

        self._set_phase(job, "checking", "Asking Immich which files it already has")
        hashed = {f.rel: f.sha1 for f in files if f.sha1}
        present = self.immich.present(hashed)
        job.already = len(present)
        job.to_upload = len(files) - len(present)

        missing_shots = [s for s in shots if any(f.rel not in present for f in s.files)]
        if missing_shots and not self._cancel.is_set():
            upload = [f for s in missing_shots for f in s.files]  # whole shots, so RAW+JPEG can stack
            self._set_phase(job, "uploading", f"Uploading {job.to_upload} new file(s) to Immich")
            self._upload(job, cv.root, upload, job_dir)

        self._set_phase(job, "verifying", "Confirming every file is in Immich")
        present = self.immich.present(hashed)
        job.confirmed = len(present)
        job.missing = [f.rel for f in files if f.rel not in present]

        if self._cancel.is_set():
            job.status = "cancelled"
            job.message = f"Cancelled. {job.confirmed} of {job.files} selected file(s) are in Immich"
            return
        if job.missing:
            job.status = "failed"
            job.message = f"{len(job.missing)} of {job.files} file(s) are NOT in Immich"
            return

        if job.album or job.tags:
            self._set_phase(job, "organizing", "Adding to album / tagging")
            asset_ids = sorted({a for a in present.values() if a})
            try:
                if job.album:
                    album_id = self.immich.add_to_album(job.album, asset_ids)
                    job.album_url = f"{self.public_url}/albums/{album_id}"
                if job.tags:
                    self.immich.tag(job.tags, asset_ids)
            except ImmichError as e:
                # The photos are safe; only the organizing failed.
                job.organize_error = f"Photos are safe in Immich, but organizing failed: {e}"
        # Only now: the page stops following the job as soon as status leaves "running".
        job.message = f"All {job.files} file(s) confirmed in Immich"
        job.status = "success"

    def _hash(self, job: Job, cv: CardView, files: list[MediaFile]) -> None:
        pending = []
        for f in files:
            if f.sha1:
                continue
            if self._cancel.is_set():
                break
            h = hashlib.sha1()
            with open(f.path, "rb") as fh:
                while block := fh.read(HASH_BLOCK):
                    h.update(block)
                    job.hashed_bytes += len(block)
                    if self._cancel.is_set():
                        break
            if self._cancel.is_set():
                break  # partial hash: discard
            f.sha1 = h.hexdigest()
            pending.append((f.rel, f.size, f.mtime_ns, f.sha1, None))
            if len(pending) >= SAVE_EVERY:
                cv.store.save(cv.key, pending)
                pending = []
        cv.store.save(cv.key, pending)

    def _upload(self, job: Job, root: Path, files: list[MediaFile], job_dir: Path) -> None:
        # immich-go gets a folder of symlinks to exactly the selected, missing shots
        # (it follows them for content, size and date). Lives in tmpfs: no disk writes.
        farm = Path(tempfile.mkdtemp(prefix=f"import-{job.id}-"))
        try:
            for f in files:
                link = farm / f.rel
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(f.path)
            job.upload_files = len(files)
            cmd = [
                self.immich_go, "upload", "from-folder",
                "--no-ui",
                "--pause-immich-jobs=false",  # never touch Immich's job queue
                "--on-errors=continue",       # upload what it can; our verify is the judge
                "--manage-raw-jpeg=StackCoverJPG",
                f"--include-extensions={','.join(sorted(MEDIA_EXTS))}",
                f"--concurrent-tasks={self.upload_concurrency}",
                # immich-go otherwise writes a new log into ~/.cache on every run, forever.
                f"--log-file={job_dir / 'immich-go.log'}",
                str(farm),
            ]
            env = {**os.environ, "IMMICH_GO_UPLOAD_SERVER": self.immich_url, "IMMICH_GO_UPLOAD_API_KEY": self.api_key}
            tail: collections.deque[str] = collections.deque(maxlen=60)
            # text=True turns immich-go's \r progress updates into separate lines.
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, errors="replace")
            threading.Thread(target=self._stop_on_cancel, args=(proc,), daemon=True).start()
            assert proc.stdout
            for line in proc.stdout:
                line = line.strip()
                if m := UPLOAD_PROGRESS.search(line):
                    job.upload_errors, job.uploaded = int(m[2]), int(m[3])
                elif line:
                    tail.append(line)
                    job.uploader_output = list(tail)
            job.uploader_exit = proc.wait()
        finally:
            shutil.rmtree(farm, ignore_errors=True)
            _truncate_head(job_dir / "immich-go.log", LOG_TAIL_BYTES)

    def _stop_on_cancel(self, proc: subprocess.Popen) -> None:
        # Only alive while immich-go runs, i.e. during an import the user started.
        while proc.poll() is None:
            if self._cancel.wait(1):
                proc.terminate()
                try:
                    proc.wait(15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return

    def _fail(self, job: Job, message: str) -> None:
        job.status = "failed"
        job.message = message

    def _prune(self) -> None:
        dirs = sorted(d for d in self.history_dir.iterdir() if d.is_dir())
        for d in dirs[: max(0, len(dirs) - self.history_limit)]:
            shutil.rmtree(d, ignore_errors=True)


def _truncate_head(path: Path, keep: int) -> None:
    """Keep only the last `keep` bytes of a log file."""
    try:
        if path.stat().st_size <= keep:
            return
        with open(path, "rb") as fh:
            fh.seek(-keep, os.SEEK_END)
            data = fh.read()
        path.write_bytes(b"[...truncated...]\n" + data)
    except OSError:
        pass
