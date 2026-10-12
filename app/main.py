"""Photo importer web app. Idle = blocked waiting for HTTP requests; no timers, no polling."""
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from immich import Immich, ImmichError
from importer import Importer

IMMICH_URL = os.environ["IMMICH_URL"].rstrip("/")
IMMICH_API_KEY = os.environ["IMMICH_API_KEY"]
# Where your browser reaches Immich, for "Open in Immich" links (if different from IMMICH_URL).
IMMICH_PUBLIC_URL = os.environ.get("IMMICH_PUBLIC_URL") or IMMICH_URL
CARDS_DIR = Path(os.environ.get("CARDS_DIR", "/cards"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "50"))
UPLOAD_CONCURRENCY = int(os.environ.get("UPLOAD_CONCURRENCY", "2"))
IMMICH_GO = os.environ.get("IMMICH_GO", "immich-go")

# Plex shares the NUC: run everything (including immich-go, which inherits this) at low
# CPU and I/O priority.
os.nice(10)
if shutil.which("ionice"):
    subprocess.run(["ionice", "-c2", "-n7", "-p", str(os.getpid())], check=False)

immich = Immich(IMMICH_URL, IMMICH_API_KEY)
importer = Importer(CARDS_DIR, DATA_DIR, immich, IMMICH_URL, IMMICH_API_KEY, IMMICH_PUBLIC_URL,
                    HISTORY_LIMIT, UPLOAD_CONCURRENCY, IMMICH_GO)
app = FastAPI(docs_url=None, redoc_url=None)
STATIC = Path(__file__).parent / "static"


class ImportRequest(BaseModel):
    card: str
    sessions: list[str] | None = None  # dates (YYYY-MM-DD); None = whole card
    album: str = ""
    tags: list[str] = []
    reread: bool = False  # ignore remembered fingerprints and re-read every file


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/overview")
def overview():
    """Everything the page needs on load. Touches Immich and the card once per call."""
    try:
        immich_status = {"ok": True, "user": immich.whoami(), "url": IMMICH_PUBLIC_URL}
    except ImmichError as e:
        immich_status = {"ok": False, "error": str(e), "url": IMMICH_PUBLIC_URL}
    cards = importer.cards()
    albums = []
    if immich_status["ok"] and any(not c["busy"] for c in cards):
        try:
            albums = immich.album_names()
        except ImmichError:
            pass
    return {
        "immich": immich_status,
        "cards": cards,
        "albums": albums,
        "job": importer.current(),
        "history": importer.history(),
    }


@app.get("/api/cards/present")
def cards_present():
    """Which cards are mounted: a directory listing, nothing more. The page asks this every
    few seconds only while it's open and visible, so a newly inserted card shows up."""
    return importer.present_cards()


@app.get("/api/thumb")
def thumb(k: str):
    data = importer.thumb(k)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/job")
def job():
    """Cheap progress read for the page to refresh while an import is running."""
    return importer.current()


@app.post("/api/import")
def start_import(req: ImportRequest):
    try:
        return importer.start(req.model_dump())
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.post("/api/cancel")
def cancel():
    if not importer.cancel():
        raise HTTPException(409, "No import is running")
    return {"ok": True}
