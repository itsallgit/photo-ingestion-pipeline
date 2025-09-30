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

1. Install Docker and Docker Compose on your machine:
   - Docker Desktop for Windows/Mac: https://www.docker.com/get-started
   - Linux: follow your distro instructions.

2. From the project root (where `docker/` and `backend/` are located), run:

```bash
# Build and start the service (maps ./data on host to /app/data in container)
docker-compose -f docker/docker-compose.yml up --build
```

3. Open your browser to `http://localhost:8000`. The web UI will be served from the FastAPI app.

## How to use
1. Prepare a temporary folder on your host containing photos you want to ingest (e.g. `/home/user/tmp_photos`).
2. Make that folder available inside the container by placing it under the `data` mount, or adjust docker-compose volumes as needed. By default `docker-compose.yml` mounts `../data` from the repository root into `/app/data` in the container. You can copy your temp folder into `./data/incoming` prior to starting the container:

   ```bash
   mkdir -p data/incoming
   cp -r /path/to/my/photos/* data/incoming/
   ```

3. In the UI, set the **source folder** to `/app/data/incoming` (or `/data/incoming` if you used the suggested mount — the UI will accept the container path).
4. Click **Scan folders**. The UI will list nested folders and photo counts.
5. For each folder, assign custom tags (comma-separated). If you leave all tags blank, Wave 1 will auto-complete and not modify any files.
6. Click **Create Session**. A session is created and shown in the Sessions panel.
7. Click **Start Ingestion** to begin Wave 1. Watch the Live log or session listing for progress.
8. When the session completes, the IPTC:Keywords of the images in the folder will have been updated in-place by `exiftool`.

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

## Support
If you encounter issues, check `data/logs/` for session logs, and the container logs via `docker-compose logs backend`.

## Development TODO List

1. Global tags empty post session
2. Update wave1 worker to show status in UI under session files
3. Update UI to show completed session
4. Move Live log in UI under Current session info
5. Fix live log not showing tags (but shows second tag)

