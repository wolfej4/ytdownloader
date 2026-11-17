"""Transactional queue, download history, and followed shows."""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


ACTIVE = ("queued", "running", "uploading", "retry_wait", "paused")
FINISHED = ("done", "error", "cancelled")


def archive_keys(entry):
    extractor, separator, video_id = entry.partition(" ")
    if not separator:
        return []
    if extractor.lower() == "youtube":
        return ["youtube:" + video_id, "ia:youtube-" + video_id]
    if extractor.lower() in ("archive.org", "archiveorg"):
        keys = ["ia:" + video_id]
        if video_id.startswith("youtube-"):
            keys.append("youtube:" + video_id.removeprefix("youtube-"))
        return keys
    return []


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, queued_at REAL NOT NULL,
                next_attempt_at REAL NOT NULL DEFAULT 0, data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS queue_order ON jobs(status, next_attempt_at, queued_at);
            CREATE TABLE IF NOT EXISTS job_keys (
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                destination TEXT NOT NULL, key TEXT NOT NULL,
                PRIMARY KEY(job_id, key)
            );
            CREATE INDEX IF NOT EXISTS identity_lookup ON job_keys(destination, key);
            CREATE TABLE IF NOT EXISTS completed (
                destination TEXT NOT NULL, key TEXT NOT NULL, finished REAL NOT NULL,
                PRIMARY KEY(destination, key)
            );
            CREATE TABLE IF NOT EXISTS archive_entries (
                destination TEXT NOT NULL, entry TEXT NOT NULL,
                PRIMARY KEY(destination, entry)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _write(self, job):
        self.db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "status=excluded.status, queued_at=excluded.queued_at, "
            "next_attempt_at=excluded.next_attempt_at, data=excluded.data",
            (job["id"], job["status"], job["queued_at"], job.get("next_attempt_at", 0), json.dumps(job)),
        )

    def get(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def jobs(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT data FROM jobs ORDER BY queued_at DESC, id DESC"
            )]

    def update(self, job_id, **fields):
        with self.transaction():
            job = self.get(job_id)
            if job is None:
                return None
            job.update(fields)
            self._write(job)
            return job

    def transition(self, job_id, allowed, status, **fields):
        with self.transaction():
            job = self.get(job_id)
            if job is None or job["status"] not in allowed:
                return False
            job.update(status=status, **fields)
            self._write(job)
            return True

    def setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT data FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def enqueue(self, items, redownload=False):
        created, skipped = [], 0
        batch_id = uuid.uuid4().hex
        with self.transaction():
            for item in items:
                destination, keys = item["destination"], item["keys"]
                placeholders = ",".join("?" for _ in keys)
                found = self.db.execute(
                    f"SELECT 1 FROM completed WHERE destination=? AND key IN ({placeholders}) LIMIT 1",
                    (destination, *keys),
                ).fetchone()
                pending = self.db.execute(
                    f"SELECT 1 FROM job_keys k JOIN jobs j ON j.id=k.job_id "
                    f"WHERE k.destination=? AND k.key IN ({placeholders}) "
                    "AND j.status NOT IN ('cancelled','done') LIMIT 1",
                    (destination, *keys),
                ).fetchone()
                if pending or (found and not redownload):
                    skipped += 1
                    continue
                job = dict(item, id=uuid.uuid4().hex, batch_id=batch_id, status="queued", redownload=redownload,
                           queued_at=time.time(), next_attempt_at=0, attempts=0,
                           percent=0, percent_str="", speed="", eta="", error="",
                           thumbnail_url="", current=None, total=None, downloaded_bytes=0,
                           total_bytes=0, log=[], phase="download")
                self._write(job)
                self.db.executemany("INSERT INTO job_keys VALUES (?,?,?)",
                                    [(job["id"], destination, key) for key in keys])
                created.append(job["id"])
        return {"created": created, "skipped": skipped, "batch_id": batch_id}

    def claim(self, max_workers):
        with self.transaction():
            if self.setting("paused", False):
                return None
            running = self.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('running','uploading')").fetchone()[0]
            if running >= max_workers:
                return None
            row = self.db.execute(
                "SELECT data FROM jobs WHERE status IN ('queued','retry_wait') AND next_attempt_at<=? "
                "ORDER BY queued_at, id LIMIT 1", (time.time(),),
            ).fetchone()
            if row is None:
                return None
            job = json.loads(row[0])
            job.update(status="running", attempts=job["attempts"] + 1, started=time.time(), error="")
            self._write(job)
            return job

    def recover(self):
        with self.transaction():
            for row in self.db.execute("SELECT data FROM jobs WHERE status IN ('running','uploading')").fetchall():
                job = json.loads(row[0])
                job.update(status="queued", next_attempt_at=0, error="Recovered after restart")
                self._write(job)

    def finish(self, job_id, archive_entries, **fields):
        with self.transaction():
            job = self.get(job_id)
            if job["status"] == "cancelled":
                return False
            job.update(status="done", percent=100, percent_str="100%", error="", finished=time.time(), **fields)
            self._write(job)
            self.db.executemany("INSERT OR REPLACE INTO completed VALUES (?,?,?)",
                                [(job["destination"], key, job["finished"]) for key in job["keys"]])
            self.db.executemany("INSERT OR IGNORE INTO archive_entries VALUES (?,?)",
                                [(job["destination"], entry) for entry in archive_entries])
            self.db.executemany("INSERT OR IGNORE INTO completed VALUES (?,?,?)",
                                [(job["destination"], key, job["finished"])
                                 for entry in archive_entries for key in archive_keys(entry)])
            return True

    def archive(self, destination):
        with self.lock:
            return [row[0] for row in self.db.execute("SELECT entry FROM archive_entries WHERE destination=?", (destination,))]

    def import_archive(self, path, destination):
        if not path.is_file():
            return
        entries = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        with self.transaction():
            self.db.executemany("INSERT OR IGNORE INTO archive_entries VALUES (?,?)",
                                [(destination, entry) for entry in entries])
            for entry in entries:
                for key in archive_keys(entry):
                    self.db.execute("INSERT OR IGNORE INTO completed VALUES (?,?,?)",
                                    (destination, key, time.time()))

    def history(self, destination):
        with self.lock:
            done = {row[0] for row in self.db.execute("SELECT key FROM completed WHERE destination=?", (destination,))}
            pending = {row[0] for row in self.db.execute(
                "SELECT k.key FROM job_keys k JOIN jobs j ON j.id=k.job_id WHERE k.destination=? "
                "AND j.status NOT IN ('done','cancelled')", (destination,))}
            return done, pending

    def record_existing(self, destination, keys):
        with self.transaction():
            self.db.executemany("INSERT OR IGNORE INTO completed VALUES (?,?,?)",
                                [(destination, key, time.time()) for key in keys])

    def remove(self, job_id):
        with self.transaction():
            self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def subscriptions(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM subscriptions ORDER BY id")]

    def save_subscription(self, subscription):
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?)",
                            (subscription["id"], json.dumps(subscription)))

    def remove_subscription(self, show_id):
        with self.transaction():
            self.db.execute("DELETE FROM subscriptions WHERE id=?", (show_id,))

    def close(self):
        with self.lock:
            self.db.close()
