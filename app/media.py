"""What's on a card: media files, capture times, shots (RAW+JPEG pairs), sessions (days)."""
import os
import re
import struct
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ") or "America/Vancouver")

# What counts as a photo/video on a Sony card. Also passed to immich-go as
# --include-extensions so "what we verify" == "what gets uploaded".
MEDIA_EXTS = {".arw", ".jpg", ".jpeg", ".hif", ".heif", ".heic", ".dng", ".mp4", ".mov"}
# DCIM holds stills; Sony puts video in PRIVATE/M4ROOT/CLIP (its THMBNL/SUB siblings are
# thumbnails/proxies and must not be uploaded).
MEDIA_DIRS = ("DCIM", "PRIVATE/M4ROOT/CLIP")
# Which file of a shot to read the capture time from, best first.
DATE_SOURCE_ORDER = (".jpg", ".jpeg", ".arw", ".dng", ".hif", ".heif", ".heic", ".mp4", ".mov")


@dataclass
class MediaFile:
    rel: str
    path: Path
    size: int
    mtime_ns: int
    sha1: str | None = None  # filled from the fingerprint store or by hashing


@dataclass
class Shot:
    key: str  # "DCIM/100MSDCF/GSH07714" — RAW+JPEG of one exposure share it
    files: list[MediaFile] = field(default_factory=list)
    taken: datetime | None = None  # camera-local wall time

    @property
    def session(self) -> str:
        return self.taken.date().isoformat()


def scan_card(root: Path) -> list[MediaFile]:
    """Stat-only listing of media files under MEDIA_DIRS."""
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
                files.append(MediaFile(str(p.relative_to(root)), p, st.st_size, st.st_mtime_ns))
    files.sort(key=lambda f: f.rel)
    return files


def group_shots(files: list[MediaFile]) -> list[Shot]:
    shots: dict[str, Shot] = {}
    for f in files:
        key = str(Path(f.rel).with_suffix("")).upper()
        shots.setdefault(key, Shot(key)).files.append(f)
    return list(shots.values())


def date_source(shot: Shot) -> MediaFile:
    rank = {e: i for i, e in enumerate(DATE_SOURCE_ORDER)}
    return min(shot.files, key=lambda f: rank.get(Path(f.rel).suffix.lower(), 99))


def capture_time(f: MediaFile) -> datetime:
    """Camera-local capture time: EXIF for stills, Sony's XML for clips, else file mtime."""
    ext = f.path.suffix.lower()
    try:
        if ext in (".jpg", ".jpeg"):
            t = _jpeg_datetime(f.path)
        elif ext in (".arw", ".dng"):
            with open(f.path, "rb") as fh:
                t = _tiff_datetime(fh, 0)
        elif ext in (".mp4", ".mov"):
            t = _sony_clip_datetime(f.path)
        else:
            t = None
    except (OSError, ValueError, struct.error):
        t = None
    return t or datetime.fromtimestamp(f.mtime_ns / 1e9, TZ).replace(tzinfo=None)


def summarize_sessions(shots: list[Shot], present: set[str]) -> list[dict]:
    """Per-day summary. A shot is 'in Immich' only if every file is fingerprinted and present."""
    by_day: dict[str, list[Shot]] = defaultdict(list)
    for s in shots:
        by_day[s.session].append(s)
    out = []
    for day, ss in sorted(by_day.items()):
        in_immich = new = unknown = 0
        for s in ss:
            if all(f.rel in present for f in s.files):
                in_immich += 1
            elif any(f.sha1 and f.rel not in present for f in s.files):
                new += 1
            else:
                unknown += 1
        times = [s.taken for s in ss]
        out.append({
            "date": day,
            "shots": len(ss),
            "files": sum(len(s.files) for s in ss),
            "bytes": sum(f.size for s in ss for f in s.files),
            "first": min(times).isoformat(timespec="minutes"),
            "last": max(times).isoformat(timespec="minutes"),
            "in_immich": in_immich,
            "new": new,
            "unknown": unknown,
        })
    return out


# ---- minimal EXIF readers (only DateTimeOriginal; reads a few KB per file) ----

_EXIF_FMT = "%Y:%m:%d %H:%M:%S"


def _jpeg_datetime(path: Path) -> datetime | None:
    with open(path, "rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            return None
        while True:
            marker = fh.read(4)
            if len(marker) < 4 or marker[0] != 0xFF or marker[1] == 0xDA:
                return None
            length = struct.unpack(">H", marker[2:])[0]
            start = fh.tell()
            if marker[1] == 0xE1 and fh.read(6) == b"Exif\x00\x00":
                return _tiff_datetime(fh, start + 6)
            fh.seek(start + length - 2)


def _tiff_datetime(fh, base: int) -> datetime | None:
    fh.seek(base)
    hdr = fh.read(8)
    e = {b"II": "<", b"MM": ">"}.get(hdr[:2])
    if not e:
        return None

    def ifd(offset: int) -> dict[int, bytes]:
        fh.seek(base + offset)
        (n,) = struct.unpack(e + "H", fh.read(2))
        raw = fh.read(12 * n)
        return {struct.unpack(e + "H", raw[i:i + 2])[0]: raw[i:i + 12] for i in range(0, len(raw) - 11, 12)}

    def ascii_value(entry: bytes) -> str:
        _, _, count = struct.unpack(e + "HHI", entry[:8])
        if count <= 4:
            data = entry[8:8 + count]
        else:
            fh.seek(base + struct.unpack(e + "I", entry[8:12])[0])
            data = fh.read(count)
        return data.split(b"\x00")[0].decode("ascii", "replace").strip()

    ifd0 = ifd(struct.unpack(e + "I", hdr[4:8])[0])
    candidates = []
    if 0x8769 in ifd0:  # Exif sub-IFD
        exif = ifd(struct.unpack(e + "I", ifd0[0x8769][8:12])[0])
        if 0x9003 in exif:  # DateTimeOriginal
            candidates.append(exif[0x9003])
    if 0x0132 in ifd0:  # DateTime
        candidates.append(ifd0[0x0132])
    for c in candidates:
        try:
            return datetime.strptime(ascii_value(c), _EXIF_FMT)
        except ValueError:
            continue
    return None


_CLIP_DATE = re.compile(rb'CreationDate\s+value="([^"]+)"')


def _sony_clip_datetime(path: Path) -> datetime | None:
    """Sony writes C0001.MP4 + C0001M01.XML with <CreationDate value="2026-10-04T14:22:31-07:00"/>."""
    for xml in sorted(path.parent.glob(path.stem + "M*.XML")) + sorted(path.parent.glob(path.stem + "M*.xml")):
        m = _CLIP_DATE.search(xml.read_bytes()[:65536])
        if m:
            return datetime.fromisoformat(m.group(1).decode()).replace(tzinfo=None)
    return None
