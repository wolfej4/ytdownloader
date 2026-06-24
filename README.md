# RT Grabber

A small local web UI around yt-dlp for archiving videos (RTArchive "IA Link",
archive.org pages, or YouTube URLs) into a Jellyfin-friendly folder layout with
embedded + sidecar thumbnails. Runs on your own machine; files are saved to a
folder you choose.

## One-time setup (Windows)

1. Install Python 3.10+ from python.org (tick "Add Python to PATH").
2. Double-click **setup.bat** (installs FastAPI, uvicorn, yt-dlp).
3. Install ffmpeg (needed for thumbnails + metadata):
       winget install Gyan.FFmpeg
   Close and reopen your terminal afterward so it's on PATH.

## Run

Double-click **start.bat**. It launches the server and opens
http://127.0.0.1:8765 in your browser. Close the server window to stop it.

## Use

1. Set your download folder (e.g. C:\Users\you\Videos\RoosterTeeth), click Save.
2. Paste one URL per line, click Start downloads.
3. Watch progress; files land in per-video folders ready for a Jellyfin
   "Home Videos and Photos" library.

The app downloads one item at a time and remembers what it has already grabbed
(.downloaded.txt in the output folder), so you can re-run with more URLs anytime.

## Docker / Portainer

The included Dockerfile bundles Python, ffmpeg, and yt-dlp. The container binds
0.0.0.0 and writes to two mounted volumes:

  /downloads  -> your media folder (point this at the same share Jellyfin reads)
  /config     -> persists config.json across restarts

Edit the host paths in docker-compose.yml, then deploy one of these ways:

1. Portainer -> Stacks -> Add stack -> Repository
   Point it at a Git repo containing this folder. Portainer builds the image
   from the Dockerfile (`build: .`) and starts it.

2. Web editor (no build context)
   On the host: `docker build -t rt-grabber .`
   Then in Portainer's web editor, comment out `build: .`, switch to
   `image: rt-grabber:latest`, and deploy.

3. Plain host: `docker compose up -d`

Open it at http://<host-ip>:8765. The folder field is already set to
/downloads — leave it there so files land on your mounted share.

Notes for Unraid:
- The compose runs as user 99:100 (nobody:users) so files match your shares.
- Pre-create the appdata folder (e.g. /mnt/user/appdata/rt-grabber) before the
  first start, otherwise Docker creates it as root and /config won't be writable.
- To update yt-dlp later, rebuild the image (or `docker exec` in and
  `pip install -U yt-dlp`).

## Notes

- Native mode binds 127.0.0.1 only. The Docker image binds 0.0.0.0 so the
  published port works; put it behind your reverse proxy for remote access
  rather than exposing it directly.
- Host/port/folders are all environment-driven: HOST, PORT, OUTPUT_DIR,
  CONFIG_DIR.
