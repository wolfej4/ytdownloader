# RT Grabber

RT Grabber downloads videos from RT Archive’s Internet Archive links, archive.org pages, and YouTube URLs. It provides a local web interface, a persistent queue, completed-download history, and delivery to a local folder or SMB share.

## Docker setup

Install Docker Engine with the Compose plugin. Clone the repository and edit the host paths in `docker-compose.yml`.

```bash
git clone https://github.com/wolfej4/ytdownloader.git
cd ytdownloader
docker compose up -d --build
```

Open `http://YOUR-DOCKER-HOST:8765`. The Docker image includes Python, yt-dlp, and ffmpeg. It uses three persistent mounts.

| Container path | Contents |
| --- | --- |
| `/config` | Settings, SQLite queue, completed history, and followed shows |
| `/staging` | Partial downloads, completed files awaiting delivery, and per-job yt-dlp archives |
| `/downloads` | Final videos when using local storage |

The supplied Compose file uses example Unraid host paths. Replace the left side of each mount for your server. Pre-create those host directories and give the container’s operating-system user read/write access. The image runs as root by default. To use Unraid’s nobody account, add `user: "99:100"` under the service and give that account access to all three mounts.

Put `/staging` on a disk-backed mount with sufficient capacity for the active workers. A job can contain a whole playlist, so its staging requirement can exceed the size of one episode. Staging on an Unraid share accessed through an existing mount is also possible. Completed staging data is removed only after successful delivery and history recording. Retained failed jobs continue to consume space until retried or removed.

In Portainer or Dockhand, deploy this folder as a Compose project with its repository/build context available. Rebuild the image when updating code or yt-dlp. A Compose web editor needs an image you have already built or a repository deployment with build context.

## Native setup

Install Python 3.10 or later and ffmpeg, including `ffprobe` if you use it independently to inspect your library. Run these commands in the repository folder.

```bash
python -m venv .venv
```

Activate `.venv` with `source .venv/bin/activate` on Linux/macOS, or `.venv\Scripts\activate` in Windows Command Prompt. Then install and launch the app.

```bash
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8765`. On Windows, `winget install Gyan.FFmpeg` is one way to install ffmpeg. Reopen the terminal after installation. Native configuration defaults to this repository folder, staging to its `staging` subdirectory, and final videos to your Videos/RoosterTeeth folder. You can change the paths in the UI.

## Downloading an archive

1. Save your local download folder or connect an SMB destination. The SMB host, share, credentials, and optional subdirectory are saved in `/config/config.json`.
2. Set the staging folder, free-space reserve, worker count, retries, and optional bandwidth limit. Defaults are three workers, a 5 GiB reserve per checked filesystem, three automatic job retries, and an initial retry delay of ten seconds.
3. Paste one URL per line, or use Browse RT Archive to open a show. A show loads in pages of 500 catalog records. Use **Load next page** or **Load all remaining** to browse beyond 5,000 records.
4. Filter the loaded records by title, season, year, available source, or download status. Title search against the remote catalog is a case-sensitive prefix search with common capitalization variants. Local title filtering matches anywhere in loaded titles.
5. Choose Website or YouTube globally, with per-episode overrides. The other available source is selected when the preferred source is absent. **Select all** selects matching loaded episodes across display pages. **Select missing** selects matching loaded episodes with neither completed history nor an existing job. Use **Queue selected** to submit them.
6. Follow progress in Transfers or Batch progress. Search transfer titles, shows, URLs, and errors. Logs, retry times, byte progress, and per-job controls remain available after restart.

Filters operate on loaded catalog records. Load all pages before selecting an entire show. The displayed loaded, matching, and selected counts make the selection scope visible. Some catalog episodes have no archived source and cannot be queued. Website and YouTube mirrors are merged when their catalog links identify the same episode.

## Recovery, duplicate handling, and delivery

SQLite at `/config/state.sqlite3` is the queue and history authority. Jobs, source aliases, destination snapshots, retry state, and followed shows survive app and container restarts. Interrupted downloads are requeued and yt-dlp resumes compatible partial files from their staging directory. Interrupted uploads reuse the completed local files.

**Pause queue** interrupts active downloads and cooperatively stops file copies. Partial files remain available for **Resume queue**. A pause persists across restarts. Resume checks storage before releasing jobs. Transient failures retry with increasing delays capped at five minutes. Permanently unavailable/private videos and unsupported URLs require intervention. The per-job Retry button resets its retry budget.

A job keeps the destination, SMB credentials, and staging location captured when it was queued. Changes to settings apply to newly queued jobs. Retrying a job continues delivery to its original destination. Keep its original mount available while recovering work.

Local and SMB deliveries write each file to a temporary destination name, compare its size with the staged original, and then replace the final filename. Failed copies retain the staged originals. A job enters completed history only after delivery succeeds. Video, JSON metadata, NFO, and artwork files can be copied again after an interrupted upload. Verification checks file size; a full media-decoding integrity scan is a separate operation.

Duplicate checks use source identifiers, known Website/YouTube aliases, and the destination. Existing queued or failed jobs are skipped when submitting the same episode again. Use Retry on its existing row. A different destination can receive its own copy. Clearing or removing completed transfer rows preserves completed history. Removing a failed or cancelled job also discards its staged files.

Each job uses yt-dlp’s `--download-archive` with a private archive file. Successful entries are merged transactionally into SQLite after delivery. The app imports a legacy local `.downloaded.txt` file at startup when present. To recognize older downloads without a ledger, run **Scan existing downloads into history**. This scans filenames containing known `[roosterteeth-…]` or `[youtube-…]` archive identifiers. Files with other names require their original archive/history data. Reload a show after a scan to refresh its download status.

Earlier versions kept their queue in memory and did not consistently configure the documented `.downloaded.txt` archive. Those past in-memory jobs cannot be reconstructed automatically. Existing media stays available for the history scan. New jobs use season subfolders; earlier folders remain where they were created.

## Jellyfin files and artwork

Numbered show episodes use this layout.

```text
Show Title/
  tvshow.nfo
  Season 02/
    S02E03 - Episode Title [source-id].mkv
    S02E03 - Episode Title [source-id].nfo
    S02E03 - Episode Title [source-id].info.json
    S02E03 - Episode Title [source-id].jpg
```

The NFO contains the supplied or extracted title, description, release date, show, season, episode, and source identifier when available. Unknown season and episode numbers are left unspecified. Direct URL downloads use extracted metadata and can be organized in a Home Videos and Photos library. Add numbered shows to a Jellyfin Shows library. Embedded thumbnails and the thumbnail backfill tool are retained.

**Scan for missing thumbnails** looks for videos with archive identifiers, adds missing JPG sidecars, and fetches available catalog artwork. Its status reports files checked, additions, and failures. Catalog and Internet Archive availability determine which artwork can be retrieved.

## Following shows and ntfy

Browse to a show, choose a check interval and the **Queue newly archived episodes** option, then click **Follow this show**. The default interval is six hours. The first successful check records the current archived catalog as a baseline. Later checks detect newly archived episodes, including previously unavailable episodes that acquire an archive link.

Automatic queuing starts disabled. Enabling it queues newly detected episodes using the selected source preference and the current destination settings. Existing episodes can be queued through the browser. Follow the same show again to update its preference, interval, or automatic queue setting. **Check now** checks followed shows immediately. **Unfollow** removes its subscription.

Save a complete ntfy topic URL, such as `https://ntfy.example.com/rt-grabber`, and an access token if your server requires it. Leave the token field blank to retain the saved token, or use **Clear saved token**. **Send test notification** verifies the saved configuration. Notifications cover new episodes, final job failures, storage pauses, and finished batches. Notification failures appear in the interface and server logs without changing a successful download into a failure. An empty topic URL disables publishing.

## Storage checks and Proxmox

The app checks staging and local destination free space and probes actual writability before starting a job and every three seconds while transfers are active. Low space or a failed write pauses the queue. The reserve is a threshold rather than a reservation for the eventual download size. Keep enough headroom for all workers, copies, and post-processing.

The free space inside an LXC filesystem can differ from the capacity of its Proxmox LVM thin pool. Use the optional node exporter to provide pool data and metadata percentages to RT Grabber.

1. Copy `scripts/report-thin-pool.py` to the Proxmox node that hosts the pool.
2. Choose an existing shared directory visible to both that node and the Docker guest. Make its status directory available to RT Grabber as a read-only bind mount, for example `/monitor`. The actual host paths depend on your existing LXC/shared-storage mounts.
3. Run the exporter on the Proxmox node once a minute, using cron or your scheduler. Replace the output path below with that shared directory.

```bash
python3 /path/to/report-thin-pool.py --pool pve/data --output /path/to/shared/pool-status/pve-data.json
```

4. In Compose, enable the example status-directory mount and set `POOL_STATUS_FILE=/monitor/pve-data.json`, or save that path in Storage settings. The default pause threshold is 95% for either data or metadata usage. An unreadable, invalid, failed, or more-than-three-minute-old report blocks new work and pauses active transfers.

This feed provides a pause signal. Pool expansion, reclaiming space, filesystem repair, and checking other affected guests remain Proxmox administration tasks. Configure node-level alerts as well. Restore pool capacity and repair affected filesystems before resuming an emergency-read-only guest. The app validates writes even when a mount reports `rw`.

## Updating and backups

Back up `/config` and preserve `/staging` for interrupted jobs. Stop RT Grabber when copying its SQLite database, or use SQLite’s backup facilities to produce a consistent live backup. A raw copy of just `state.sqlite3` while its WAL is active can omit recent transactions. Protect configuration and database backups because saved SMB credentials and notification tokens are stored there.

Update from your checked-out deployment branch and rebuild.

```bash
git pull --ff-only
docker compose up -d --build
```

The supplied Docker health check tests the API. The storage status panel checks write availability. Run a single app process per `/config` database and staging mount. Uvicorn’s multi-worker/reload modes are unsuitable for this background worker model.

## Environment variables

| Variable | Default or purpose |
| --- | --- |
| `HOST` | `127.0.0.1` natively, `0.0.0.0` in Docker |
| `PORT` | `8765` |
| `CONFIG_DIR` | Repository folder natively, `/config` in Docker |
| `OUTPUT_DIR` | Initial local output folder, `/downloads` in Docker |
| `STAGING_DIR` | Initial staging folder, `/staging` in Docker |
| `POOL_STATUS_FILE` | Optional initial path to the node-exported pool status JSON |
| `YT_DLP_BIN` | `yt-dlp` |
| `FFMPEG_BIN` | `ffmpeg` for artwork conversion |
| `LOG_LEVEL` | `INFO` |

Saved UI settings take precedence over initial environment defaults. The Docker image also directs yt-dlp’s cache to `/staging/cache`. Remote access should use your authenticated reverse proxy. API responses hide saved passwords and tokens, and the application has no built-in user login.

## Development checks

Install the normal requirements plus `httpx` for FastAPI’s test client, then run the regression suite.

```bash
python -m pip install -r requirements.txt httpx
python -m unittest discover -s tests -v
```
