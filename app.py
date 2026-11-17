"""
RT Grabber — a small local web UI around yt-dlp for archiving videos
(RTArchive "IA Link" / archive.org / YouTube) into a Jellyfin-friendly
folder layout with embedded + sidecar thumbnails.

Run:  python app.py    then open  http://127.0.0.1:8765
"""

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
import smbclient

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from downloads import DownloadManager, destination_id, smb_path, url_keys
from state import ACTIVE, FINISHED, JobStore

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

# Plain stdout logging so `docker logs` / Portainer's log viewer show it.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("rt-grabber")
thumb_log = logging.getLogger("rt-grabber.thumbnails")

DEFAULTS = {
    "output_dir": DEFAULT_OUTPUT, "max_workers": 3,
    "staging_dir": os.environ.get("STAGING_DIR", str(CONFIG_DIR / "staging")),
    "min_free_gb": 5.0, "max_retries": 3, "retry_delay_seconds": 10,
    "bandwidth_limit": "", "ntfy_url": "", "ntfy_token": "",
    "pool_status_file": os.environ.get("POOL_STATUS_FILE", ""), "pool_max_percent": 95.0,
}
_config_lock = threading.RLock()


def load_config() -> dict:
    if CONFIG_PATH.exists():
        loaded = {**DEFAULTS, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        if loaded.get("smb", {}).get("host") and loaded["output_dir"].startswith("//"):
            loaded["output_dir"] = DEFAULT_OUTPUT
        return loaded
    return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic replacement preserves the previous config if writing is interrupted.
    with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG_DIR, encoding="utf-8", delete=False) as fh:
        temporary = Path(fh.name)
        try:
            json.dump(cfg, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(CONFIG_PATH)
    finally:
        temporary.unlink(missing_ok=True)


config = load_config()
store: JobStore | None = None
manager: DownloadManager | None = None


# --------------------------------------------------------------------------
# SMB  (pure-Python SMB2/3 via smbprotocol — no kernel mount needed)
# --------------------------------------------------------------------------
_smb_connected = False
_smb_lock = threading.Lock()


def _smb_open_session(host: str, share: str, username: str, password: str, domain: str) -> None:
    account = f"{domain}\\{username}" if domain and "\\" not in username else username
    kw: dict = {"username": account, "password": password, "connection_timeout": 15}
    smbclient.register_session(host, **kw)
    smbclient.stat(f"\\\\{host}\\{share}")  # raises if share is unreachable / auth fails


def _try_auto_connect() -> None:
    global _smb_connected
    smb = config.get("smb")
    if not smb or not smb.get("host"):
        return
    try:
        _smb_open_session(smb["host"], smb["share"], smb["username"], smb["password"], smb.get("domain", ""))
        with _smb_lock:
            _smb_connected = True
    except Exception as exc:
        print(f"[SMB] Auto-connect failed: {exc}", file=sys.stderr)


@asynccontextmanager
async def lifespan(app):
    global store, manager
    store = JobStore(CONFIG_DIR / "state.sqlite3")
    manager = DownloadManager(store, config)
    if not config.get("smb", {}).get("host"):
        store.import_archive(Path(config["output_dir"]) / ".downloaded.txt", destination_id(current_target()))
    _try_auto_connect()
    manager.start()
    follow_thread = threading.Thread(target=follow_worker, name="show-follows", daemon=True)
    follow_thread.start()
    try:
        yield
    finally:
        manager.stop()
        follow_thread.join(timeout=25)
        # Daemon workers may still be returning from network timeouts. Their
        # transactions finish against the connection before process exit.


app = FastAPI(title="RT Grabber", lifespan=lifespan)


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
    if "timestampValue" in v:
        return v["timestampValue"]
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


def _encode_cursor(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def _decode_cursor(value, scope):
    if not value:
        return None
    try:
        data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if data["scope"] != scope or len(value) > 12000:
            raise ValueError("Cursor does not match this search")
        return data
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, "Invalid catalog cursor") from exc


def _catalog_page(show_id, cursor="", limit=500):
    scope = "show:" + show_id
    decoded = _decode_cursor(cursor, scope)
    query = {
        "from": [{"collectionId": "videos"}],
        "where": {"fieldFilter": {"field": {"fieldPath": "shows"},
                  "op": "ARRAY_CONTAINS", "value": {"stringValue": show_id}}},
        "orderBy": [{"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}],
        "limit": limit,
    }
    if decoded:
        query["startAt"] = {"values": [{"referenceValue": f"{FIRESTORE_URL.removeprefix('https://firestore.googleapis.com/v1/')}/videos/{decoded['id']}"}], "before": False}
    docs = _run_firestore_query({"structuredQuery": query})
    next_cursor = _encode_cursor({"scope": scope, "id": docs[-1]["id"]}) if len(docs) == limit else None
    return docs, next_cursor


def _merge_episodes(episodes):
    results, by_key = [], {}
    for episode in episodes:
        existing = next((by_key[key] for key in episode["keys"] if key in by_key), None)
        if existing is None:
            results.append(episode)
            existing = episode
        else:
            for field in ("rt_url", "youtube_url", "description", "show", "season", "episode_number"):
                if existing.get(field) is None or existing.get(field) == "":
                    existing[field] = episode.get(field)
            existing["keys"] = list(dict.fromkeys(existing["keys"] + episode["keys"]))
        for key in existing["keys"]:
            by_key[key] = existing
    return results


def _episode_history(episodes):
    done, pending = store.history(destination_id(current_target()))
    for episode in episodes:
        episode["download_status"] = "downloaded" if done.intersection(episode["keys"]) else "queued" if pending.intersection(episode["keys"]) else "missing"
    return episodes


@app.get("/api/browse/shows/{show_id}/episodes")
def browse_episodes(show_id: str, cursor: str = "", limit: int = Query(5000, ge=1, le=5000)) -> dict:
    fallback_title = show_id
    try:
        docs, next_cursor = _catalog_page(show_id, cursor, limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Couldn't reach RT Archive: {exc}")
    try:
        fallback_title = next((s.get("title") for s in get_shows() if s["id"] == show_id), show_id)
    except Exception:
        pass
    episodes = _merge_episodes([_shape_episode(d, fallback_show=fallback_title) for d in docs])
    episodes.sort(key=lambda e: e["date"])
    return {"episodes": _episode_history(episodes), "next_cursor": next_cursor}


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
        "keys": list(dict.fromkeys(key for u in (rt_url, youtube_url) if u for key in url_keys(u))) or ["catalog:" + d["id"]],
        "description": attrs.get("description") or d.get("description") or "",
        "release_date": _release_date(d.get("sort_date") or d.get("date")),
        "title": d.get("title") or d["id"],
        "date": _date_number(d.get("sort_date") or d.get("date")),
        "duration": d.get("duration"),
        "rt_url": rt_url,
        "youtube_url": youtube_url,
        "show": attrs.get("show_title") or fallback_show
        or (show_slug.replace("-", " ").title() if show_slug else None),
        "season": attrs.get("season_number"),
        "episode_number": attrs.get("number"),
    }


def _date_number(value):
    try:
        number = float(value)
        return number / 1000 if number > 100000000000 else number
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0


def _release_date(value):
    stamp = _date_number(value)
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d") if stamp else ""
    except (ValueError, OverflowError, OSError):
        return ""


@app.get("/api/browse/episodes")
def browse_episode_search(q: str = "", cursor: str = "", limit: int = Query(100, ge=1, le=500)) -> dict:
    query = q.strip()
    if not query:
        return {"episodes": [], "next_cursor": None}
    variants = sorted({query, query.upper(), query.title()})
    scope = "title:" + query
    positions = (_decode_cursor(cursor, scope) or {}).get("positions", {})
    episodes = []
    try:
        for variant in variants:
            if positions.get(variant) is False:
                continue
            structured = {
                "from": [{"collectionId": "videos"}],
                "where": {"compositeFilter": {"op": "AND", "filters": [
                    {"fieldFilter": {"field": {"fieldPath": "title"}, "op": "GREATER_THAN_OR_EQUAL", "value": {"stringValue": variant}}},
                    {"fieldFilter": {"field": {"fieldPath": "title"}, "op": "LESS_THAN", "value": {"stringValue": variant + "\uf8ff"}}},
                ]}},
                "orderBy": [{"field": {"fieldPath": "title"}, "direction": "ASCENDING"},
                            {"field": {"fieldPath": "__name__"}, "direction": "ASCENDING"}],
                "limit": limit,
            }
            position = positions.get(variant)
            if position:
                structured["startAt"] = {"values": [{"stringValue": position["title"]},
                    {"referenceValue": f"{FIRESTORE_URL.removeprefix('https://firestore.googleapis.com/v1/')}/videos/{position['id']}"}], "before": False}
            docs = _run_firestore_query({"structuredQuery": structured})
            positions[variant] = {"id": docs[-1]["id"], "title": docs[-1]["title"]} if len(docs) == limit else False
            episodes.extend(_shape_episode(d) for d in docs)
    except Exception as exc:
        raise HTTPException(502, f"Couldn't reach RT Archive: {exc}")
    episodes = _merge_episodes(episodes)
    episodes.sort(key=lambda e: e["title"].lower())
    next_cursor = _encode_cursor({"scope": scope, "positions": positions}) if any(positions.values()) else None
    return {"episodes": _episode_history(episodes), "next_cursor": next_cursor}


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
    Path(config["staging_dir"]).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=config["staging_dir"]) as td:
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


def _iter_video_files(target=None):
    """Yield (kind, dirpath, filename, root) for every video in the active storage location."""
    target = target or current_target()
    if target["kind"] == "smb":
        _smb_open_session(target["host"], target["share"], target.get("username", ""),
                          target.get("password", ""), target.get("domain", ""))
        root = smb_path(target)
        for dirpath, _dirs, files in smbclient.walk(root):
            for fn in files:
                if fn.lower().endswith(VIDEO_EXTS):
                    yield "smb", dirpath, fn, root
    else:
        root = target["path"]
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
    relative = dirpath_norm[len(root_norm):].strip("/\\")
    return relative.replace("/", sep).split(sep)[0]


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

            show_name = _show_folder_name(kind, dirpath, root)
            art_dir = root.rstrip("/\\") + ("\\" if kind == "smb" else os.sep) + show_name if show_name else dirpath
            folder_key = (kind, art_dir)
            if folder_key not in seen_folders:
                seen_folders.add(folder_key)
                if show_name and not _fpath_exists(kind, art_dir, "folder.jpg"):
                    thumb_log.info("fetching show art for folder %r", show_name)
                    poster = _lookup_show_poster(show_name)
                    if poster:
                        try:
                            _fwrite_bytes(kind, art_dir, "folder.jpg", poster)
                            thumb_log.info("wrote %s/folder.jpg (%d bytes)", art_dir, len(poster))
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
    season: int | None = Field(None, ge=0)
    episode: int | None = Field(None, ge=0)
    title: str = ""
    description: str = ""
    release_date: str = ""
    keys: list[str] = Field(default_factory=list, max_length=10)


class DownloadRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    items: list[DownloadItem] = Field(default_factory=list)


class ConfigRequest(BaseModel):
    output_dir: str | None = None
    staging_dir: str | None = None
    min_free_gb: float | None = Field(None, ge=0, le=100000)
    max_retries: int | None = Field(None, ge=0, le=10)
    retry_delay_seconds: int | None = Field(None, ge=1, le=300)
    bandwidth_limit: str | None = None
    ntfy_url: str | None = None
    ntfy_token: str | None = None
    pool_status_file: str | None = None
    pool_max_percent: float | None = Field(None, ge=50, le=99.9)


class WorkersRequest(BaseModel):
    max_workers: int


def current_target():
    with _config_lock:
        smb = config.get("smb", {})
        if smb.get("host"):
            return {"kind": "smb", **smb}
        return {"kind": "local", "path": config["output_dir"]}


def prepare_items(items):
    target = current_target()
    destination = destination_id(target)
    prepared = []
    for item in items:
        data = item.model_dump()
        url = data["url"].strip()
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise HTTPException(400, "Download URLs must use http or https")
        if parsed.username or parsed.password:
            raise HTTPException(400, "Use a URL without embedded credentials")
        data.update(url=url, keys=list(dict.fromkeys([*url_keys(url), *data["keys"]])),
                    target=target, destination=destination, staging_root=config["staging_dir"])
        prepared.append(data)
    return prepared


@app.get("/api/config")
def get_config() -> dict:
    result = {key: value for key, value in config.items() if key not in ("smb", "ntfy_token")}
    smb = config.get("smb", {})
    result.update(yt_dlp_ok=shutil.which(YT_DLP) is not None, ffmpeg_ok=shutil.which(FFMPEG_BIN) is not None,
                  smb_connected=_smb_connected, ntfy_token_set=bool(config.get("ntfy_token")),
                  smb={key: value for key, value in smb.items() if key != "password"})
    return result


@app.post("/api/config")
def set_config(req: ConfigRequest) -> dict:
    updates = req.model_dump(exclude_none=True)
    for field in ("output_dir", "staging_dir"):
        if field in updates:
            path = updates[field].strip()
            if not path:
                raise HTTPException(400, f"{field} cannot be empty")
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(400, f"Cannot use {field}: {exc}") from exc
            updates[field] = str(Path(path).resolve())
    if "bandwidth_limit" in updates:
        updates["bandwidth_limit"] = updates["bandwidth_limit"].strip()
        if updates["bandwidth_limit"] and not re.fullmatch(r"\d+(?:\.\d+)?[KMGkmg]?", updates["bandwidth_limit"]):
            raise HTTPException(400, "Use a bandwidth limit such as 5M, 500K, or leave blank")
    if updates.get("ntfy_url"):
        parsed = urlsplit(updates["ntfy_url"])
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.path.strip("/"):
            raise HTTPException(400, "Enter a full ntfy topic URL such as https://ntfy.example.com/rt-grabber")
    with _config_lock:
        save_config({**config, **updates})
        config.update(updates)
    return get_config()


@app.post("/api/workers")
def set_workers(req: WorkersRequest) -> dict:
    number = max(1, min(20, req.max_workers))
    with _config_lock:
        save_config({**config, "max_workers": number})
        config["max_workers"] = number
    manager.wake.set()
    return {"max_workers": number}


@app.post("/api/download")
def submit(req: DownloadRequest) -> dict:
    items = list(req.items) + [DownloadItem(url=url) for url in req.urls if url.strip()]
    items = [item for item in items if item.url.strip()]
    if not items:
        raise HTTPException(400, "No URLs provided")
    prepared = prepare_items(items)
    result = store.enqueue(prepared)
    manager.wake.set()
    return result


def public_job(job):
    return {key: value for key, value in job.items() if key not in ("target",)}


@app.get("/api/jobs")
def list_jobs(q: str = "", status: str = "", offset: int = Query(0, ge=0),
              limit: int | None = Query(None, ge=1, le=5000)) -> dict:
    all_jobs = store.jobs()
    counts = {state: 0 for state in (*ACTIVE, *FINISHED)}
    for job in all_jobs:
        counts[job["status"]] += 1
    filtered = [job for job in all_jobs if (not status or job["status"] == status)
                and q.casefold() in " ".join(str(job.get(key) or "") for key in ("title", "show", "url", "error")).casefold()]
    page = filtered[offset:offset + limit] if limit else filtered[offset:]
    return {"jobs": [public_job(job) for job in page], "total": len(filtered), "counts": counts,
            "completed_bytes": sum(j.get("delivered_bytes", 0) for j in all_jobs if j["status"] == "done"),
            "paused": manager.runtime_paused or store.setting("paused", False),
            "pause_reason": manager.storage_error or store.setting("pause_reason", ""),
            "notification_error": manager.last_notification_error}


@app.get("/api/batches")
def list_batches():
    batches = {}
    for job in store.jobs():
        batch = batches.setdefault(job["batch_id"], {"id": job["batch_id"], "total": 0, "done": 0,
                  "error": 0, "cancelled": 0, "active": 0, "bytes": 0, "queued_at": job["queued_at"]})
        batch["total"] += 1
        batch[job["status"] if job["status"] in FINISHED else "active"] += 1
        batch["bytes"] += job.get("delivered_bytes", 0)
    return {"batches": list(batches.values())}


@app.post("/api/queue/pause")
def pause_queue():
    manager.pause()
    return {"paused": True}


@app.post("/api/queue/resume")
def resume_queue():
    try:
        manager.resume()
    except OSError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"paused": False}


@app.get("/api/storage")
def get_storage():
    try:
        return {"locations": manager.check_storage(), "error": ""}
    except OSError as exc:
        return {"locations": [], "error": str(exc)}


@app.post("/api/notifications/test")
def test_notification():
    if not config.get("ntfy_url"):
        raise HTTPException(400, "Save an ntfy topic URL first")
    if not manager.notify("RT Grabber", "Test notification from RT Grabber"):
        raise HTTPException(502, manager.last_notification_error)
    return {"sent": True}


def _job(job_id):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _remove_job(job):
    with manager.process_lock:
        if job["id"] in manager.in_flight:
            raise HTTPException(409, "The worker is still stopping. Try again shortly")
        job = _job(job["id"])
        if job["status"] not in FINISHED:
            raise HTTPException(400, "Cancel the download before removing it")
        shutil.rmtree(Path(job["staging_root"]) / job["id"], ignore_errors=True)
        store.remove(job["id"])


@app.post("/api/jobs/clear")
def clear_finished() -> dict:
    for job in store.jobs():
        if job["status"] in FINISHED:
            _remove_job(job)
    return {"ok": True}


@app.post("/api/jobs/clear/done")
def clear_done() -> dict:
    for job in store.jobs():
        if job["status"] == "done":
            _remove_job(job)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = _job(job_id)
    if job["status"] in FINISHED:
        raise HTTPException(400, "Job is already finished")
    if not store.transition(job_id, ACTIVE, "cancelled", finished=time.time()):
        raise HTTPException(409, "Job has already finished")
    with manager.process_lock:
        proc = manager.processes.get(job_id)
        if proc and proc.poll() is None:
            proc.terminate()
    return {"cancelled": True}


def _retry_job(job_id):
    job = _job(job_id)
    if job["status"] not in ("error", "cancelled"):
        raise HTTPException(400, "Only errored or cancelled jobs can be retried")
    with manager.process_lock:
        if job_id in manager.in_flight:
            raise HTTPException(409, "The worker is still stopping. Try again shortly")
        if not store.transition(job_id, ("error", "cancelled"), "queued", attempts=0, next_attempt_at=0, error="", queued_at=time.time()):
            raise HTTPException(409, "Job state changed. Refresh and try again")
    manager.wake.set()


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict:
    _retry_job(job_id)
    return {"retried": True}


@app.post("/api/jobs/retry/failed")
def retry_failed_jobs() -> dict:
    failed = [job for job in store.jobs() if job["status"] == "error"]
    for job in failed:
        _retry_job(job["id"])
    return {"retried": len(failed)}


@app.delete("/api/jobs/{job_id}")
def remove_job(job_id: str) -> dict:
    _remove_job(_job(job_id))
    return {"removed": True}


_follow_lock = threading.Lock()


class FollowRequest(BaseModel):
    show_id: str = Field(min_length=1, max_length=250)
    title: str = ""
    preference: str = "roosterteeth"
    interval_hours: float = Field(6, ge=0.25, le=720)
    auto_queue: bool = False


@app.get("/api/follows")
def list_follows():
    return {"follows": [{key: value for key, value in follow.items() if key != "known_keys"}
                        for follow in store.subscriptions()]}


@app.post("/api/follows")
def follow_show(req: FollowRequest):
    if req.preference not in ("roosterteeth", "youtube"):
        raise HTTPException(400, "Choose Website or YouTube")
    with _follow_lock:
        previous = next((f for f in store.subscriptions() if f["id"] == req.show_id), {})
        follow = {**previous, **req.model_dump(), "id": req.show_id, "next_check": 0,
                  "known_keys": previous.get("known_keys"), "error": ""}
        store.save_subscription(follow)
    return {"followed": True}


@app.delete("/api/follows/{show_id}")
def unfollow_show(show_id: str):
    with _follow_lock:
        store.remove_subscription(show_id)
    return {"removed": True}


def check_follows(force=False):
    if not _follow_lock.acquire(blocking=False):
        return
    try:
        for follow in store.subscriptions():
            if not force and follow.get("next_check", 0) > time.time():
                continue
            if manager.stop_event.is_set():
                return
            try:
                episodes, cursor = [], ""
                while True:
                    docs, cursor = _catalog_page(follow["id"], cursor, 500)
                    episodes.extend(_shape_episode(d, follow["title"]) for d in docs)
                    if not cursor or manager.stop_event.is_set():
                        break
                if manager.stop_event.is_set():
                    return
                episodes = _merge_episodes(episodes)
                archived = [ep for ep in episodes if ep["rt_url"] or ep["youtube_url"]]
                known = set(follow.get("known_keys") or [])
                fresh = [ep for ep in archived if not known.intersection(ep["keys"])] if follow.get("known_keys") is not None else []
                if fresh and follow["auto_queue"]:
                    items = []
                    for ep in fresh:
                        preferred = ep["youtube_url"] if follow["preference"] == "youtube" else ep["rt_url"]
                        items.append(DownloadItem(url=preferred or ep["rt_url"] or ep["youtube_url"],
                            show=ep["show"], season=ep["season"], episode=ep["episode_number"],
                            title=ep["title"], description=ep["description"], release_date=ep["release_date"], keys=ep["keys"]))
                    store.enqueue(prepare_items(items))
                    manager.wake.set()
                for ep in archived:
                    known.update(ep["keys"])
                if fresh:
                    manager.notify("New archived episodes", f"{follow['title'] or follow['id']}: {len(fresh)} new episodes" + (" queued" if follow["auto_queue"] else " available"))
                follow.update(known_keys=sorted(known), last_check=time.time(), new_count=len(fresh), error="")
            except Exception as exc:
                follow["error"] = str(exc)[-1000:]
                log.warning("Follow check failed for %s: %s", follow["id"], exc)
            follow["next_check"] = time.time() + follow["interval_hours"] * 3600
            store.save_subscription(follow)
    finally:
        _follow_lock.release()


def follow_worker():
    while not manager.stop_event.is_set():
        try:
            check_follows()
        except Exception:
            log.exception("Could not save followed show state")
        manager.stop_event.wait(30)


@app.post("/api/follows/check")
def check_followed_shows():
    threading.Thread(target=check_follows, kwargs={"force": True}, daemon=True).start()
    return {"started": True}


_library_state = {"running": False, "checked": 0, "recorded": 0, "error": ""}
_library_lock = threading.Lock()


def _scan_library():
    target = current_target()
    destination = destination_id(target)
    try:
        for kind, dirpath, filename, _root in _iter_video_files(target):
            video_id = _bracket_id(filename)
            with _library_lock:
                _library_state["checked"] += 1
            if video_id and _IA_ID_RE.match(video_id):
                store.record_existing(destination, url_keys("https://archive.org/details/" + video_id))
                with _library_lock:
                    _library_state["recorded"] += 1
    except Exception as exc:
        with _library_lock:
            _library_state["error"] = str(exc)
    finally:
        with _library_lock:
            _library_state["running"] = False


@app.post("/api/library/scan")
def scan_library():
    with _library_lock:
        if _library_state["running"]:
            raise HTTPException(409, "A library scan is already running")
        _library_state.update(running=True, checked=0, recorded=0, error="")
    threading.Thread(target=_scan_library, daemon=True).start()
    return {"started": True}


@app.get("/api/library/scan")
def library_scan_status():
    with _library_lock:
        return dict(_library_state)


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
    try:
        _smb_open_session(host, share, req.username, req.password, req.domain)
    except Exception as exc:
        raise HTTPException(400, f"Connection failed: {exc}")
    subdir = req.subdir.strip().strip("/\\")
    smb = {"host": host, "share": share, "username": req.username,
           "password": req.password, "domain": req.domain, "subdir": subdir}
    with _config_lock:
        updated = {**config, "smb": smb}
        save_config(updated)
        config.update(updated)
    with _smb_lock:
        _smb_connected = True
    return {"connected": True, "output_dir": config["output_dir"]}


@app.post("/api/smb/disconnect")
def smb_disconnect() -> dict:
    global _smb_connected
    with _smb_lock:
        _smb_connected = False
    with _config_lock:
        updated = dict(config)
        updated.pop("smb", None)
        save_config(updated)
        config.pop("smb", None)
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
