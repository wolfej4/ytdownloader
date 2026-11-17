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
        # Index compact queue fields so page and summary queries can avoid
        # decoding the JSON payload and logs for every historical job.
        with self.transaction():
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
            for name, definition in (
                ("batch_id", "TEXT NOT NULL DEFAULT ''"),
                ("search_text", "TEXT NOT NULL DEFAULT ''"),
                ("delivered_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            for row in self.db.execute("SELECT data FROM jobs WHERE batch_id=''").fetchall():
                self._write(json.loads(row[0]))
            self.db.execute("CREATE INDEX IF NOT EXISTS job_page_order ON jobs(queued_at DESC, id DESC)")
            self.db.execute("CREATE INDEX IF NOT EXISTS batch_summary ON jobs(batch_id, status, delivered_bytes, queued_at)")
            self.db.execute("CREATE INDEX IF NOT EXISTS job_search ON jobs(search_text, status, queued_at, id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS job_status_summary ON jobs(status, delivered_bytes)")

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
        search_text = " ".join(str(job.get(key) or "") for key in ("title", "show", "url", "error")).casefold()
        self.db.execute(
            "INSERT INTO jobs (id,status,queued_at,next_attempt_at,data,batch_id,search_text,delivered_bytes) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "status=excluded.status, queued_at=excluded.queued_at, "
            "next_attempt_at=excluded.next_attempt_at, data=excluded.data, "
            "batch_id=excluded.batch_id, search_text=excluded.search_text, delivered_bytes=excluded.delivered_bytes",
            (job["id"], job["status"], job["queued_at"], job.get("next_attempt_at", 0), json.dumps(job),
             job["batch_id"], search_text, job.get("delivered_bytes", 0)),
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

    def jobs_by_status(self, statuses):
        if not statuses:
            return []
        with self.lock:
            placeholders = ",".join("?" for _ in statuses)
            return [json.loads(row[0]) for row in self.db.execute(
                f"SELECT data FROM jobs WHERE status IN ({placeholders}) ORDER BY queued_at DESC, id DESC",
                statuses,
            )]

    def job_page(self, q="", status="", offset=0, limit=None):
        clauses, arguments = [], []
        if status:
            clauses.append("status=?")
            arguments.append(status)
        if q:
            clauses.append("instr(search_text, ?) > 0")
            arguments.append(q.casefold())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # Substring searches scan the compact search index, then fetch JSON
        # only for IDs that belong to the requested page.
        source = "jobs INDEXED BY job_search" if q else "jobs"
        with self.lock:
            total = self.db.execute("SELECT COUNT(*) FROM " + source + where, arguments).fetchone()[0]
            selection = "SELECT id FROM " + source + where + " ORDER BY queued_at DESC, id DESC LIMIT ? OFFSET ?"
            rows = self.db.execute(
                "SELECT data FROM jobs WHERE id IN (" + selection + ") ORDER BY queued_at DESC, id DESC",
                (*arguments, limit if limit is not None else -1, offset),
            )
            return [json.loads(row[0]) for row in rows], total

    def summary(self):
        with self.lock:
            counts = {state: 0 for state in (*ACTIVE, *FINISHED)}
            completed_bytes = 0
            for row in self.db.execute("SELECT status,COUNT(*),SUM(delivered_bytes) FROM jobs GROUP BY status"):
                counts[row[0]] = row[1]
                if row[0] == "done":
                    completed_bytes = row[2] or 0
            return counts, completed_bytes

    def batches(self, batch_id=None, limit=None):
        where = " WHERE batch_id=?" if batch_id is not None else ""
        arguments = (batch_id,) if batch_id is not None else ()
        with self.lock:
            rows = self.db.execute(
                "SELECT batch_id AS id,COUNT(*) AS total,"
                "SUM(status='done') AS done,SUM(status='error') AS error,"
                "SUM(status='cancelled') AS cancelled,"
                "SUM(status NOT IN ('done','error','cancelled')) AS active,"
                "SUM(delivered_bytes) AS bytes,MAX(queued_at) AS queued_at FROM jobs"
                + where + " GROUP BY batch_id ORDER BY queued_at DESC,id DESC LIMIT ?",
                (*arguments, limit if limit is not None else -1),
            )
            return [dict(row) for row in rows]

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
