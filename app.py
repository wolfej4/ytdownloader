"""
RT Grabber — a small local web UI around yt-dlp for archiving videos
(RTArchive "IA Link" / archive.org / YouTube) into a Jellyfin-friendly
folder layout with embedded + sidecar thumbnails.

Run:  python app.py    then open  http://127.0.0.1:8765
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent

# CONFIG_DIR lets the container persist config.json on a mounted volume (/config).
# Defaults to the app folder for native (Windows) use.
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(APP_DIR)))
CONFIG_PATH = CONFIG_DIR / "config.json"

# Default download folder. In Docker this is set to /downloads (a mounted volume);
# natively it falls back to a folder in the user's Videos directory.
DEFAULT_OUTPUT = os.environ.get("OUTPUT_DIR") or str(
    Path(os.path.expanduser("~")) / "Videos" / "RoosterTeeth"
)

YT_DLP = os.environ.get("YT_DLP_BIN", "yt-dlp")
SENTINEL = "@@PROG@@"

PROGRESS_TEMPLATE = (
    "download:" + SENTINEL
    + "%(progress._percent_str)s|%(progress._speed_str)s|"
    + "%(progress._eta_str)s|%(info.title)s"
)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"output_dir": DEFAULT_OUTPUT}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


config = load_config()

# --------------------------------------------------------------------------
# SMB
# --------------------------------------------------------------------------
SMB_MOUNT_POINT = "/mnt/smb"
_smb_connected = False
_smb_lock = threading.Lock()


def _is_mounted() -> bool:
    try:
        return subprocess.run(
            ["mountpoint", "-q", SMB_MOUNT_POINT], capture_output=True
        ).returncode == 0
    except FileNotFoundError:
        return False


def _do_mount(host: str, share: str, username: str, password: str, domain: str) -> None:
    Path(SMB_MOUNT_POINT).mkdir(parents=True, exist_ok=True)
    subprocess.run(["modprobe", "cifs"], capture_output=True)  # load kernel module if not already present
    opts = f"username={username},password={password},uid=99,gid=100,file_mode=0664,dir_mode=0775,vers=3.0"
    if domain:
        opts += f",domain={domain}"
    r = subprocess.run(
        ["mount.cifs", f"//{host}/{share}", SMB_MOUNT_POINT, "-o", opts],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or f"mount.cifs exited {r.returncode}")
    if not _is_mounted():
        raise RuntimeError("mount.cifs exited 0 but share is not mounted — check host/share name")


def _do_unmount() -> None:
    r = subprocess.run(["umount", SMB_MOUNT_POINT], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"umount exited {r.returncode}")


def _try_auto_mount() -> None:
    global _smb_connected
    smb = config.get("smb")
    if not smb or not smb.get("host"):
        return
    try:
        if not _is_mounted():
            _do_mount(smb["host"], smb["share"], smb["username"], smb["password"], smb.get("domain", ""))
        with _smb_lock:
            _smb_connected = True
    except Exception as exc:
        print(f"[SMB] Auto-mount failed: {exc}", file=sys.stderr)


_try_auto_mount()

# --------------------------------------------------------------------------
# Job store + worker queue
# --------------------------------------------------------------------------
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
work_queue: "queue.Queue[str]" = queue.Queue()

ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")


def _set(job_id: str, **fields) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def run_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return

    out_dir = config["output_dir"]
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    archive_db = str(Path(out_dir) / ".downloaded.txt")

    cmd = [
        YT_DLP,
        "--newline",
        "--progress-template", PROGRESS_TEMPLATE,
        "--download-archive", archive_db,
        "--ignore-errors",
        "--no-overwrites",
        "--embed-metadata",
        "--embed-thumbnail",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "--merge-output-format", "mkv",
        "-o", "%(title).200B [%(id)s]/%(title).200B [%(id)s].%(ext)s",
        "-o", "thumbnail:%(title).200B [%(id)s]/poster.jpg",
        "-P", out_dir,
        job["url"],
    ]

    _set(job_id, status="running", started=time.time())
    tail: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        _set(job_id, status="error",
             error=f"Could not find '{YT_DLP}'. Install it and make sure it's on PATH.")
        return

    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.rstrip("\n")
        if not line:
            continue
        tail.append(line)
        del tail[:-12]  # keep last 12 lines for error reporting

        if line.startswith(SENTINEL):
            parts = line[len(SENTINEL):].split("|")
            pct = (parts[0] if len(parts) > 0 else "").strip()
            speed = (parts[1] if len(parts) > 1 else "").strip()
            eta = (parts[2] if len(parts) > 2 else "").strip()
            title = (parts[3] if len(parts) > 3 else "").strip()
            num = None
            try:
                num = float(pct.replace("%", ""))
            except ValueError:
                pass
            _set(job_id, percent=num, percent_str=pct, speed=speed,
                 eta=eta, title=title or job.get("title"))
        else:
            m = ITEM_RE.search(line)
            if m:
                _set(job_id, current=int(m.group(1)), total=int(m.group(2)))

    proc.wait()
    if proc.returncode == 0:
        _set(job_id, status="done", percent=100.0, percent_str="100%",
             finished=time.time())
    else:
        _set(job_id, status="error", finished=time.time(),
             error="\n".join(tail[-6:]) or f"yt-dlp exited with code {proc.returncode}")


def worker() -> None:
    while True:
        job_id = work_queue.get()
        try:
            run_job(job_id)
        except Exception as exc:  # noqa: BLE001
            _set(job_id, status="error", error=str(exc))
        finally:
            work_queue.task_done()


threading.Thread(target=worker, daemon=True).start()

# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
app = FastAPI(title="RT Grabber")


class DownloadRequest(BaseModel):
    urls: list[str]


class ConfigRequest(BaseModel):
    output_dir: str


@app.get("/api/config")
def get_config() -> dict:
    with _smb_lock:
        connected = _smb_connected
    smb = config.get("smb", {})
    return {
        "output_dir": config["output_dir"],
        "yt_dlp_ok": shutil.which(YT_DLP) is not None,
        "ffmpeg_ok": shutil.which("ffmpeg") is not None,
        "smb_connected": connected,
        "smb": {
            "host": smb.get("host", ""),
            "share": smb.get("share", ""),
            "username": smb.get("username", ""),
            "password": smb.get("password", ""),
            "domain": smb.get("domain", ""),
            "subdir": smb.get("subdir", ""),
        },
    }


@app.post("/api/config")
def set_config(req: ConfigRequest) -> dict:
    path = req.output_dir.strip()
    if not path:
        raise HTTPException(400, "Output folder cannot be empty.")
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"Can't use that folder: {exc}")
    config["output_dir"] = path
    save_config(config)
    return {"output_dir": path}


@app.post("/api/download")
def submit(req: DownloadRequest) -> dict:
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "No URLs provided.")
    created = []
    for url in urls:
        job_id = uuid.uuid4().hex[:8]
        with jobs_lock:
            jobs[job_id] = {
                "id": job_id, "url": url, "status": "queued",
                "percent": 0.0, "percent_str": "", "speed": "", "eta": "",
                "title": "", "current": None, "total": None, "error": "",
                "queued_at": time.time(),
            }
        work_queue.put(job_id)
        created.append(job_id)
    return {"created": created}


@app.get("/api/jobs")
def list_jobs() -> dict:
    with jobs_lock:
        items = sorted(jobs.values(), key=lambda j: j["queued_at"], reverse=True)
    return {"jobs": items}


@app.post("/api/jobs/clear")
def clear_finished() -> dict:
    with jobs_lock:
        for jid in [j["id"] for j in jobs.values() if j["status"] in ("done", "error")]:
            del jobs[jid]
    return {"ok": True}


class SmbConnectRequest(BaseModel):
    host: str
    share: str
    username: str
    password: str
    domain: str = ""
    subdir: str = ""


@app.post("/api/smb/connect")
def smb_connect(req: SmbConnectRequest) -> dict:
    global _smb_connected
    host = req.host.strip()
    share = req.share.strip()
    if not host or not share:
        raise HTTPException(400, "Host and share are required.")
    if _is_mounted():
        try:
            _do_unmount()
        except Exception:
            pass
    try:
        _do_mount(host, share, req.username, req.password, req.domain)
    except RuntimeError as exc:
        raise HTTPException(400, f"Mount failed: {exc}")
    with _smb_lock:
        _smb_connected = True
    subdir = req.subdir.strip().lstrip("/")
    mount_path = SMB_MOUNT_POINT if not subdir else str(Path(SMB_MOUNT_POINT) / subdir)
    config["smb"] = {
        "host": host, "share": share, "username": req.username,
        "password": req.password, "domain": req.domain, "subdir": subdir,
    }
    config["output_dir"] = mount_path
    save_config(config)
    return {"connected": True, "output_dir": mount_path}


@app.post("/api/smb/disconnect")
def smb_disconnect() -> dict:
    global _smb_connected
    if _is_mounted():
        try:
            _do_unmount()
        except RuntimeError as exc:
            raise HTTPException(400, f"Unmount failed: {exc}")
    with _smb_lock:
        _smb_connected = False
    config.pop("smb", None)
    config["output_dir"] = DEFAULT_OUTPUT
    save_config(config)
    return {"connected": False, "output_dir": config["output_dir"]}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(APP_DIR / "static" / "index.html"))


app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8765"))
    print(f"RT Grabber running at  http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
