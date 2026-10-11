"""Photo importer web app. Idle = blocked waiting for HTTP requests; no timers, no polling."""
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from immich import Immich, ImmichError
from importer import Importer, find_cards

IMMICH_URL = os.environ["IMMICH_URL"].rstrip("/")
IMMICH_API_KEY = os.environ["IMMICH_API_KEY"]
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
importer = Importer(CARDS_DIR, DATA_DIR, immich, IMMICH_URL, IMMICH_API_KEY,
                    HISTORY_LIMIT, UPLOAD_CONCURRENCY, IMMICH_GO)
app = FastAPI(docs_url=None, redoc_url=None)
STATIC = Path(__file__).parent / "static"


class ImportRequest(BaseModel):
    card: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/overview")
def overview():
    """Everything the page needs on load. Touches Immich and the card once per call."""
    try:
        immich_status = {"ok": True, "user": immich.whoami(), "url": IMMICH_URL}
    except ImmichError as e:
        immich_status = {"ok": False, "error": str(e), "url": IMMICH_URL}
    return {
        "immich": immich_status,
        "cards": find_cards(CARDS_DIR),
        "job": importer.current(),
        "history": importer.history(),
    }


@app.get("/api/job")
def job():
    """Cheap progress read for the page to refresh while an import is running."""
    return importer.current()


@app.post("/api/import")
def start_import(req: ImportRequest):
    try:
        return importer.start(req.card)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/imports/{job_id}")
def get_record(job_id: str):
    rec = importer.record(job_id)
    if rec is None:
        raise HTTPException(404, "No such import")
    return rec
