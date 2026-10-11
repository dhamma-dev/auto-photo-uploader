"""Card discovery and the import job: hash -> check -> upload missing -> verify.

Nothing here runs unless the user presses Import. One import at a time, one attempt
per press, no background retries.
"""
import collections
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from immich import Immich, ImmichError

# What counts as a photo/video on a Sony card. Kept in sync with immich-go's
# --include-extensions so "what we verify" == "what gets uploaded".
MEDIA_EXTS = {".arw", ".jpg", ".jpeg", ".hif", ".heif", ".heic", ".dng", ".mp4", ".mov"}
# DCIM holds stills; Sony puts video in PRIVATE/M4ROOT/CLIP (its THMBNL/SUB siblings are
# thumbnails/proxies and must not be uploaded).
MEDIA_DIRS = ("DCIM", "PRIVATE/M4ROOT/CLIP")
HASH_BLOCK = 4 * 1024 * 1024
LOG_TAIL_BYTES = 256 * 1024


class ImportFailed(Exception):
    """A failure with a message that's already fit to show on the page."""


@dataclass
class MediaFile:
    rel: str
    path: Path
    size: int
    mtime: float


def scan_card(root: Path) -> list[MediaFile]:
    files = []
    for d in MEDIA_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [n for n in dirnames if not n.startswith(".")]
            for name in filenames:
                if name.startswith(".") or Path(name).suffix.lower() not in MEDIA_EXTS:
                    continue
                p = Path(dirpath) / name
                st = p.stat()
                files.append(MediaFile(str(p.relative_to(root)), p, st.st_size, st.st_mtime))
    files.sort(key=lambda f: f.rel)
    return files


def find_cards(cards_dir: Path) -> list[dict]:
    """Mounted cards with a DCIM folder. Stat-only, so cheap enough for each page load."""
    cards = []
    if not cards_dir.is_dir():
        return cards
    for entry in sorted(cards_dir.iterdir()):
        # Empty leftover mount-point dirs stay behind after a card is pulled.
        if not (os.path.ismount(entry) and (entry / "DCIM").is_dir()):
            continue
        try:
            files = scan_card(entry)
        except OSError:
            continue
        mtimes = [f.mtime for f in files]
        cards.append(
            {
                "name": entry.name,
                "files": len(files),
                "bytes": sum(f.size for f in files),
                "first": min(mtimes) if mtimes else None,
                "last": max(mtimes) if mtimes else None,
            }
        )
    return cards


@dataclass
class Job:
    id: str
    card: str
    started: float
    status: str = "running"  # running | success | failed
    phase: str = "scanning"  # scanning | hashing | checking | uploading | verifying | done
    message: str = ""
    files: int = 0
    bytes: int = 0
    hashed_bytes: int = 0
    already: int = 0
    to_upload: int = 0
    confirmed: int = 0
    missing: list[str] = field(default_factory=list)
    uploader_exit: int | None = None
    uploader_output: list[str] = field(default_factory=list)
    finished: float | None = None


class Importer:
    def __init__(self, cards_dir: Path, data_dir: Path, immich: Immich, immich_url: str, api_key: str,
                 history_limit: int, upload_concurrency: int, immich_go: str = "immich-go"):
        self.cards_dir = cards_dir
        self.history_dir = data_dir / "imports"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.immich = immich
        self.immich_url = immich_url
        self.api_key = api_key
        self.history_limit = history_limit
        self.upload_concurrency = upload_concurrency
        self.immich_go = immich_go
        self.lock = threading.Lock()
        self.job: Job | None = None
        # sha1 by (path, size, mtime) for the most recent card only, so "Retry"
        # doesn't re-read a slow card. Cleared when a different card is imported.
        self._hash_cache: dict[tuple, str] = {}
        self._hash_cache_card: str | None = None

    # ---- public ----

    def start(self, card: str) -> Job:
        with self.lock:
            if self.job and self.job.status == "running":
                raise RuntimeError("An import is already running")
            root = self.cards_dir / card
            if "/" in card or not (os.path.ismount(root) and (root / "DCIM").is_dir()):
                raise FileNotFoundError(f"No card named {card!r} is inserted")
            self.job = Job(id=datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
                           card=card, started=time.time())
            threading.Thread(target=self._run, args=(self.job, root), daemon=True).start()
            return self.job

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

    def record(self, job_id: str) -> dict | None:
        p = self.history_dir / Path(job_id).name / "record.json"
        return json.loads(p.read_text()) if p.is_file() else None

    # ---- the job ----

    def _run(self, job: Job, root: Path) -> None:
        job_dir = self.history_dir / job.id
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._import(job, root, job_dir)
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

    def _import(self, job: Job, root: Path, job_dir: Path) -> None:
        job.message = "Reading the card's file list"
        files = scan_card(root)
        job.files = len(files)
        job.bytes = sum(f.size for f in files)
        if not files:
            raise ImportFailed("No photos or videos found on the card")

        job.phase = "hashing"
        job.message = "Reading every file to fingerprint it"
        hashes = self._hash_all(job, files)

        job.phase = "checking"
        job.message = "Asking Immich which files it already has"
        present = self.immich.present(hashes)
        job.already = len(present)
        job.to_upload = len(files) - len(present)

        if job.to_upload:
            job.phase = "uploading"
            job.message = f"Uploading {job.to_upload} new file(s) to Immich"
            self._upload(job, root, job_dir)

        job.phase = "verifying"
        job.message = "Confirming every file is in Immich"
        present = self.immich.present(hashes)
        job.confirmed = len(present)
        job.missing = [f.rel for f in files if f.rel not in present]
        if job.missing:
            job.status = "failed"
            job.message = f"{len(job.missing)} of {job.files} file(s) are NOT in Immich"
        else:
            job.status = "success"
            job.message = f"All {job.files} file(s) confirmed in Immich"

    def _hash_all(self, job: Job, files: list[MediaFile]) -> dict[str, str]:
        if self._hash_cache_card != job.card:
            self._hash_cache, self._hash_cache_card = {}, job.card
        out = {}
        for f in files:
            key = (str(f.path), f.size, f.mtime)
            digest = self._hash_cache.get(key)
            if digest is None:
                h = hashlib.sha1()
                with open(f.path, "rb") as fh:
                    while block := fh.read(HASH_BLOCK):
                        h.update(block)
                        job.hashed_bytes += len(block)
                digest = self._hash_cache[key] = h.hexdigest()
            else:
                job.hashed_bytes += f.size
            out[f.rel] = digest
        return out

    def _upload(self, job: Job, root: Path, job_dir: Path) -> None:
        paths = [str(root / d) for d in MEDIA_DIRS if (root / d).is_dir()]
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
            *paths,
        ]
        env = {**os.environ, "IMMICH_GO_UPLOAD_SERVER": self.immich_url, "IMMICH_GO_UPLOAD_API_KEY": self.api_key}
        tail: collections.deque[str] = collections.deque(maxlen=60)
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, errors="replace")
        assert proc.stdout
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail.append(line)
                job.uploader_output = list(tail)
        job.uploader_exit = proc.wait()
        _truncate_head(job_dir / "immich-go.log", LOG_TAIL_BYTES)

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
