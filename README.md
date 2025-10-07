# Photo Ingestion Pipeline — MVP (Wave 1)

This project is a locally-run photo ingestion pipeline designed to tag photos using IPTC:Keywords (via `exiftool`) in a fully local, portable Docker container. This MVP implements **Wave 1** (custom tags based on folder mappings). The backend is a FastAPI application that serves a small Vue-based frontend (loaded via CDN) as static files.

## Features (MVP - Wave 1)
- Create ingestion sessions by pointing the app at a local folder containing photos.
- Scan nested folders and assign custom tag(s) to each folder (UI).
- Apply custom tags to photos' IPTC:Keywords using `exiftool`.
- Maintain session state and idempotency (per-session JSON files under `data/`).
- Global `tags.json` containing all discovered tags and counts.
- Live log streaming via WebSocket during ingestion.
- All persistent data stored under the `data/` folder so the app can be moved between machines.

## Project layout
- `backend/` — FastAPI application and static frontend.
- `docker/` — Dockerfile and docker-compose manifest.
- `data/` — Persistent runtime data (created at first run or mounted from host).


## Quickstart (Docker)
The recommended way to run the app is using Docker & docker-compose.


**Important:** Before starting the container, make sure you have a `data` directory and an `INGESTION_TARGET` directory at the project root. All session, tag, and log files will be stored in `data/`, and all photos to be tagged should be copied into `INGESTION_TARGET/`. If these directories don't exist, create them:

```bash
mkdir data
mkdir INGESTION_TARGET
```

1. Install Docker and Docker Compose on your machine:
   - Docker Desktop for Windows/Mac: https://www.docker.com/get-started
   - Linux: follow your distro instructions.


2. From the project root (where `docker/`, `backend/`, and `INGESTION_TARGET/` are located), run:

```bash
# Build and start the service (maps ./data on host to /app/data and ./INGESTION_TARGET to /app/INGESTION_TARGET in container)
docker-compose -f docker/docker-compose.yml up --build
```

3. Open your browser to `http://localhost:8000`. The web UI will be served from the FastAPI app.

## How to use

1. Copy all photos you want to tag into the `INGESTION_TARGET` directory at the project root (e.g. `C:\Users\me\photo-ingestion-pipeline\INGESTION_TARGET`).
2. Start the container as described above. The `INGESTION_TARGET` directory will be available inside the container as `/app/INGESTION_TARGET`.
3. In the UI, click **Scan folders**. The app will automatically scan the Target Directory and list nested folders and photo counts.
4. For each folder, assign custom tags (comma-separated). If you leave all tags blank, Wave 1 will auto-complete and not modify any files.
5. Click **Create Session**. A session is created and shown in the Sessions panel.
6. Click **Start Ingestion** to begin Wave 1. Watch the Live log or session listing for progress.
7. When the session completes, the IPTC:Keywords of the images in the folder will have been updated in-place by `exiftool`.

## Data storage and portability
- All persistent state (sessions, tags, logs) is stored inside the `data/` directory at the repository root and is mounted into the container as `/app/data`. Copy the entire project directory (including `data/`) to another machine to preserve the application state.

## Notes & troubleshooting
- The container installs `exiftool` (libimage-exiftool-perl) during image build. If exiftool is missing at runtime, check container logs for build errors.
- This MVP focuses only on Wave 1 (folder-based custom tags). Subsequent waves (location, people, objects) will be added iteratively.
- The frontend is a minimal Vue app loaded via CDN; no Node build step is required.

## Files created during runtime
- `data/sessions/session-<ts>-<id>.json` — session metadata
- `data/sessions/session-<ts>-<id>-files.json` — per-file state for the session
- `data/logs/session-<ts>-<id>.log` — session log
- `data/tags.json` — global tags and counts

## Development
If you wish to run the backend directly without Docker:

1. Create a Python 3.11 virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r backend/requirements.txt
   ```
2. Make sure `exiftool` is installed and available in your PATH.
3. Run:
   ```bash
   cd backend
   uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

## Updating the Application

For any changes (frontend, backend, or dependencies), use this single command from the project root to tear down containers, force a clean image build (no cache), and recreate containers:

```bash
docker-compose -f docker/docker-compose.yml down --remove-orphans \
   && docker-compose -f docker/docker-compose.yml build --no-cache --pull \
   && docker-compose -f docker/docker-compose.yml up -d --force-recreate --remove-orphans
```

Notes:
- This ensures COPY layers (including `backend/static/main.css`) reflect your latest files.
- The `data/` volume is preserved across rebuilds; do not run `docker-compose down -v` unless you want to delete persisted data.
- If you still see stale assets in the browser after rebuilding, clear your browser cache or hard-reload (Ctrl+Shift+R), and verify the server is serving the updated file:

```bash
curl -sS http://localhost:8000/ui/main.css | sed -n '1,40p'
```

If the in-container file still appears stale, inspect the file inside the backend container:

```bash
docker-compose -f docker/docker-compose.yml exec backend sh -c 'sed -n "1,40p" /app/static/main.css'
```

Finally, check `docker-compose.yml` for any host bind-mounts that overlay `/app` inside the container; if a bind-mount exists, the container will serve files directly from your host path (so rebuilding the image won't change what the container serves).

## Support
If you encounter issues, check `data/logs/` for session logs, and the container logs via `docker-compose logs backend`.



## TODO
- Export global tags working but UI display of tags doesn't show tag key.
- Wave 1 status per folder not updating correctly.
- Batch exiftool for folders, currently running image by image (but working).
- Check log file length mismatch error when starting ingestion.