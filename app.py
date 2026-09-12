"""
RT Grabber — a small local web UI around yt-dlp for archiving videos
(RTArchive "IA Link" / archive.org / YouTube) into a Jellyfin-friendly
folder layout with embedded + sidecar thumbnails.

Run:  python app.py    then open  http://127.0.0.1:8765
"""

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests
import smbclient

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
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
SENTINEL = "@@PROG@@"

# Plain stdout logging so `docker logs` / Portainer's log viewer show it.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rt-grabber")
thumb_log = logging.getLogger("rt-grabber.thumbnails")

PROGRESS_TEMPLATE = (
    "download:" + SENTINEL
    + "%(progress._percent_str)s|%(progress._speed_str)s|"
    + "%(progress._eta_str)s|%(info.title)s|%(info.thumbnail)s"
)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"output_dir": DEFAULT_OUTPUT, "max_workers": 3}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


config = load_config()
if "max_workers" not in config:
    config["max_workers"] = 3

# --------------------------------------------------------------------------
# SMB  (pure-Python SMB2/3 via smbprotocol — no kernel mount needed)
# --------------------------------------------------------------------------
SMB_STAGE_DIR = Path("/tmp/rtgrabber-stage")
_smb_state: dict = {}
_smb_connected = False
_smb_lock = threading.Lock()


def _smb_unc(*parts: str) -> str:
    """Return a UNC path rooted at the configured share + optional subdir."""
    segs = [f"\\\\{_smb_state['host']}\\{_smb_state['share']}"]
    subdir = _smb_state.get("subdir", "").strip("/\\")
    if subdir:
        segs.extend(subdir.replace("/", "\\").split("\\"))
    for p in parts:
        p = p.strip("/\\").replace("/", "\\")
        if p and p != ".":
            segs.extend(p.split("\\"))
    return "\\".join(segs)


def _smb_open_session(host: str, share: str, username: str, password: str, domain: str) -> None:
    smbclient.reset_connection_cache()
    kw: dict = {"username": username, "password": password}
    if domain:
        kw["domain"] = domain
    smbclient.register_session(host, **kw)
    smbclient.stat(f"\\\\{host}\\{share}")  # raises if share is unreachable / auth fails


def _copy_to_smb(local_dir: Path) -> None:
    smbclient.makedirs(_smb_unc(), exist_ok=True)
    for src in local_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(local_dir)
        if rel.parent != Path("."):
            smbclient.makedirs(_smb_unc(str(rel.parent)), exist_ok=True)
        with src.open("rb") as fsrc, smbclient.open_file(_smb_unc(str(rel)), mode="wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)


def _try_auto_connect() -> None:
    global _smb_connected, _smb_state
    smb = config.get("smb")
    if not smb or not smb.get("host"):
        return
    try:
        _smb_open_session(smb["host"], smb["share"], smb["username"], smb["password"], smb.get("domain", ""))
        with _smb_lock:
            _smb_state = dict(smb)
            _smb_connected = True
    except Exception as exc:
        print(f"[SMB] Auto-connect failed: {exc}", file=sys.stderr)


_try_auto_connect()

# --------------------------------------------------------------------------
# Job store + worker queue
# --------------------------------------------------------------------------
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
work_queue: "queue.Queue[str]" = queue.Queue()

# Tracks running subprocesses so we can cancel them
_job_procs: dict[str, subprocess.Popen] = {}
_job_procs_lock = threading.Lock()

# Dynamic concurrency control
_active = 0
_active_cond = threading.Condition()

ITEM_RE = re.compile(r"Downloading item (\d+) of (\d+)")

_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_pathpart(name: str) -> str:
    cleaned = _INVALID_PATH_CHARS.sub("", name).strip().rstrip(". ")
    return cleaned[:150] or "Untitled"


def _build_output_template(job: dict) -> str:
    prefix = ""
    season, episode = job.get("season"), job.get("episode")
    if season is not None and episode is not None:
        prefix = f"S{int(season):02d}E{int(episode):02d} - "
    filename = f"{prefix}%(title).200B [%(id)s].%(ext)s"
    show = job.get("show")
    if show:
        return f"{_sanitize_pathpart(show)}/{filename}"
    return filename


def _set(job_id: str, **fields) -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def run_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return

    # Check if cancelled before we even start
    if job.get("status") == "cancelled":
        return

    with _smb_lock:
        use_smb = _smb_connected

    if use_smb:
        stage_dir: Path | None = SMB_STAGE_DIR / job_id
        stage_dir.mkdir(parents=True, exist_ok=True)
        out_dir = str(stage_dir)
    else:
        stage_dir = None
        out_dir = config["output_dir"]
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    cmd = [
        YT_DLP,
        "--newline",
        "--progress-template", PROGRESS_TEMPLATE,
        "--ignore-errors",
        "--embed-thumbnail",
        "--convert-thumbnails", "jpg",
        "--embed-metadata",
        "--remux-video", "mkv",
        "--merge-output-format", "mkv",
        "-o", _build_output_template(job),
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

    with _job_procs_lock:
        _job_procs[job_id] = proc

    try:
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
                thumb = (parts[4] if len(parts) > 4 else "").strip()
                num = None
                try:
                    num = float(pct.replace("%", ""))
                except ValueError:
                    pass
                updates: dict = dict(percent=num, percent_str=pct, speed=speed,
                                     eta=eta, title=title or job.get("title"))
                if thumb and thumb != "NA":
                    updates["thumbnail_url"] = thumb
                _set(job_id, **updates)
            else:
                m = ITEM_RE.search(line)
                if m:
                    _set(job_id, current=int(m.group(1)), total=int(m.group(2)))
    finally:
        with _job_procs_lock:
            _job_procs.pop(job_id, None)

    proc.wait()

    # Check if cancelled mid-run
    with jobs_lock:
        current_status = jobs.get(job_id, {}).get("status")
    if current_status == "cancelled":
        if stage_dir:
            shutil.rmtree(stage_dir, ignore_errors=True)
        return

    if proc.returncode == 0:
        if use_smb and stage_dir:
            _set(job_id, status="uploading", percent=100.0, percent_str="100%")
            try:
                _copy_to_smb(stage_dir)
                _set(job_id, status="done", finished=time.time())
            except Exception as exc:
                _set(job_id, status="error", finished=time.time(),
                     error=f"SMB upload failed: {exc}")
            finally:
                shutil.rmtree(stage_dir, ignore_errors=True)
        else:
            _set(job_id, status="done", percent=100.0, percent_str="100%",
                 finished=time.time())
    else:
        _set(job_id, status="error", finished=time.time(),
             error="\n".join(tail[-6:]) or f"yt-dlp exited with code {proc.returncode}")
        if stage_dir:
            shutil.rmtree(stage_dir, ignore_errors=True)


def worker() -> None:
    global _active
    while True:
        job_id = work_queue.get()
        # Wait until we're under the concurrency limit
        with _active_cond:
            while _active >= config.get("max_workers", 3):
                _active_cond.wait()
            _active += 1
        try:
            run_job(job_id)
        except Exception as exc:  # noqa: BLE001
            _set(job_id, status="error", error=str(exc))
        finally:
            with _active_cond:
                _active -= 1
                _active_cond.notify_all()
            work_queue.task_done()


_POOL_SIZE = 20  # large enough that the concurrency limit is always the bottleneck
for _ in range(_POOL_SIZE):
    threading.Thread(target=worker, daemon=True).start()

# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
app = FastAPI(title="RT Grabber")

# --------------------------------------------------------------------------
# RT Archive browsing (reads the same public Firestore backend the
# rtarchive.org web app itself queries client-side)
# --------------------------------------------------------------------------
FIRESTORE_URL = "https://firestore.googleapis.com/v1/projects/rt-archive/databases/(default)/documents"

_shows_cache: dict = {"data": None, "ts": 0.0}
_shows_lock = threading.Lock()
SHOWS_TTL = 3600.0


def _fs_val(v: dict):
    if "stringValue" in v:
        return v["stringValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return v["doubleValue"]
    if "booleanValue" in v:
        return v["booleanValue"]
    if "nullValue" in v:
        return None
    if "arrayValue" in v:
        return [_fs_val(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: _fs_val(x) for k, x in v["mapValue"].get("fields", {}).items()}
    if "referenceValue" in v:
        return v["referenceValue"]
    return None


def _fs_doc(doc: dict) -> dict:
    out = {k: _fs_val(v) for k, v in doc.get("fields", {}).items()}
    out["id"] = doc["name"].rsplit("/", 1)[-1]
    return out


def _fetch_all_shows() -> list[dict]:
    shows: list[dict] = []
    page_token = None
    while True:
        params = {"pageSize": 300}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(f"{FIRESTORE_URL}/shows", params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        for doc in data.get("documents", []):
            shows.append(_fs_doc(doc))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return shows


def get_shows() -> list[dict]:
    with _shows_lock:
        stale = time.time() - _shows_cache["ts"] > SHOWS_TTL
        if _shows_cache["data"] is None or stale:
            try:
                _shows_cache["data"] = _fetch_all_shows()
                _shows_cache["ts"] = time.time()
            except Exception:
                if _shows_cache["data"] is None:
                    raise
        return _shows_cache["data"]


def _show_thumbnail(show: dict, size: str = "thumb") -> str:
    try:
        images = show["rt_metadata"]["included"]["images"]
    except (KeyError, TypeError):
        return ""
    if not images:
        return ""
    for want in ("title_card", "poster", "hero"):
        for im in images:
            if im.get("attributes", {}).get("image_type") == want:
                attrs = im["attributes"]
                return attrs.get(size) or attrs.get("thumb", "")
    attrs = images[0].get("attributes", {})
    return attrs.get(size) or attrs.get("thumb", "")


def _run_firestore_query(body: dict) -> list[dict]:
    resp = requests.post(f"{FIRESTORE_URL}:runQuery", json=body, timeout=20)
    resp.raise_for_status()
    return [_fs_doc(row["document"]) for row in resp.json() if "document" in row]


def _archive_ia_url(platform: str, external_id: str) -> str:
    return f"https://archive.org/details/{platform}-{external_id}"


@app.get("/api/browse/shows")
def browse_shows(q: str = "") -> dict:
    try:
        shows = get_shows()
    except Exception as exc:
        raise HTTPException(502, f"Couldn't reach RT Archive: {exc}")

    needle = q.strip().lower()
    results = []
    for s in shows:
        title = s.get("title") or s["id"]
        if needle and needle not in title.lower():
            continue
        results.append({
            "id": s["id"],
            "title": title,
            "episode_count": s.get("rt_episode_count", 0),
            "thumbnail": _show_thumbnail(s),
        })
    results.sort(key=lambda r: r["title"].lower())
    return {"shows": results[:200]}


@app.get("/api/browse/shows/{show_id}/episodes")
def browse_episodes(show_id: str) -> dict:
    fallback_title = show_id
    try:
        fallback_title = next(
            (s.get("title") for s in get_shows() if s["id"] == show_id), show_id
        )
    except Exception:
        pass

    body = {
        "structuredQuery": {
            "from": [{"collectionId": "videos"}],
            "where": {"fieldFilter": {
                "field": {"fieldPath": "shows"},
                "op": "ARRAY_CONTAINS",
                "value": {"stringValue": show_id},
            }},
            "limit": 1000,
        }
    }
    try:
        docs = _run_firestore_query(body)
    except Exception as exc:
        raise HTTPException(502, f"Couldn't reach RT Archive: {exc}")

    episodes = [_shape_episode(d, fallback_show=fallback_title) for d in docs]
    episodes.sort(key=lambda e: e["date"])
    return {"episodes": episodes}


def _shape_episode(d: dict, fallback_show: str | None = None) -> dict:
    platform = d.get("platform")
    ai_id = d.get("ai_id") or ""
    own_url = f"https://archive.org/details/{ai_id}" if ai_id else None
    rt_url = own_url if platform == "roosterteeth" else None
    youtube_url = own_url if platform == "youtube" else None

    linked_id = d.get("linked_video_id")
    linked_platform = d.get("linked_video_platform")
    if linked_id and linked_platform:
        linked_url = _archive_ia_url(linked_platform, linked_id)
        if linked_platform == "roosterteeth" and not rt_url:
            rt_url = linked_url
        elif linked_platform == "youtube" and not youtube_url:
            youtube_url = linked_url

    attrs = (d.get("rt_metadata") or {}).get("attributes") or {}
    shows = d.get("shows") or []
    show_slug = shows[0] if shows else None

    return {
        "id": d["id"],
        "title": d.get("title") or d["id"],
        "date": d.get("sort_date") or d.get("date") or 0,
        "duration": d.get("duration"),
        "rt_url": rt_url,
        "youtube_url": youtube_url,
        "show": attrs.get("show_title") or fallback_show
        or (show_slug.replace("-", " ").title() if show_slug else None),
        "season": attrs.get("season_number"),
        "episode_number": attrs.get("number"),
    }


@app.get("/api/browse/episodes")
def browse_episode_search(q: str = "") -> dict:
    query = q.strip()
    if not query:
        return {"episodes": []}

    # Firestore only supports "starts with" prefix range queries on a field —
    # no substring/full-text search — and string comparison is case-sensitive,
    # so try a few common casings (rtarchive.org titles are a mix of ALL CAPS
    # and Title Case) and merge the results.
    variants = {query, query.upper(), query.title()}
    docs_by_id: dict[str, dict] = {}
    try:
        for variant in variants:
            body = {
                "structuredQuery": {
                    "from": [{"collectionId": "videos"}],
                    "where": {"compositeFilter": {"op": "AND", "filters": [
                        {"fieldFilter": {
                            "field": {"fieldPath": "title"},
                            "op": "GREATER_THAN_OR_EQUAL",
                            "value": {"stringValue": variant},
                        }},
                        {"fieldFilter": {
                            "field": {"fieldPath": "title"},
                            "op": "LESS_THAN",
                            "value": {"stringValue": variant + ""},
                        }},
                    ]}},
                    "limit": 40,
                }
            }
            for d in _run_firestore_query(body):
                docs_by_id[d["id"]] = d
    except Exception as exc:
        raise HTTPException(502, f"Couldn't reach RT Archive: {exc}")

    episodes = []
    for d in docs_by_id.values():
        # Skip a YouTube-side doc when it's just the mirror of an RT-sourced
        # episode — that canonical doc will surface on its own and carries
        # richer metadata (show/season/episode number).
        if d.get("platform") == "youtube" and d.get("linked_video_platform") == "roosterteeth":
            continue
        episodes.append(_shape_episode(d))

    episodes.sort(key=lambda e: (e["title"] or "").lower())
    return {"episodes": episodes[:100]}


# --------------------------------------------------------------------------
# Thumbnail backfill scan — walks already-downloaded videos, adding a
# sidecar .jpg (fetched from archive.org) for any that lack one, and a
# folder.jpg (fetched from the rtarchive.org show catalog) per show folder.
# --------------------------------------------------------------------------
VIDEO_EXTS = (".mkv", ".mp4", ".webm", ".mov", ".avi")
_BRACKET_ID_RE = re.compile(r"\[([^\[\]]+)\]\.[A-Za-z0-9]+$")
_IA_ID_RE = re.compile(r"^(roosterteeth|youtube)-")

_thumb_scan_lock = threading.Lock()
_thumb_scan_state: dict = {
    "running": False, "checked": 0, "total": 0,
    "added": 0, "errors": 0, "finished_at": None,
}


def _bracket_id(filename: str) -> str | None:
    m = _BRACKET_ID_RE.search(filename)
    return m.group(1) if m else None


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_VIDEO_FILE_EXTS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpg", ".mpeg")


def _select_ia_thumbnail_file(files: list[dict]) -> dict | None:
    # 1. archive.org's derive process names a video's poster frame after the
    #    uploaded source file itself, just swapped to .jpg — e.g. the
    #    original "2013-12-16_GTA_V_Train_Hopping_[17627].mp4" gets a
    #    matching "...[17627].jpg" alongside it. This is the real convention
    #    and the most reliable match when the original file is identifiable.
    by_name_lower = {f.get("name", "").lower(): f for f in files}
    originals = [
        f for f in files
        if f.get("source") == "original" and f.get("name", "").lower().endswith(_VIDEO_FILE_EXTS)
    ]
    for orig in originals:
        stem = orig["name"].rsplit(".", 1)[0]
        match = by_name_lower.get(f"{stem.lower()}.jpg") or by_name_lower.get(f"{stem.lower()}.jpeg")
        if match:
            return match

    # Exclude the ".thumbs/" directory archive.org auto-generates for every
    # video: dozens of tiny per-frame scrubber captures that happen to have
    # "thumb" in their path but are never the actual poster image.
    candidates = [
        f for f in files
        if f.get("name", "").lower().endswith(_IMAGE_EXTS)
        and ".thumbs/" not in f.get("name", "").lower()
    ]
    if not candidates:
        return None
    # 2. archive.org's own canonical "item image" — reliable when present.
    exact = next((f for f in candidates if f.get("name", "").lower() == "__ia_thumb.jpg"), None)
    if exact:
        return exact
    # 3. Anything IA itself tagged as the item's tile image.
    tagged = [f for f in candidates if "item tile" in (f.get("format") or "").lower()]
    if tagged:
        return max(tagged, key=lambda f: int(f.get("size") or 0))
    # 4. Last resort: the largest remaining image file — matches what's
    #    usually the actual poster/cover art versus a stray small icon.
    return max(candidates, key=lambda f: int(f.get("size") or 0))


def _image_to_jpg(data: bytes, source_name: str) -> bytes | None:
    if source_name.lower().endswith((".jpg", ".jpeg")):
        return data
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"in{Path(source_name).suffix or '.img'}"
        dst = Path(td) / "out.jpg"
        src.write_bytes(data)
        try:
            result = subprocess.run(
                [FFMPEG_BIN, "-y", "-loglevel", "error", "-i", str(src), str(dst)],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            thumb_log.error("ffmpeg binary %r not found on PATH — can't convert %s to jpg", FFMPEG_BIN, source_name)
            return None
        except Exception as exc:
            thumb_log.error("ffmpeg conversion crashed on %s: %s", source_name, exc)
            return None
        if result.returncode != 0:
            thumb_log.warning("ffmpeg exited %s converting %s->jpg: %s",
                               result.returncode, source_name, result.stderr.strip()[-500:])
            return None
        if not dst.exists():
            thumb_log.warning("ffmpeg reported success but produced no output file for %s", source_name)
            return None
        return dst.read_bytes()


def _fetch_ia_thumbnail_jpg(identifier: str) -> bytes | None:
    meta_url = f"https://archive.org/metadata/{identifier}"
    try:
        resp = requests.get(meta_url, timeout=20)
        resp.raise_for_status()
        files = resp.json().get("files", [])
    except Exception as exc:
        thumb_log.warning("metadata fetch failed for %s (%s): %s", identifier, meta_url, exc)
        return None

    chosen = _select_ia_thumbnail_file(files)
    if not chosen:
        sample = [f.get("name") for f in files[:15]]
        thumb_log.warning("no image file found in %s's IA metadata (%d files); sample: %s",
                           identifier, len(files), sample)
        return None
    name = chosen["name"]
    thumb_log.info("using %s (%s bytes) as thumbnail source for %s", name, chosen.get("size"), identifier)

    dl_url = f"https://archive.org/download/{identifier}/{name}"
    try:
        img = requests.get(dl_url, timeout=30)
        img.raise_for_status()
    except Exception as exc:
        thumb_log.warning("thumbnail download failed for %s (%s): %s", identifier, dl_url, exc)
        return None

    jpg = _image_to_jpg(img.content, name)
    if jpg is None:
        thumb_log.warning("image->jpg conversion failed for %s (source %s)", identifier, name)
    return jpg


def _lookup_show_poster(folder_name: str) -> bytes | None:
    try:
        shows = get_shows()
    except Exception as exc:
        thumb_log.warning("couldn't load show catalog to find art for folder %r: %s", folder_name, exc)
        return None
    needle = folder_name.strip().lower()
    match = next((s for s in shows if (s.get("title") or "").strip().lower() == needle), None)
    if not match:
        thumb_log.warning("no rtarchive.org show matches folder name %r — skipping folder.jpg", folder_name)
        return None
    url = _show_thumbnail(match, size="large") or _show_thumbnail(match)
    if not url:
        thumb_log.warning("show %r matched but has no artwork in its catalog entry", folder_name)
        return None
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception as exc:
        thumb_log.warning("show art download failed for %r (%s): %s", folder_name, url, exc)
        return None


def _fpath_exists(kind: str, dirpath: str, filename: str) -> bool:
    if kind == "smb":
        return smbclient.path.exists(f"{dirpath}\\{filename}")
    return (Path(dirpath) / filename).exists()


def _fwrite_bytes(kind: str, dirpath: str, filename: str, data: bytes) -> None:
    if kind == "smb":
        with smbclient.open_file(f"{dirpath}\\{filename}", mode="wb") as fh:
            fh.write(data)
    else:
        (Path(dirpath) / filename).write_bytes(data)


def _iter_video_files():
    """Yield (kind, dirpath, filename, root) for every video in the active storage location."""
    with _smb_lock:
        use_smb = _smb_connected
    if use_smb:
        root = _smb_unc()
        for dirpath, _dirs, files in smbclient.walk(root):
            for fn in files:
                if fn.lower().endswith(VIDEO_EXTS):
                    yield "smb", dirpath, fn, root
    else:
        root = config["output_dir"]
        root_path = Path(root)
        if not root_path.exists():
            return
        for p in root_path.rglob("*"):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                yield "local", str(p.parent), p.name, root


def _show_folder_name(kind: str, dirpath: str, root: str) -> str | None:
    root_norm = root.rstrip("\\/")
    dirpath_norm = dirpath.rstrip("\\/")
    if dirpath_norm == root_norm:
        return None
    sep = "\\" if kind == "smb" else os.sep
    return dirpath_norm.replace("/", sep).rsplit(sep, 1)[-1]


def _run_thumbnail_scan() -> None:
    with _smb_lock:
        use_smb = _smb_connected
    thumb_log.info("scan starting — source=%s output_dir=%s",
                    "smb" if use_smb else "local", config.get("output_dir"))
    with _thumb_scan_lock:
        _thumb_scan_state.update(running=True, checked=0, total=0,
                                  added=0, errors=0, finished_at=None)

    seen_folders: set = set()
    try:
        videos = list(_iter_video_files())
        thumb_log.info("found %d video file(s) to check", len(videos))
        with _thumb_scan_lock:
            _thumb_scan_state["total"] = len(videos)

        for kind, dirpath, filename, root in videos:
            with _thumb_scan_lock:
                _thumb_scan_state["checked"] += 1

            base = filename.rsplit(".", 1)[0]
            jpg_name = f"{base}.jpg"
            vid_id = _bracket_id(filename)

            if not vid_id:
                thumb_log.debug("skip %r — no [id] found in filename", filename)
            elif not _IA_ID_RE.match(vid_id):
                thumb_log.debug("skip %r — id %r doesn't look like an archive.org RT/YouTube id", filename, vid_id)
            elif _fpath_exists(kind, dirpath, jpg_name):
                thumb_log.debug("skip %r — %s already exists", filename, jpg_name)
            else:
                thumb_log.info("fetching thumbnail for %r (id=%s)", filename, vid_id)
                data = _fetch_ia_thumbnail_jpg(vid_id)
                if data:
                    try:
                        _fwrite_bytes(kind, dirpath, jpg_name, data)
                        thumb_log.info("wrote %s/%s (%d bytes)", dirpath, jpg_name, len(data))
                        with _thumb_scan_lock:
                            _thumb_scan_state["added"] += 1
                    except Exception as exc:
                        thumb_log.error("failed writing %s/%s: %s", dirpath, jpg_name, exc)
                        with _thumb_scan_lock:
                            _thumb_scan_state["errors"] += 1
                else:
                    thumb_log.warning("no thumbnail obtained for %r (id=%s) — see warnings above", filename, vid_id)
                    with _thumb_scan_lock:
                        _thumb_scan_state["errors"] += 1

            folder_key = (kind, dirpath)
            if folder_key not in seen_folders:
                seen_folders.add(folder_key)
                show_name = _show_folder_name(kind, dirpath, root)
                if show_name and not _fpath_exists(kind, dirpath, "folder.jpg"):
                    thumb_log.info("fetching show art for folder %r", show_name)
                    poster = _lookup_show_poster(show_name)
                    if poster:
                        try:
                            _fwrite_bytes(kind, dirpath, "folder.jpg", poster)
                            thumb_log.info("wrote %s/folder.jpg (%d bytes)", dirpath, len(poster))
                            with _thumb_scan_lock:
                                _thumb_scan_state["added"] += 1
                        except Exception as exc:
                            thumb_log.error("failed writing %s/folder.jpg: %s", dirpath, exc)
                            with _thumb_scan_lock:
                                _thumb_scan_state["errors"] += 1
                    else:
                        with _thumb_scan_lock:
                            _thumb_scan_state["errors"] += 1
    except Exception:
        thumb_log.exception("thumbnail scan crashed")
    finally:
        with _thumb_scan_lock:
            _thumb_scan_state["running"] = False
            _thumb_scan_state["finished_at"] = time.time()
        thumb_log.info("scan finished — checked=%d added=%d errors=%d",
                        _thumb_scan_state["checked"], _thumb_scan_state["added"], _thumb_scan_state["errors"])


@app.post("/api/thumbnails/scan")
def start_thumbnail_scan() -> dict:
    with _thumb_scan_lock:
        if _thumb_scan_state["running"]:
            raise HTTPException(409, "A scan is already running.")
    threading.Thread(target=_run_thumbnail_scan, daemon=True).start()
    return {"started": True}


@app.get("/api/thumbnails/scan")
def get_thumbnail_scan_status() -> dict:
    with _thumb_scan_lock:
        return dict(_thumb_scan_state)


class DownloadItem(BaseModel):
    url: str
    show: str | None = None
    season: int | None = None
    episode: int | None = None


class DownloadRequest(BaseModel):
    urls: list[str] = []
    items: list[DownloadItem] = []


class ConfigRequest(BaseModel):
    output_dir: str


class WorkersRequest(BaseModel):
    max_workers: int


@app.get("/api/config")
def get_config() -> dict:
    with _smb_lock:
        connected = _smb_connected
    smb = config.get("smb", {})
    return {
        "output_dir": config["output_dir"],
        "max_workers": config.get("max_workers", 3),
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


@app.post("/api/workers")
def set_workers(req: WorkersRequest) -> dict:
    n = max(1, min(20, req.max_workers))
    config["max_workers"] = n
    save_config(config)
    with _active_cond:
        _active_cond.notify_all()
    return {"max_workers": n}


@app.post("/api/download")
def submit(req: DownloadRequest) -> dict:
    items = list(req.items) + [DownloadItem(url=u) for u in req.urls if u.strip()]
    items = [it for it in items if it.url.strip()]
    if not items:
        raise HTTPException(400, "No URLs provided.")
    created = []
    for item in items:
        job_id = uuid.uuid4().hex[:8]
        with jobs_lock:
            jobs[job_id] = {
                "id": job_id, "url": item.url.strip(), "status": "queued",
                "show": item.show, "season": item.season, "episode": item.episode,
                "percent": 0.0, "percent_str": "", "speed": "", "eta": "",
                "title": "", "thumbnail_url": "", "current": None, "total": None,
                "error": "", "queued_at": time.time(),
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
        for jid in [j["id"] for j in jobs.values() if j["status"] in ("done", "error", "cancelled")]:
            del jobs[jid]
    return {"ok": True}


@app.post("/api/jobs/clear/done")
def clear_done() -> dict:
    with jobs_lock:
        for jid in [j["id"] for j in jobs.values() if j["status"] == "done"]:
            del jobs[jid]
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job["status"] in ("done", "error", "cancelled"):
        raise HTTPException(400, "Job is already finished.")
    _set(job_id, status="cancelled", finished=time.time())
    with _job_procs_lock:
        proc = _job_procs.get(job_id)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"cancelled": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict:
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job["status"] not in ("error", "cancelled"):
        raise HTTPException(400, "Only errored or cancelled jobs can be retried.")
    _set(job_id,
         status="queued", percent=0.0, percent_str="", speed="", eta="",
         error="", current=None, total=None, thumbnail_url="", queued_at=time.time())
    work_queue.put(job_id)
    return {"retried": True}


class SmbConnectRequest(BaseModel):
    host: str
    share: str
    username: str
    password: str
    domain: str = ""
    subdir: str = ""


@app.post("/api/smb/connect")
def smb_connect(req: SmbConnectRequest) -> dict:
    global _smb_connected, _smb_state
    host = req.host.strip()
    share = req.share.strip()
    if not host or not share:
        raise HTTPException(400, "Host and share are required.")
    try:
        _smb_open_session(host, share, req.username, req.password, req.domain)
    except Exception as exc:
        raise HTTPException(400, f"Connection failed: {exc}")
    subdir = req.subdir.strip().strip("/\\")
    with _smb_lock:
        _smb_state = {
            "host": host, "share": share, "username": req.username,
            "password": req.password, "domain": req.domain, "subdir": subdir,
        }
        _smb_connected = True
    config["smb"] = dict(_smb_state)
    config["output_dir"] = f"//{host}/{share}/{subdir}".rstrip("/")
    save_config(config)
    return {"connected": True, "output_dir": config["output_dir"]}


@app.post("/api/smb/disconnect")
def smb_disconnect() -> dict:
    global _smb_connected, _smb_state
    smbclient.reset_connection_cache()
    with _smb_lock:
        _smb_connected = False
        _smb_state = {}
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
