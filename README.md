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

When working on the application, you'll need different commands depending on what you're updating:

### 1. Frontend Changes (HTML/JavaScript/CSS)
For changes to files in `backend/static/`:
```bash
# Stop the container but keep the volume
docker-compose -f docker/docker-compose.yml stop
# Remove the container but keep the volume
docker rm photo-ingestion-pipeline
# Rebuild and start, mounting the existing volume
docker-compose -f docker/docker-compose.yml up --build -d
```

### 2. Backend Code Changes (Python)
For changes to Python files in `backend/`:
```bash
# Same process as frontend changes
docker-compose -f docker/docker-compose.yml stop
docker rm photo-ingestion-pipeline
docker-compose -f docker/docker-compose.yml up --build -d
```

### 3. Dependencies Changes
When modifying `requirements.txt` or the Dockerfile:
```bash
# Same process as above, but you might need to pull new base images
docker-compose -f docker/docker-compose.yml stop
docker rm photo-ingestion-pipeline
docker-compose -f docker/docker-compose.yml build --no-cache
docker-compose -f docker/docker-compose.yml up -d
```

### 4. Data Persistence
Your data is preserved in these locations:
- Session data: `data/sessions/`
- Logs: `data/logs/`
- Tags: `data/tags.json`
- Photos: `data/incoming/`

The volume mount in docker-compose.yml (`../data:/app/data`) ensures this data persists between rebuilds.

Important: 
- Never use `docker-compose down -v` as it will remove volumes
- Always stop and remove the container before rebuild to ensure clean state
- Use `-d` flag to run in detached mode (background)
- Check logs with `docker-compose -f docker/docker-compose.yml logs -f`

## Support
If you encounter issues, check `data/logs/` for session logs, and the container logs via `docker-compose logs backend`.

