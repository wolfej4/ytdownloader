"""Regression tests for durable jobs, delivery, catalog paging, and followed shows."""

import importlib.util
import json
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

# Import the service without starting workers or touching an existing configuration.
_IMPORT_DIRECTORY = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"CONFIG_DIR": _IMPORT_DIRECTORY.name,
                             "OUTPUT_DIR": _IMPORT_DIRECTORY.name + "/media",
                             "STAGING_DIR": _IMPORT_DIRECTORY.name + "/stage"}):
    import app

from downloads import DownloadManager, Interrupted, destination_id
from state import JobStore


class ArchiveWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = {**app.DEFAULTS, "output_dir": str(self.root / "output"),
                       "staging_dir": str(self.root / "staging"), "min_free_gb": 0,
                       "max_retries": 2, "ntfy_url": "", "pool_status_file": ""}
        self.store = JobStore(self.root / "config" / "state.sqlite3")
        self.manager = DownloadManager(self.store, self.config)
        self.globals = patch.multiple(app, config=self.config, store=self.store, manager=self.manager,
                                      CONFIG_DIR=self.root / "config", CONFIG_PATH=self.root / "config" / "config.json")
        self.globals.start()
        self.client = TestClient(app.app)

    def tearDown(self):
        self.globals.stop()
        self.store.close()
        self.temporary.cleanup()

    def enqueue(self, url="https://archive.org/details/roosterteeth-123", **fields):
        item = app.DownloadItem(url=url, **fields)
        result = self.store.enqueue(app.prepare_items([item]))
        return result["created"][0]

    def fake_download(self, job, media, archive):
        target = media / "Example" / "Season 02" / "S02E03 - Title [roosterteeth-123].mkv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video bytes")
        target.with_suffix(".info.json").write_text(json.dumps({"title": "Extracted title", "upload_date": "20261117"}))
        archive.write_text(archive.read_text() + "archive.org roosterteeth-123\n")

    def test_concurrent_enqueue_and_claim_are_unique(self):
        prepared = app.prepare_items([app.DownloadItem(url="https://youtu.be/video-id")])
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _: self.store.enqueue(prepared), range(20)))
        self.assertEqual(sum(len(result["created"]) for result in results), 1)
        with ThreadPoolExecutor(max_workers=8) as workers:
            claims = list(workers.map(lambda _: self.store.claim(3), range(20)))
        self.assertEqual(sum(job is not None for job in claims), 1)

    def test_history_survives_clearing_and_cross_source_aliases(self):
        jid = self.enqueue(keys=["ia:youtube-video-id", "youtube:video-id"])
        self.store.finish(jid, ["archive.org roosterteeth-123"])
        self.store.remove(jid)
        result = self.store.enqueue(app.prepare_items([app.DownloadItem(url="https://youtu.be/video-id")]))
        self.assertEqual(result["created"], [])
        self.assertEqual(result["skipped"], 1)
        self.config["output_dir"] = str(self.root / "another-destination")
        result = self.store.enqueue(app.prepare_items([app.DownloadItem(url="https://youtu.be/video-id")]))
        self.assertEqual(len(result["created"]), 1)

    def test_legacy_archive_and_playlist_entries_populate_history(self):
        archive = self.root / ".downloaded.txt"
        archive.write_text("youtube older-video\narchive.org roosterteeth-older\n")
        destination = destination_id(app.current_target())
        self.store.import_archive(archive, destination)
        done, _ = self.store.history(destination)
        self.assertTrue({"youtube:older-video", "ia:youtube-older-video", "ia:roosterteeth-older"} <= done)
        jid = self.enqueue(url="https://www.youtube.com/playlist?list=list-id")
        self.store.finish(jid, ["youtube child-video"])
        self.assertIn("youtube:child-video", self.store.history(destination)[0])

    def test_reopen_recovers_running_upload_without_redownloading(self):
        jid = self.enqueue(show="Example", season=2, episode=3)
        job = self.store.claim(3)
        media = self.root / "staging" / jid / "media"
        media.mkdir(parents=True)
        archive = media.parent / "archive.txt"
        archive.write_text("")
        self.fake_download(job, media, archive)
        self.store.update(jid, status="uploading", download_complete=True,
                          video_files=[str(p.relative_to(media)) for p in media.rglob("*.mkv")])
        self.store.close()
        self.store = JobStore(self.root / "config" / "state.sqlite3")
        app.store = self.store
        self.manager.store = self.store
        self.store.recover()
        with patch.object(self.manager, "download") as download:
            self.manager.run_job(self.store.claim(3))
            download.assert_not_called()
        self.assertEqual(self.store.get(jid)["status"], "done")
        self.assertTrue((self.root / "output" / "Example" / "Season 02" / "S02E03 - Title [roosterteeth-123].mkv").is_file())

    def test_upload_failure_keeps_media_then_retry_delivers(self):
        jid = self.enqueue(show="Example", season=2, episode=3, title="A & B", description="<p>A description</p>")
        with patch.object(self.manager, "download", side_effect=self.fake_download), patch.object(self.manager, "deliver", side_effect=OSError("SMB offline")):
            self.manager.run_job(self.store.claim(3))
        job = self.store.get(jid)
        self.assertEqual(job["status"], "retry_wait")
        self.assertTrue(job["download_complete"])
        self.assertTrue((self.root / "staging" / jid / "media").is_dir())
        self.assertFalse(self.store.history(job["destination"])[0])
        self.store.update(jid, next_attempt_at=0)
        with patch.object(self.manager, "download") as download:
            self.manager.run_job(self.store.claim(3))
            download.assert_not_called()
        self.assertEqual(self.store.get(jid)["status"], "done")
        nfo = self.root / "output" / "Example" / "Season 02" / "S02E03 - Title [roosterteeth-123].nfo"
        metadata = ET.parse(nfo).getroot()
        self.assertEqual(metadata.tag, "episodedetails")
        self.assertEqual(metadata.findtext("title"), "A & B")
        self.assertEqual(metadata.findtext("plot"), "A description")
        self.assertEqual(metadata.findtext("aired"), "2026-11-17")
        self.assertFalse((self.root / "staging" / jid).exists())

    def test_emergency_read_only_pauses_and_preserves_work(self):
        jid = self.enqueue()
        with patch.object(self.manager, "check_storage", side_effect=OSError(30, "Read-only file system")):
            self.manager.run_job(self.store.claim(3))
        self.assertEqual(self.store.get(jid)["status"], "paused")
        self.assertTrue(self.store.setting("paused"))
        self.assertIsNone(self.store.claim(3))
        self.manager.resume()
        self.assertEqual(self.store.get(jid)["status"], "queued")

    def test_pause_recovery_and_cancel_control(self):
        jid = self.enqueue()
        job = self.store.claim(3)
        self.manager.pause()
        with self.assertRaises(Interrupted):
            self.manager.control(jid)
        self.store.update(jid, status="paused")
        self.manager.resume()
        self.assertFalse(self.store.setting("paused"))
        self.assertEqual(self.store.get(jid)["status"], "queued")
        self.client.post(f"/api/jobs/{jid}/cancel")
        with self.assertRaises(Interrupted):
            self.manager.control(jid)
        self.assertEqual(self.client.post(f"/api/jobs/{jid}/retry").status_code, 200)
        with self.manager.process_lock:
            self.manager.in_flight.add(jid)
        self.store.update(jid, status="cancelled")
        self.assertEqual(self.client.post(f"/api/jobs/{jid}/retry").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/jobs/{jid}").status_code, 409)

    def test_pool_status_rejects_full_stale_or_invalid_reports(self):
        path = self.root / "pool.json"
        self.config["pool_status_file"] = str(path)
        status = {"checked_at": time.time(), "pool": "pve/data", "data_percent": 100, "metadata_percent": 3.46}
        path.write_text(json.dumps(status))
        with self.assertRaisesRegex(OSError, "data_percent"):
            self.manager.check_storage()
        status.update(data_percent=50, checked_at=time.time()-181)
        path.write_text(json.dumps(status))
        with self.assertRaisesRegex(OSError, "3 minutes"):
            self.manager.check_storage()
        status["checked_at"] = time.time()
        path.write_text(json.dumps(status))
        self.assertTrue(self.manager.check_storage())
        path.write_text("not JSON")
        with self.assertRaises(OSError):
            self.manager.check_storage()

    def test_catalog_pages_beyond_5000_have_exclusive_document_cursors(self):
        def query(body):
            structured = body["structuredQuery"]
            start = 0
            if "startAt" in structured:
                self.assertFalse(structured["startAt"]["before"])
                name = structured["startAt"]["values"][0]["referenceValue"]
                self.assertTrue(name.startswith("projects/rt-archive/"))
                start = int(name.rsplit("/", 1)[1]) + 1
            return [{"id": str(i), "title": str(i), "platform": "roosterteeth", "ai_id": f"roosterteeth-{i}", "date": i}
                    for i in range(start, min(5011, start + structured["limit"]))]
        with patch.object(app, "get_shows", return_value=[]), patch.object(app, "_run_firestore_query", side_effect=query):
            first = self.client.get("/api/browse/shows/example/episodes").json()
            second = self.client.get("/api/browse/shows/example/episodes", params={"cursor": first["next_cursor"]}).json()
        self.assertEqual(len(first["episodes"]), 5000)
        self.assertEqual(len(second["episodes"]), 11)
        self.assertIsNone(second["next_cursor"])
        self.assertFalse({ep["id"] for ep in first["episodes"]} & {ep["id"] for ep in second["episodes"]})
        invalid = self.client.get("/api/browse/shows/example/episodes?cursor=invalid")
        self.assertEqual(invalid.status_code, 400)

    def test_follow_baseline_new_archive_and_no_duplicate_notifications(self):
        self.client.post("/api/follows", json={"show_id": "example", "title": "Example", "auto_queue": True})
        documents = [{"id": "old", "platform": "roosterteeth", "ai_id": "roosterteeth-old"}]
        with patch.object(app, "_catalog_page", side_effect=lambda *args: (documents, None)), patch.object(self.manager, "notify") as notify:
            app.check_follows(force=True)
            self.assertEqual(self.store.jobs(), [])
            documents.append({"id": "new", "platform": "roosterteeth", "ai_id": "roosterteeth-new"})
            app.check_follows(force=True)
            app.check_follows(force=True)
            self.assertEqual(len(self.store.jobs()), 1)
            notify.assert_called_once()
        subscription = self.store.subscriptions()[0]
        self.assertEqual(len(subscription["known_keys"]), 2)

    def test_settings_preserve_secrets_and_jobs_snapshot_destination(self):
        self.config.update(smb={"host": "server", "share": "media", "password": "secret"}, ntfy_token="ntfy-secret")
        result = self.client.get("/api/config")
        self.assertNotIn("secret", result.text)
        jid = self.enqueue()
        jobs = self.client.get("/api/jobs")
        self.assertNotIn("secret", jobs.text)
        self.config["smb"]["share"] = "elsewhere"
        self.assertEqual(self.store.get(jid)["target"]["share"], "media")
        self.assertEqual(self.client.post("/api/config", json={"bandwidth_limit": "--bad"}).status_code, 400)
        self.assertEqual(self.client.post("/api/config", json={"bandwidth_limit": "5M"}).status_code, 200)

    def test_smb_copy_verifies_then_renames_and_keeps_source(self):
        jid = self.enqueue()
        self.store.update(jid, target={"kind": "smb", "host": "server", "share": "media"})
        job = self.store.get(jid)
        media = self.root / "source"
        media.mkdir()
        original = media / "video.mkv"
        original.write_bytes(b"original")
        copied = self.root / "copied.part"
        with patch("downloads.smbclient.register_session"), patch("downloads.smbclient.makedirs"), \
             patch("downloads.smbclient.open_file", side_effect=lambda *args, **kwargs: copied.open("wb")), \
             patch("downloads.smbclient.stat", side_effect=lambda *args: copied.stat()), \
             patch("downloads.smbclient.replace") as replace:
            self.manager.deliver(job, media)
            replace.assert_called_once()
            self.assertTrue(replace.call_args.args[0].endswith(".part"))
        self.assertEqual(original.read_bytes(), b"original")
        self.assertEqual(copied.read_bytes(), b"original")

    def test_5000_jobs_persist_and_dashboard_pages(self):
        items = [app.DownloadItem(url=f"https://archive.org/details/roosterteeth-{i}") for i in range(5000)]
        result = self.store.enqueue(app.prepare_items(items))
        self.assertEqual(len(result["created"]), 5000)
        first = self.client.get("/api/jobs?limit=50").json()
        self.assertEqual(len(first["jobs"]), 50)
        self.assertEqual(first["total"], 5000)
        self.assertEqual(first["counts"]["queued"], 5000)
        self.assertEqual(len(self.client.get("/api/batches").json()["batches"]), 1)

    def test_low_space_stops_before_download(self):
        jid = self.enqueue()
        self.config["min_free_gb"] = 100000
        with patch.object(self.manager, "download") as download:
            self.manager.run_job(self.store.claim(3))
            download.assert_not_called()
        self.assertEqual(self.store.get(jid)["status"], "paused")
        self.assertIn("reserve", self.store.setting("pause_reason"))

    def test_cancellation_wins_over_retry_and_completion(self):
        jid = self.enqueue()
        job = self.store.claim(3)
        self.store.update(jid, status="cancelled")
        self.manager.fail(job, RuntimeError("temporary failure"))
        self.assertEqual(self.store.get(jid)["status"], "cancelled")
        self.assertFalse(self.store.finish(jid, ["youtube should-not-complete"]))
        self.assertNotIn("youtube:should-not-complete", self.store.history(job["destination"])[0])

    def test_smb_size_mismatch_preserves_original_and_never_publishes(self):
        jid = self.enqueue()
        job = self.store.get(jid)
        job["target"] = {"kind": "smb", "host": "server", "share": "media"}
        media = self.root / "source"
        media.mkdir()
        (media / "video.mkv").write_bytes(b"original")
        temporary = self.root / "copy"
        with patch("downloads.smbclient.register_session"), patch("downloads.smbclient.makedirs"), \
             patch("downloads.smbclient.open_file", side_effect=lambda *args, **kwargs: temporary.open("wb")), \
             patch("downloads.smbclient.stat", return_value=Mock(st_size=1)), \
             patch("downloads.smbclient.replace") as replace:
            with self.assertRaisesRegex(RuntimeError, "size mismatch"):
                self.manager.deliver(job, media)
            replace.assert_not_called()
        self.assertTrue((media / "video.mkv").exists())

    def test_notification_uses_saved_token_and_failure_preserves_job_success(self):
        self.config.update(ntfy_url="https://ntfy.example.com/test", ntfy_token="private-token")
        response = Mock()
        with patch("downloads.requests.post", return_value=response) as post:
            self.assertTrue(self.manager.notify("Batch finished", "Message"))
            self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer private-token")
            self.assertEqual(post.call_args.kwargs["data"], b"Message")
        import requests
        with patch("downloads.requests.post", side_effect=requests.Timeout("temporarily unavailable")):
            self.assertFalse(self.manager.notify("Batch finished", "Message"))
            self.assertIn("temporarily unavailable", self.manager.last_notification_error)

    def test_pool_exporter_uses_lvm_report_and_writes_error_status(self):
        spec = importlib.util.spec_from_file_location("pool_exporter", Path(__file__).resolve().parents[1] / "scripts" / "report-thin-pool.py")
        exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(exporter)
        report = {"report": [{"lv": [{"lv_attr": "twi-aotz--", "data_percent": "75.20", "metadata_percent": "3.40"}]}]}
        destination = self.root / "shared" / "pool.json"
        with patch.object(exporter.subprocess, "run", return_value=Mock(stdout=json.dumps(report))):
            result = exporter.report("pve/data", destination)
        self.assertEqual(result["data_percent"], 75.2)
        self.assertEqual(json.loads(destination.read_text())["error"], "")
        with patch.object(exporter.subprocess, "run", side_effect=OSError("lvs unavailable")):
            result = exporter.report("pve/data", destination)
        self.assertEqual(result["error"], "lvs unavailable")

    def test_missing_staging_never_completes_or_records_history(self):
        jid = self.enqueue()
        self.store.update(jid, download_complete=True, video_files=["missing.mkv"])
        self.manager.run_job(self.store.claim(3))
        job = self.store.get(jid)
        self.assertEqual(job["status"], "retry_wait")
        self.assertIn("missing", job["error"])
        self.assertEqual(self.store.history(job["destination"])[0], set())

    def test_manual_queue_defaults_to_redownload_and_blocks_existing_jobs(self):
        jid = self.enqueue(keys=["ia:youtube-mirror", "youtube:mirror"])
        self.store.finish(jid, ["archive.org roosterteeth-123"])
        payload = {"items": [{"url": "https://youtu.be/mirror", "keys": ["ia:roosterteeth-123"]}], "redownload": False}
        normal = self.client.post("/api/download", json=payload).json()
        self.assertEqual(normal["created"], [])
        payload.pop("redownload")
        fresh = self.client.post("/api/download", json=payload).json()
        self.assertEqual(len(fresh["created"]), 1)
        job = self.store.get(fresh["created"][0])
        self.assertTrue(job["redownload"])
        self.assertEqual(job["target"], app.current_target())
        duplicate = self.client.post("/api/download", json=payload).json()
        self.assertEqual(duplicate["created"], [])
        self.assertEqual(duplicate["skipped"], 1)
        self.assertEqual(self.store.get(jid)["status"], "done")

    def test_default_redownload_uses_fresh_archive_and_overwrites_existing_file(self):
        original = self.enqueue(show="Example", season=2, episode=3)
        self.store.finish(original, ["archive.org roosterteeth-123"])
        output = self.root / "output" / "Example" / "Season 02" / "S02E03 - Title [roosterteeth-123].mkv"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"old video contents")
        result = self.client.post("/api/download", json={"items": [{
            "url": "https://archive.org/details/roosterteeth-123", "show": "Example", "season": 2, "episode": 3,
        }]}).json()
        fresh_id = result["created"][0]

        def download(job, media, archive):
            self.assertEqual(archive.read_text().strip(), "")
            self.fake_download(job, media, archive)

        with patch.object(self.manager, "download", side_effect=download):
            self.manager.run_job(self.store.claim(3))
        self.assertEqual(self.store.get(fresh_id)["status"], "done")
        self.assertEqual(output.read_bytes(), b"video bytes")
        self.assertEqual(self.store.get(original)["status"], "done")

    def test_pasted_urls_default_to_redownload_after_restart(self):
        jid = self.enqueue()
        self.store.finish(jid, ["archive.org roosterteeth-123"])
        result = self.client.post("/api/download", json={
            "urls": ["https://archive.org/details/roosterteeth-123"],
        }).json()
        fresh_id = result["created"][0]
        self.store.close()
        self.store = JobStore(self.root / "config" / "state.sqlite3")
        app.store = self.store
        self.manager.store = self.store
        self.store.recover()
        self.assertTrue(self.store.get(fresh_id)["redownload"])
        self.assertEqual(self.store.get(fresh_id)["status"], "queued")


if __name__ == "__main__":
    unittest.main()
