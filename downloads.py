"""Resumable downloads and verified delivery to local folders or SMB shares."""

import html
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import requests
import smbclient

from state import ACTIVE

log = logging.getLogger("rt-grabber.downloads")
VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".mov", ".avi"}
PROGRESS = "@@PROG@@"
FILE = "@@FILE@@"


def url_key(url):
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if host == "archive.org" and len(parts) >= 2 and parts[0] in ("details", "download"):
        return "ia:" + parts[1]
    if host == "youtu.be" and parts:
        return "youtube:" + parts[0]
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com"):
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if not video_id and len(parts) >= 2 and parts[0] in ("shorts", "embed", "live"):
            video_id = parts[1]
        if video_id:
            return "youtube:" + video_id
    return "url:" + parsed._replace(fragment="").geturl()


def url_keys(url):
    key = url_key(url)
    keys = [key]
    if key.startswith("ia:youtube-"):
        keys.append("youtube:" + key.removeprefix("ia:youtube-"))
    return keys


def clean_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value).strip().rstrip(". ")[:150] or "Untitled"


def output_template(job):
    prefix = ""
    if job.get("season") is not None and job.get("episode") is not None:
        prefix = f"S{job['season']:02d}E{job['episode']:02d} - "
    filename = prefix + "%(title).200B [%(id)s].%(ext)s"
    folders = []
    if job.get("show"):
        # Literal percent signs in user/catalog names must survive yt-dlp templates.
        folders.append(clean_name(job["show"]).replace("%", "%%"))
        if job.get("season") is not None:
            folders.append(f"Season {job['season']:02d}")
    return "/".join([*folders, filename])


def destination_id(target):
    if target["kind"] == "smb":
        return "smb:" + "/".join(str(target.get(k, "")).strip("/\\").lower()
                                  for k in ("host", "share", "subdir"))
    return "local:" + os.path.normcase(str(Path(target["path"]).resolve()))


def smb_path(target, relative=""):
    root = f"\\\\{target['host']}\\{target['share']}"
    for part in (target.get("subdir", ""), str(relative)):
        normalized = part.replace("/", "\\").strip("\\")
        if normalized:
            root += "\\" + normalized
    return root


def storage_check(path, minimum_gb):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free
    if free < minimum_gb * 1024 ** 3:
        raise OSError(f"{path} has {free / 1024 ** 3:.1f} GiB free; reserve is {minimum_gb:g} GiB")
    # A mount may advertise rw while ext4 is in emergency_ro.
    with tempfile.TemporaryFile(dir=path) as probe:
        probe.write(b"rt-grabber storage check\n")
        probe.flush()
        os.fsync(probe.fileno())
    return {"path": str(path), "free_bytes": free, "writable": True}


class Interrupted(Exception):
    pass


class DownloadManager:
    def __init__(self, store, config):
        self.store = store
        self.config = config
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.processes = {}
        self.in_flight = set()
        self.process_lock = threading.Lock()
        self.threads = []
        self.storage_error = ""
        self.last_notification_error = ""
        self.notification_lock = threading.Lock()
        self.runtime_paused = False

    def start(self):
        self.store.recover()
        for i in range(20):
            self.threads.append(threading.Thread(target=self.worker, name=f"download-{i}", daemon=True))
        self.threads.append(threading.Thread(target=self.monitor, name="storage-monitor", daemon=True))
        for thread in self.threads:
            thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        self.terminate_processes()
        deadline = time.monotonic() + 20
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        with self.process_lock:
            for proc in self.processes.values():
                if proc.poll() is None:
                    proc.kill()
        for thread in self.threads:
            thread.join(timeout=2)

    def terminate_processes(self):
        with self.process_lock:
            for proc in self.processes.values():
                if proc.poll() is None:
                    proc.terminate()

    def pause(self, reason="Paused by user"):
        self.runtime_paused = True
        self.storage_error = reason
        try:
            self.store.set_setting("paused", True)
            self.store.set_setting("pause_reason", reason)
        finally:
            self.terminate_processes()

    def resume(self):
        with self.process_lock:
            if self.in_flight:
                raise OSError("Workers are still stopping. Resume again shortly.")
        self.check_storage()
        for job in self.store.jobs():
            if job["status"] == "paused":
                self.store.update(job["id"], status="queued", next_attempt_at=0, error="")
        self.store.set_setting("paused", False)
        self.store.set_setting("pause_reason", "")
        self.storage_error = ""
        self.runtime_paused = False
        self.wake.set()

    def check_storage(self, job=None):
        status_file = self.config.get("pool_status_file", "")
        if status_file:
            try:
                status = json.loads(Path(status_file).read_text(encoding="utf-8"))
                if time.time() - float(status["checked_at"]) > 180:
                    raise ValueError("status is more than 3 minutes old")
                if status.get("error"):
                    raise ValueError(status["error"])
                threshold = self.config["pool_max_percent"]
                for key in ("data_percent", "metadata_percent"):
                    if float(status[key]) >= threshold:
                        raise ValueError(f"{status['pool']} {key} is {status[key]}% (limit {threshold:g}%)")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise OSError(f"Proxmox pool guard: {exc}") from exc
        minimum = self.config["min_free_gb"]
        paths = [self.config["staging_dir"]]
        if job:
            paths = [job["staging_root"]]
            if job["target"]["kind"] == "local":
                paths.append(job["target"]["path"])
        elif not self.config.get("smb", {}).get("host"):
            paths.append(self.config["output_dir"])
        return [storage_check(path, minimum) for path in dict.fromkeys(paths)]

    def control(self, job_id):
        job = self.store.get(job_id)
        if not job or job["status"] == "cancelled":
            raise Interrupted("Cancelled")
        if self.stop_event.is_set() or self.runtime_paused or self.store.setting("paused", False):
            raise Interrupted("Paused")

    def worker(self):
        while not self.stop_event.is_set():
            job = None
            try:
                with self.process_lock:
                    if not self.runtime_paused:
                        job = self.store.claim(self.config["max_workers"])
                        if job:
                            self.in_flight.add(job["id"])
                if job:
                    self.run_job(job)
            except (sqlite3.Error, OSError) as exc:
                log.exception("Queue/storage operation failed")
                try:
                    self.pause(f"Storage unavailable: {exc}")
                    if job:
                        self.store.transition(job["id"], ("running", "uploading"), "paused", error=self.storage_error)
                except (sqlite3.Error, OSError):
                    log.exception("Could not persist the storage pause")
            except Exception as exc:
                log.exception("Download worker failed")
                if job:
                    self.fail(job, exc)
            finally:
                if job:
                    with self.process_lock:
                        self.in_flight.discard(job["id"])
            if job is None:
                self.wake.wait(1)
                self.wake.clear()

    def monitor(self):
        while not self.stop_event.wait(3):
            try:
                active = [j for j in self.store.jobs() if j["status"] in ("running", "uploading")]
                if active and not self.runtime_paused:
                    self.check_storage()
                    for job in active:
                        self.check_storage(job)
            except (OSError, sqlite3.Error) as exc:
                log.error("Storage guard paused transfers: %s", exc)
                try:
                    self.pause(f"Storage unavailable: {exc}")
                    self.notify("Storage paused", str(exc), "warning")
                except (OSError, sqlite3.Error):
                    self.terminate_processes()

    def fail(self, job, exc):
        current = self.store.get(job["id"])
        if not current or current["status"] == "cancelled":
            return
        message = str(exc)[-4000:]
        permanent = any(text in message.lower() for text in (
            "unsupported url", "video unavailable", "private video", "http error 404", "no such file or directory: 'yt-dlp'",
        ))
        retries = self.config["max_retries"]
        retry = not permanent and current["attempts"] <= retries
        delay = min(300, self.config["retry_delay_seconds"] * 2 ** max(0, current["attempts"] - 1))
        changed = self.store.transition(job["id"], ("running", "uploading"),
                                        "retry_wait" if retry else "error", error=message,
                                        next_attempt_at=time.time() + delay if retry else 0)
        if not changed:
            return
        if not retry:
            self.notify("Download failed", (job.get("title") or job["url"]) + "\n" + message, "failure")
            self.notify_batch(job["batch_id"])

    def command(self, job, media_dir, archive):
        template = ("download:" + PROGRESS + "%(progress)j")
        cmd = [os.environ.get("YT_DLP_BIN", "yt-dlp"), "--newline", "--progress", "--no-simulate",
               "--progress-template", template, "--progress-delta", "1", "--continue",
               "--print", "after_move:" + FILE + "%(filepath)j", "--write-info-json", "--write-thumbnail",
               "--embed-thumbnail", "--convert-thumbnails", "jpg", "--embed-metadata",
               "--remux-video", "mkv", "--merge-output-format", "mkv",
               "--download-archive", str(archive), "--retries", "3", "--fragment-retries", "3",
               "--retry-sleep", "http:exp=2:30", "-o", output_template(job), "-P", str(media_dir)]
        if self.config.get("bandwidth_limit"):
            cmd.extend(["--limit-rate", self.config["bandwidth_limit"]])
        cmd.extend(["--", job["url"]])
        return cmd

    def run_job(self, job):
        try:
            self.control(job["id"])
            try:
                self.check_storage(job)
            except OSError as exc:
                self.pause(f"Storage unavailable: {exc}")
                self.store.transition(job["id"], ("running", "uploading"), "paused", error=self.storage_error)
                self.notify("Storage paused", str(exc), "warning")
                return
            work = Path(job["staging_root"]) / job["id"]
            media = work / "media"
            media.mkdir(parents=True, exist_ok=True)
            archive = work / "archive.txt"
            if not archive.exists():
                archive.write_text("\n".join(self.store.archive(job["destination"])) + "\n", encoding="utf-8")
            current = self.store.get(job["id"])
            if current.get("download_complete"):
                expected = current.get("video_files", [])
                if not expected or any(not (media / name).is_file() or (media / name).stat().st_size == 0 for name in expected):
                    raise RuntimeError("Staged video files are missing. Restore the original staging mount, or remove this job and queue it again.")
            if not current.get("download_complete"):
                self.download(job, media, archive)
                files = [str(p.relative_to(media)) for p in media.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
                if not files:
                    raise RuntimeError("yt-dlp produced no video files. Check the URL, playlist contents, and source archive entries.")
                self.metadata(job, media)
                self.store.update(job["id"], download_complete=True, phase="upload", video_files=files)
            self.control(job["id"])
            if not self.store.transition(job["id"], ("running",), "uploading", phase="upload", percent=0):
                raise Interrupted("Job was stopped")
            delivered = self.deliver(job, media)
            self.control(job["id"])
            entries = [line for line in archive.read_text(encoding="utf-8").splitlines() if line.strip()]
            # Delivery, completion history, and archive entries become visible together.
            if self.store.finish(job["id"], entries, delivered_bytes=delivered):
                shutil.rmtree(work, ignore_errors=True)
                self.notify_batch(job["batch_id"])
        except Interrupted:
            current = self.store.get(job["id"])
            if current and current["status"] != "cancelled":
                self.store.transition(job["id"], ("running", "uploading"), "paused", error=self.storage_error or "Paused")
        except OSError as exc:
            if exc.errno in (28, 30, 122):
                self.pause(f"Storage unavailable: {exc}")
                self.store.transition(job["id"], ("running", "uploading"), "paused", error=self.storage_error)
            else:
                self.fail(job, exc)
        except Exception as exc:
            self.fail(job, exc)

    def download(self, job, media, archive):
        cmd = self.command(job, media, archive)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        with self.process_lock:
            self.processes[job["id"]] = proc
        tail, updates, last_save = [], {}, 0
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line.startswith(PROGRESS):
                    try:
                        progress = json.loads(line[len(PROGRESS):])
                        downloaded = progress.get("downloaded_bytes") or 0
                        total = progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0
                        updates.update(downloaded_bytes=downloaded, total_bytes=total,
                                       percent=100 * downloaded / total if total else 0,
                                       speed=progress.get("_speed_str") or "", eta=progress.get("_eta_str") or "")
                    except (TypeError, ValueError):
                        log.debug("Could not parse progress line")
                elif line.startswith(FILE):
                    log.debug("Completed media %s", line[len(FILE):])
                elif line:
                    tail.append(line[-1000:])
                    tail = tail[-40:]
                if time.monotonic() - last_save >= 1:
                    self.control(job["id"])
                    self.store.update(job["id"], log=tail, **updates)
                    last_save = time.monotonic()
            proc.wait()
            self.control(job["id"])
            self.store.update(job["id"], log=tail, **updates)
            if proc.returncode != 0:
                raise RuntimeError("\n".join(tail[-8:]) or f"yt-dlp exited with code {proc.returncode}")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            with self.process_lock:
                self.processes.pop(job["id"], None)

    def metadata(self, job, media):
        for video in media.rglob("*"):
            if not video.is_file() or video.suffix.lower() not in VIDEO_EXTS:
                continue
            if video.stat().st_size == 0:
                raise RuntimeError(f"Empty video file: {video.name}")
            info_file = video.with_suffix(".info.json")
            info = json.loads(info_file.read_text(encoding="utf-8")) if info_file.is_file() else {}
            if not job.get("title") and info.get("title"):
                self.store.update(job["id"], title=info["title"], thumbnail_url=info.get("thumbnail") or "")
            season, episode = job.get("season"), job.get("episode")
            root = ET.Element("episodedetails" if season is not None and episode is not None else "movie")
            values = {"title": job.get("title") or info.get("title") or video.stem,
                      "plot": html.unescape(re.sub("<[^>]+>", "", job.get("description") or info.get("description") or "")),
                      "showtitle": job.get("show"), "season": season, "episode": episode}
            date = job.get("release_date") or info.get("upload_date") or ""
            if len(date) == 8 and date.isdigit():
                date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                values.update(aired=date, premiered=date, year=date[:4])
            for key, value in values.items():
                if value is not None and value != "":
                    ET.SubElement(root, key).text = str(value)
            ET.SubElement(root, "uniqueid", type="rtgrabber").text = job["keys"][0]
            ET.ElementTree(root).write(video.with_suffix(".nfo"), encoding="utf-8", xml_declaration=True)
            if job.get("show"):
                show_dir = media / clean_name(job["show"])
                show_dir.mkdir(parents=True, exist_ok=True)
                show_nfo = ET.Element("tvshow")
                ET.SubElement(show_nfo, "title").text = job["show"]
                ET.ElementTree(show_nfo).write(show_dir / "tvshow.nfo", encoding="utf-8", xml_declaration=True)

    def deliver(self, job, media):
        target = job["target"]
        remote = target["kind"] == "smb"
        if remote:
            account = target.get("username", "")
            if target.get("domain") and "\\" not in account:
                account = target["domain"] + "\\" + account
            smbclient.register_session(target["host"], username=account,
                                       password=target.get("password", ""), connection_timeout=15)
            smbclient.makedirs(smb_path(target), exist_ok=True)
        files = [p for p in media.rglob("*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError("Staging contains no files to deliver")
        total = sum(p.stat().st_size for p in files)
        copied, last_save = 0, 0
        for src in files:
            self.control(job["id"])
            relative = src.relative_to(media)
            destination = smb_path(target, relative) if remote else Path(target["path"]) / relative
            temporary = str(destination) + ".rtgrabber-" + job["id"] + ".part"
            if remote:
                smbclient.makedirs(smb_path(target, relative.parent), exist_ok=True)
                output = smbclient.open_file(temporary, mode="wb")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                output = open(temporary, "wb")
            with src.open("rb") as source, output as dest:
                while chunk := source.read(1024 * 1024):
                    self.control(job["id"])
                    dest.write(chunk)
                    copied += len(chunk)
                    if time.monotonic() - last_save >= 1:
                        self.store.update(job["id"], percent=100 * copied / total if total else 0,
                                          upload_bytes=copied, upload_total_bytes=total)
                        last_save = time.monotonic()
                dest.flush()
                if not remote:
                    os.fsync(dest.fileno())
            actual_size = smbclient.stat(temporary).st_size if remote else Path(temporary).stat().st_size
            if actual_size != src.stat().st_size:
                raise RuntimeError(f"Upload size mismatch for {src.name}; local copy retained")
            if remote:
                smbclient.replace(temporary, destination)
            else:
                os.replace(temporary, destination)
        return total

    def notify(self, title, message, tag="information"):
        url = self.config.get("ntfy_url", "").strip()
        if not url:
            return False
        headers = {"Title": title.encode("ascii", "replace").decode(), "Tags": tag}
        if self.config.get("ntfy_token"):
            headers["Authorization"] = "Bearer " + self.config["ntfy_token"]
        try:
            response = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
            response.raise_for_status()
            self.last_notification_error = ""
            return True
        except requests.RequestException as exc:
            self.last_notification_error = str(exc)
            log.warning("Notification failed: %s", exc)
            return False

    def notify_batch(self, batch_id):
        with self.notification_lock:
            jobs = [j for j in self.store.jobs() if j["batch_id"] == batch_id]
            if not jobs or any(j["status"] in ACTIVE for j in jobs):
                return
            marker = "notified:" + batch_id
            if self.store.setting(marker, False):
                return
            done = sum(j["status"] == "done" for j in jobs)
            failed = sum(j["status"] == "error" for j in jobs)
            if self.notify("Batch finished", f"{done} completed, {failed} failed, {len(jobs) - done - failed} cancelled."):
                self.store.set_setting(marker, True)
