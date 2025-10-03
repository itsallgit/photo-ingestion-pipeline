import os
import json
import uuid
import threading
import hashlib
import time
import datetime
import asyncio
import subprocess
from typing import List, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# Constants
APP_ROOT = os.path.dirname(__file__)
DATA_ROOT = os.path.abspath(os.path.join(APP_ROOT, "..", "data"))
SESSIONS_DIR = os.path.join(DATA_ROOT, "sessions")
LOGS_DIR = os.path.join(DATA_ROOT, "logs")
TAGS_FILE = os.path.join(DATA_ROOT, "tags.json")

# Ensure directories exist
for p in (DATA_ROOT, SESSIONS_DIR, LOGS_DIR):
    os.makedirs(p, exist_ok=True)

# Default config
DEFAULT_CONFIG = {
    "batch_size": 16,
    "object_confidence_threshold": 0.35,
    "face_match_threshold": 0.6,
    "geocode_mode": "offline"
}

# Globals
session_queues: Dict[str, asyncio.Queue] = {}
EVENT_LOOP = None
app = FastAPI(title="Photo Ingestion Pipeline - Wave 1 (MVP)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static frontend
static_dir = os.path.join(APP_ROOT, "static")
app.mount("/ui", StaticFiles(directory=static_dir, html=True), name="static")

@app.on_event("startup")
async def startup_event():
    global EVENT_LOOP
    try:
        EVENT_LOOP = asyncio.get_event_loop()
    except RuntimeError:
        EVENT_LOOP = asyncio.new_event_loop()
    # create tags.json if missing
    if not os.path.exists(TAGS_FILE):
        with open(TAGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"tags": {}, "updated_at": datetime.datetime.utcnow().isoformat()+"Z"}, f)

# -------------------------
# Utility functions
# -------------------------
def normalize_tag(tag: str) -> str:
    if not tag:
        return ""
    t = tag.lower()
    # replace spaces with underscore
    t = t.replace(" ", "_")
    # keep a-z, 0-9, underscore and hyphen
    import re
    t = re.sub(r'[^a-z0-9_\-]', '', t)
    t = t.replace("__", "_")
    t = t.strip("_")
    return t

def compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

PHOTO_EXTS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'}

def is_photo(path: str) -> bool:
    _, ext = os.path.splitext(path.lower())
    return ext in PHOTO_EXTS

def run_exiftool_read_keywords(path: str) -> List[str]:
    # uses exiftool -j -Keywords file
    try:
        res = subprocess.run(["exiftool", "-j", "-Keywords", path], capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("exiftool not found in PATH. Please install exiftool.")
    except subprocess.CalledProcessError as e:
        # exiftool returns non-zero on some files; try to parse stdout anyway
        res = e
    out = res.stdout.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
        if isinstance(data, list) and len(data) > 0:
            item = data[0]
            kw = item.get("Keywords")
            if kw is None:
                return []
            if isinstance(kw, list):
                return [str(x) for x in kw]
            else:
                return [str(kw)]
    except Exception:
        return []
    return []

def run_exiftool_write_keywords(path: str, keywords: List[str]) -> None:
    # Overwrite IPTC:Keywords with exact set
    # exiftool -overwrite_original -Keywords="a" -Keywords="b" file
    if not keywords:
        # Remove all Keywords (write empty) - exiftool uses -Keywords= to clear?
        # Use -Keywords= to clear
        cmd = ["exiftool", "-overwrite_original", "-Keywords=", path]
    else:
        cmd = ["exiftool", "-overwrite_original"]
        for k in keywords:
            cmd.append(f"-Keywords={k}")
        cmd.append(path)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("exiftool not found in PATH. Please install exiftool.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"exiftool failed: {e.stderr or e.stdout}")

def read_json_file(path: str) -> Any:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def write_json_atomic(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def append_log(session_id: str, line: str) -> None:
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    log_line = f"[{ts}] {line}\n"
    logpath = os.path.join(LOGS_DIR, f"{session_id}.log")
    with open(logpath, "a", encoding='utf-8') as f:
        f.write(log_line)
    # also send to websocket if queue exists
    send_ws_event(session_id, {"type": "log", "text": line, "ts": ts})

# -------------------------
# Scanning and session creation
# -------------------------
def scan_source_folder(source_folder: str) -> Dict[str, Any]:
    # Return subfolders relative to source_folder and basic file listing
    result = {"subfolders": {}, "total_files": 0, "photos": 0, "videos": 0}
    if not os.path.isdir(source_folder):
        raise HTTPException(status_code=400, detail="source_folder does not exist or is not a directory")
    for root, dirs, files in os.walk(source_folder):
        # relative path from source
        rel = os.path.relpath(root, source_folder)
        if rel == ".":
            rel = ""
        file_count = 0
        photo_count = 0
        for name in files:
            file_count += 1
            path = os.path.join(root, name)
            if is_photo(path):
                photo_count += 1
                result["photos"] += 1
            result["total_files"] += 1
        result["subfolders"][rel] = {"path": root, "file_count": file_count, "photo_count": photo_count}
    return result

def create_session(source_folder: str, folder_mappings: Dict[str, List[str]], config: Dict[str, Any]) -> Dict[str, Any]:
    # Validate inputs
    if not os.path.isdir(source_folder):
        raise HTTPException(status_code=400, detail="source_folder does not exist or is not a directory")
    cfg = DEFAULT_CONFIG.copy()
    if config:
        cfg.update(config)
    session_id = f"session-{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    # scan files
    files = []
    for root, dirs, filenames in os.walk(source_folder):
        for fn in filenames:
            full = os.path.join(root, fn)
            rel_dir = os.path.relpath(root, source_folder)
            if rel_dir == ".":
                rel_dir = ""
            entry = {
                "path": full,
                "rel_dir": rel_dir,
                "sha256": None,
                "is_photo": is_photo(full),
                "waves_completed": [],
                "iptc_keywords_before": [],
                "iptc_keywords_after": [],
                "last_processed": None,
                "errors": []
            }
            files.append(entry)
    session_meta = {
        "session_id": session_id,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "source_folder": source_folder,
        "folder_mappings": folder_mappings or {},
        "config": cfg,
        "summary": {
            "file_count": len(files),
            "photos_count": sum(1 for f in files if f["is_photo"]),
            "videos_count": sum(1 for f in files if not f["is_photo"])
        },
        "status": "created"
    }
    # write session meta and files list
    session_file = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    session_files_file = os.path.join(SESSIONS_DIR, f"{session_id}-files.json")
    write_json_atomic(session_file, session_meta)
    write_json_atomic(session_files_file, files)
    append_log(session_id, "Session created")
    return session_meta

# -------------------------
# Websocket helpers
# -------------------------
def ensure_queue(session_id: str) -> asyncio.Queue:
    q = session_queues.get(session_id)
    if q is None:
        q = asyncio.Queue()
        session_queues[session_id] = q
    return q

def send_ws_event(session_id: str, event: Dict[str, Any]):
    # Put event into the asyncio queue for the session
    if EVENT_LOOP is None:
        return
    q = ensure_queue(session_id)
    try:
        asyncio.run_coroutine_threadsafe(q.put(event), EVENT_LOOP)
    except Exception:
        pass

# -------------------------
# Wave 1 worker
# -------------------------
def wave1_worker(session_id: str):
    session_file = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    session_files_file = os.path.join(SESSIONS_DIR, f"{session_id}-files.json")
    if not os.path.exists(session_file) or not os.path.exists(session_files_file):
        append_log(session_id, "Session files missing, aborting")
        return
    session_meta = read_json_file(session_file)
    files = read_json_file(session_files_file)
    source_folder = session_meta["source_folder"]
    folder_mappings = session_meta.get("folder_mappings", {}) or {}
    cfg = session_meta.get("config", DEFAULT_CONFIG)
    any_mapping = any(folder_mappings.get(k) for k in folder_mappings)
    append_log(session_id, f"Wave 1 starting. any_mapping={any_mapping}")

    # If no mapping provided anywhere, auto-complete wave1
    if not any_mapping:
        append_log(session_id, "No custom tags provided for any folders. Wave 1 auto-completes.")
        all_new_tags = []
        for f in files:
            if f["is_photo"]:
                if "wave1" not in f["waves_completed"]:
                    f["waves_completed"].append("wave1")
                    f["last_processed"] = datetime.datetime.utcnow().isoformat() + "Z"
        write_json_atomic(session_files_file, files)
        # update global tags.json (no new tags in this case, but ensures file exists)
        update_global_tags([])
        session_meta["status"] = "wave1_completed"
        write_json_atomic(session_file, session_meta)
        send_ws_event(session_id, {"type":"wave_complete", "wave":"wave1"})
        append_log(session_id, "Wave 1 auto-complete done")
        return

    # Group files by folder for batch processing
    folder_files = {}
    for f in files:
        if not f["is_photo"]:
            continue
        if "wave1" in f["waves_completed"]:
            continue
        rel_dir = f.get("rel_dir", "")
        folder_files.setdefault(rel_dir, []).append(f)

    # Process each folder that has mappings
    all_new_tags = []
    total_files = sum(len(files) for files in folder_files.values())
    processed_files = 0

    for folder, folder_batch in folder_files.items():
        # Skip folders without mappings
        if not folder_batch:
            continue

        # Find tags for this folder
        tags = []
        if folder in folder_mappings and folder_mappings[folder]:
            tags = folder_mappings[folder]
        else:
            # try to find mapping for a parent folder path
            best = ""
            for k in folder_mappings.keys():
                if k == "":
                    continue
                if folder == k or folder.startswith(k + os.sep) or folder.startswith(k + "/"):
                    if len(k) > len(best):
                        best = k
            if best and folder_mappings.get(best):
                tags = folder_mappings.get(best, [])

        # normalize tags
        tags_norm = [normalize_tag(t) for t in tags if t and normalize_tag(t)]
        
        if not tags_norm:
            # No tags for this folder, mark all files complete
            for f in folder_batch:
                if "wave1" not in f["waves_completed"]:
                    f["waves_completed"].append("wave1")
                    f["last_processed"] = datetime.datetime.utcnow().isoformat() + "Z"
                processed_files += 1
            continue

        # Get existing tags for all files in batch
        folder_total = len(folder_batch)
        folder_processed = 0
        send_ws_event(session_id, {
            "type": "folder_progress",
            "folder": folder,
            "total": folder_total,
            "processed": folder_processed
        })

        # Process batch
        for f in folder_batch:
            # Check if we should stop
            session_meta = read_json_file(session_file)
            if session_meta.get("status") == "stopped":
                append_log(session_id, "Stop requested, ending processing")
                return

            try:
                existing = run_exiftool_read_keywords(f["path"])
            except Exception as e:
                err = f"Failed reading keywords for {f['path']}: {str(e)}"
                f["errors"].append(err)
                append_log(session_id, err)
                continue

            existing_norm = [normalize_tag(x) for x in existing if x and normalize_tag(x)]
            new_tags = [t for t in tags_norm if t not in existing_norm]
            merged = sorted(set(existing_norm + new_tags))

            if set(merged) != set(existing_norm):
                try:
                    run_exiftool_write_keywords(f["path"], merged)
                    append_log(session_id, f"Updated tags for {f['path']}")
                except Exception as e:
                    err = f"Failed writing keywords for {f['path']}: {str(e)}"
                    f["errors"].append(err)
                    append_log(session_id, err)
                    continue

            f["iptc_keywords_before"] = existing_norm
            f["iptc_keywords_after"] = merged
            if "wave1" not in f["waves_completed"]:
                f["waves_completed"].append("wave1")
            f["last_processed"] = datetime.datetime.utcnow().isoformat() + "Z"
            all_new_tags.extend(new_tags)
            
            folder_processed += 1
            processed_files += 1
            
            # Update progress
            send_ws_event(session_id, {
                "type": "folder_progress",
                "folder": folder,
                "total": folder_total,
                "processed": folder_processed,
                "complete": folder_processed >= folder_total
            })

        # Save progress after each folder
        write_json_atomic(session_files_file, files)

    # Update global tags
    update_global_tags(all_new_tags)

    # Mark session completed
    session_meta["status"] = "wave1_completed"
    write_json_atomic(session_file, session_meta)
    append_log(session_id, f"Wave 1 complete. Total processed={processed_files}/{total_files}")
    send_ws_event(session_id, {
        "type": "wave_complete",
        "wave": "wave1",
        "processed": processed_files,
        "total": total_files
    })

def update_global_tags(new_tags: List[str]) -> None:
    data = read_json_file(TAGS_FILE) or {"tags": {}, "updated_at": None}
    counts = data.get("tags", {})
    for t in new_tags:
        t_norm = normalize_tag(t)
        if not t_norm:
            continue
        counts[t_norm] = counts.get(t_norm, 0) + 1
    data["tags"] = counts
    data["updated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    write_json_atomic(TAGS_FILE, data)

# -------------------------
# API endpoints
# -------------------------
@app.get("/api/health")
async def health():
    return {"status":"ok"}

@app.get("/api/subfolders")
async def api_subfolders(source: str):
    # source is path on server/container
    info = scan_source_folder(source)
    return info

@app.post("/api/sessions")
async def api_create_session(payload: Dict):
    source = payload.get("source_folder")
    folder_mappings = payload.get("folder_mappings", {})
    config = payload.get("config", {})
    if not source:
        raise HTTPException(status_code=400, detail="source_folder required")
    session = create_session(source, folder_mappings, config)
    return session

@app.get("/api/sessions")
async def api_list_sessions():
    items = []
    for fn in os.listdir(SESSIONS_DIR):
        if fn.endswith(".json") and "-files" not in fn:
            path = os.path.join(SESSIONS_DIR, fn)
            try:
                meta = read_json_file(path)
                items.append(meta)
            except Exception:
                continue
    items = sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)
    return items

@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="session not found")
    return read_json_file(path)

@app.get("/api/sessions/{session_id}/files")
async def api_get_session_files(session_id: str):
    path = os.path.join(SESSIONS_DIR, f"{session_id}-files.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="session files not found")
    return read_json_file(path)

@app.post("/api/sessions/{session_id}/start")
async def api_start_session(session_id: str):
    # spawn background thread for wave1
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="session not found")
    meta = read_json_file(path)
    if meta.get("status") == "wave1_running":
        return {"status":"already_running"}
    meta["status"] = "wave1_running"
    write_json_atomic(path, meta)
    append_log(session_id, "Starting Wave 1 worker")
    thread = threading.Thread(target=wave1_worker, args=(session_id,), daemon=True)
    thread.start()
    return {"status":"started"}

@app.post("/api/sessions/{session_id}/stop")
async def api_stop_session(session_id: str):
    # Not implemented granular stop; we mark status and worker will check (simple)
    path = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="session not found")
    meta = read_json_file(path)
    meta["status"] = "stopped"
    write_json_atomic(path, meta)
    append_log(session_id, "Stop requested by user")
    return {"status":"stop_requested"}

@app.get("/api/tags")
async def api_get_tags():
    data = read_json_file(TAGS_FILE)
    return data or {"tags": {}, "updated_at": None}

# Websocket for session events
@app.websocket("/api/sessions/{session_id}/ws")
async def session_ws(websocket: WebSocket, session_id: str):
    await websocket.accept()
    q = ensure_queue(session_id)
    try:
        while True:
            event = await q.get()
            try:
                await websocket.send_json(event)
            except Exception:
                pass
    except WebSocketDisconnect:
        return

# Serve index.html directly for root
@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = os.path.join(static_dir, "index.html")
    return FileResponse(index_path)
