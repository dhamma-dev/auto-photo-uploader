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
        key = shot_key(f.rel)
        shots.setdefault(key, Shot(key)).files.append(f)
    return list(shots.values())


def date_source(shot: Shot) -> MediaFile:
    rank = {e: i for i, e in enumerate(DATE_SOURCE_ORDER)}
    return min(shot.files, key=lambda f: rank.get(Path(f.rel).suffix.lower(), 99))


def read_header(f: MediaFile) -> tuple[datetime, bytes | None]:
    """Camera-local capture time (EXIF for stills, Sony's XML for clips, else file mtime)
    and the small preview JPEG the camera embeds in stills. Reads a few KB per file."""
    ext = f.path.suffix.lower()
    t, thumb = None, None
    try:
        if ext in (".jpg", ".jpeg"):
            t, thumb = _jpeg_info(f.path)
        elif ext in (".arw", ".dng"):
            with open(f.path, "rb") as fh:
                t, thumb = _tiff_info(fh, 0)
        elif ext in (".mp4", ".mov"):
            t = _sony_clip_datetime(f.path)
    except (OSError, ValueError, struct.error):
        pass
    return t or datetime.fromtimestamp(f.mtime_ns / 1e9, TZ).replace(tzinfo=None), thumb


def shot_key(rel: str) -> str:
    return str(Path(rel).with_suffix("")).upper()


def sample(items: list, n: int) -> list:
    """Up to n items spread evenly across the list."""
    if len(items) <= n:
        return list(items)
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def summarize_sessions(shots: list[Shot], present: set[str], has_thumb: set[str]) -> list[dict]:
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
        ordered = sorted(ss, key=lambda s: s.taken)
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
            "videos": sum(1 for s in ss if all(Path(f.rel).suffix.lower() in (".mp4", ".mov") for f in s.files)),
            "thumbs": sample([s.key for s in ordered if s.key in has_thumb], 12),
        })
    return out


# ---- minimal EXIF readers (only DateTimeOriginal; reads a few KB per file) ----

_EXIF_FMT = "%Y:%m:%d %H:%M:%S"


def _jpeg_info(path: Path) -> tuple[datetime | None, bytes | None]:
    with open(path, "rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            return None, None
        while True:
            marker = fh.read(4)
            if len(marker) < 4 or marker[0] != 0xFF or marker[1] == 0xDA:
                return None, None
            length = struct.unpack(">H", marker[2:])[0]
            start = fh.tell()
            if marker[1] == 0xE1 and fh.read(6) == b"Exif\x00\x00":
                return _tiff_info(fh, start + 6)
            fh.seek(start + length - 2)


MAX_THUMB = 128 * 1024


def _tiff_info(fh, base: int) -> tuple[datetime | None, bytes | None]:
    """DateTimeOriginal and the embedded JPEG thumbnail (IFD0/IFD1 tags 0x0201/0x0202)."""
    fh.seek(base)
    hdr = fh.read(8)
    e = {b"II": "<", b"MM": ">"}.get(hdr[:2])
    if not e:
        return None, None

    def ifd(offset: int) -> tuple[dict[int, bytes], int]:
        fh.seek(base + offset)
        (n,) = struct.unpack(e + "H", fh.read(2))
        raw = fh.read(12 * n + 4)
        entries = {struct.unpack(e + "H", raw[i:i + 2])[0]: raw[i:i + 12] for i in range(0, 12 * n, 12)}
        nxt = struct.unpack(e + "I", raw[12 * n:12 * n + 4])[0] if len(raw) >= 12 * n + 4 else 0
        return entries, nxt

    def number(entry: bytes) -> int:
        typ = struct.unpack(e + "H", entry[2:4])[0]
        return struct.unpack(e + "H", entry[8:10])[0] if typ == 3 else struct.unpack(e + "I", entry[8:12])[0]

    def ascii_value(entry: bytes) -> str:
        _, _, count = struct.unpack(e + "HHI", entry[:8])
        if count <= 4:
            data = entry[8:8 + count]
        else:
            fh.seek(base + struct.unpack(e + "I", entry[8:12])[0])
            data = fh.read(count)
        return data.split(b"\x00")[0].decode("ascii", "replace").strip()

    ifd0, next_off = ifd(struct.unpack(e + "I", hdr[4:8])[0])
    ifds = [ifd0]
    if next_off:
        ifds.append(ifd(next_off)[0])

    candidates = []
    if 0x8769 in ifd0:  # Exif sub-IFD
        exif, _ = ifd(number(ifd0[0x8769]))
        if 0x9003 in exif:  # DateTimeOriginal
            candidates.append(exif[0x9003])
    if 0x0132 in ifd0:  # DateTime
        candidates.append(ifd0[0x0132])
    taken = None
    for c in candidates:
        try:
            taken = datetime.strptime(ascii_value(c), _EXIF_FMT)
            break
        except ValueError:
            continue

    thumb = None
    for d in reversed(ifds):  # IFD1 is the usual home of the thumbnail
        if 0x0201 in d and 0x0202 in d:
            off, length = number(d[0x0201]), number(d[0x0202])
            if 0 < length <= MAX_THUMB:
                fh.seek(base + off)
                data = fh.read(length)
                if data[:2] == b"\xff\xd8":
                    thumb = data
                    break
    return taken, thumb


_CLIP_DATE = re.compile(rb'CreationDate\s+value="([^"]+)"')


def _sony_clip_datetime(path: Path) -> datetime | None:
    """Sony writes C0001.MP4 + C0001M01.XML with <CreationDate value="2026-10-04T14:22:31-07:00"/>."""
    for xml in sorted(path.parent.glob(path.stem + "M*.XML")) + sorted(path.parent.glob(path.stem + "M*.xml")):
        m = _CLIP_DATE.search(xml.read_bytes()[:65536])
        if m:
            return datetime.fromisoformat(m.group(1).decode()).replace(tzinfo=None)
    return None
