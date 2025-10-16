import os
import json
import uuid
import threading
import hashlib
import time
import datetime
import asyncio
import subprocess
import traceback
from typing import List, Dict, Any
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
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

import logging

# Globals
session_queues = {}  # type: Dict[str, asyncio.Queue]
EVENT_LOOP = None

# per-session logger cache
session_loggers = {}  # type: Dict[str, logging.Logger]
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
            # use a simple list of tags (deduped) rather than counts
            json.dump({"tags": [], "updated_at": datetime.datetime.utcnow().isoformat()+"Z"}, f)

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

def run_exiftool_read_keywords(path: str, session_id: str = None) -> List[str]:
    # uses exiftool -j -Keywords file
    cmd = ["exiftool", "-j", "-Keywords", path]
    try:
        structured_log(session_id or "global", "info", "Running exiftool read", {"cmd": cmd, "path": path})
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
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


def run_exiftool_read_keywords_batch(paths: List[str], session_id: str = None) -> Dict[str, List[str]]:
    """Read Keywords for multiple files in one exiftool invocation. Returns mapping path -> keywords list."""
    if not paths:
        return {}
    cmd = ["exiftool", "-j", "-Keywords"] + paths
    try:
        # log the command
        structured_log(session_id or "global", "info", "Running exiftool read batch", {"cmd": cmd, "count": len(paths)})
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("exiftool not found in PATH. Please install exiftool.")
    except subprocess.CalledProcessError as e:
        # Try to parse stdout even on non-zero exit
        res = e
    out = (res.stdout or "").strip()
    if not out:
        return {p: [] for p in paths}
    try:
        data = json.loads(out)
        result = {}
        if isinstance(data, list):
            for item in data:
                file = item.get('SourceFile') or item.get('FileName')
                kw = item.get('Keywords')
                if kw is None:
                    kws = []
                elif isinstance(kw, list):
                    kws = [str(x) for x in kw]
                else:
                    kws = [str(kw)]
                # exiftool returns filename only; attempt to match to absolute path
                # we'll map by filename fallback
                if file and os.path.isabs(file):
                    key = file
                else:
                    # find path that endswith file
                    matches = [p for p in paths if os.path.basename(p) == file]
                    key = matches[0] if matches else None
                if key:
                    result[key] = kws
        # ensure all paths present
        for p in paths:
            result.setdefault(p, [])
        return result
    except Exception:
        return {p: [] for p in paths}


def run_exiftool_write_keywords_batch(paths: List[str], keywords: List[str], session_id: str = None) -> None:
    """Write the same set of Keywords to multiple files in one exiftool invocation.

    This uses -overwrite_original and multiple -Keywords= entries, one exiftool call.
    Logs the command executed.
    """
    if not paths:
        return
    if not keywords:
        # clear keywords on all files
        cmd = ["exiftool", "-overwrite_original"]
        for p in paths:
            cmd.append(f"-Keywords=")
        cmd += paths
    else:
        cmd = ["exiftool", "-overwrite_original"]
        for k in keywords:
            cmd.append(f"-Keywords={k}")
        cmd += paths
    try:
        structured_log(session_id or "global", "info", "Running exiftool write batch", {"cmd": cmd, "count": len(paths)})
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("exiftool not found in PATH. Please install exiftool.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"exiftool failed: {e.stderr or e.stdout}")


def run_exiftool_write_keywords_to_folder(folder_path: str, keywords: List[str], session_id: str = None, recursive: bool = False) -> None:
    """Write Keywords to files in a folder using exiftool.

    This function uses exiftool's own recursion option (-r) when `recursive` is True.
    It performs additive writes using -Keywords+= to avoid wiping existing tags.
    No custom recursion traversal is implemented here; exiftool handles recursion.
    """
    if not os.path.isdir(folder_path):
        raise RuntimeError(f"folder not found: {folder_path}")
    # Only apply to known photo extensions to avoid touching unrelated files in the folder
    exts = [e.lstrip('.') for e in PHOTO_EXTS]
    if not keywords:
        # If no keywords, do nothing (do not clear keywords) — additive behavior preferred
        structured_log(session_id or "global", "info", "No keywords provided for folder write, skipping", {"folder": folder_path})
        return
    # build an additive write command (non-recursive): -Keywords+= will add keywords to files in the folder
    cmd = ["exiftool", "-overwrite_original"]
    if recursive:
        cmd.append("-r")
    for k in keywords:
        cmd.append(f"-Keywords+={k}")
    # add extension filters
    for ex in exts:
        cmd.extend(["-ext", ex])
    cmd.append(folder_path)
    try:
        structured_log(session_id or "global", "info", "Running exiftool write to folder", {"cmd": cmd, "folder": folder_path})
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("exiftool not found in PATH. Please install exiftool.")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"exiftool failed: {e.stderr or e.stdout}")

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
        structured_log("global", "info", "Running exiftool write", {"cmd": cmd, "path": path})
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

def get_session_logger(session_id: str) -> logging.Logger:
    """Return (and create if necessary) a logger that appends to data/logs/<session_id>.log.

    The logger writes human-readable lines produced by structured_log. We cache loggers to
    avoid adding multiple handlers on repeated calls.
    """
    logger = session_loggers.get(session_id)
    if logger:
        return logger
    # create logger
    logger = logging.getLogger(f"session.{session_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # avoid duplicate handlers
    if not logger.handlers:
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
        except Exception:
            pass
        logpath = os.path.join(LOGS_DIR, f"{session_id}.log")
        try:
            fh = logging.FileHandler(logpath, mode='a', encoding='utf-8')
            fh.setLevel(logging.INFO)
            # message already includes timestamp/level; keep formatter simple
            fh.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(fh)
        except Exception:
            # fallback: do nothing; calls to logger will be no-ops
            pass
    session_loggers[session_id] = logger
    return logger


def structured_log(session_id: str, level: str, message: str, meta: Dict[str, Any] = None) -> None:
    """Write a human-readable log line to the session file (append) and emit a websocket event.

    Log line format: [ISO_TS] [LEVEL] message \n<meta-as-json-optional>
    """
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    lvl = (level or "info").upper()

    # Single-line log format: [ISO_TS] [LEVEL] message
    # Metadata is sent as a separate field on websocket events and not appended to the file line.
    line = f"[{ts}] [{lvl}] {message}"

    # write to per-session file using simple append+flush (guaranteed)
    try:
        p = os.path.join(LOGS_DIR, f"{session_id}.log")
        with open(p, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception:
        # best-effort fallback to logger
        try:
            logger = get_session_logger(session_id)
            if logger:
                if lvl == 'ERROR' or lvl == 'CRITICAL':
                    logger.error(line)
                else:
                    logger.info(line)
        except Exception:
            pass

    # also send to websocket clients (meta is included in the event)
    event = {"type": "log", "ts": ts, "text": message, "level": level}
    if meta:
        event["meta"] = meta
    send_ws_event(session_id, event)


def append_log(session_id: str, line: str) -> None:
    # backward-compatible wrapper used throughout the codebase
    # If the line contains a trailing JSON object (common in older code paths),
    # extract it and pass it as meta so the human-readable message stays clean.
    if not line:
        structured_log(session_id, "info", "", None)
        return
    try:
        import re
        m = re.match(r'^(.*)\s(\{.*\})\s*$', line)
        if m:
            msg = m.group(1).strip()
            meta_raw = m.group(2)
            try:
                meta = json.loads(meta_raw)
            except Exception:
                meta = meta_raw
            structured_log(session_id, "info", msg, meta)
            return
    except Exception:
        pass
    structured_log(session_id, "info", line)

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
    structured_log(session_id, "info", "Session created", {"file_count": len(files), "photos": session_meta["summary"]["photos_count"]})
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
    global EVENT_LOOP
    if EVENT_LOOP is None:
        # try to recover a running loop reference if possible
        try:
            EVENT_LOOP = asyncio.get_running_loop()
        except Exception:
            try:
                EVENT_LOOP = asyncio.get_event_loop()
            except Exception:
                EVENT_LOOP = None
    if EVENT_LOOP is None:
        # log missing event loop for debugging directly to file to avoid recursion
        try:
            logger = get_session_logger(session_id)
            if logger:
                logger.error(f"[EVENT_LOOP MISSING] cannot send ws event {event.get('type')}")
        except Exception:
            pass
        return
    q = ensure_queue(session_id)
    try:
        # write a safe file-only log entry (do not call structured_log -> would send ws event)
        try:
            logger = get_session_logger(session_id)
            if logger:
                logger.info(f">>> Event: Queueing - {event.get('type')}")
        except Exception:
            pass
        asyncio.run_coroutine_threadsafe(q.put(event), EVENT_LOOP)
        try:
            logger = get_session_logger(session_id)
        except Exception:
            pass
    except Exception:
        try:
            logger = get_session_logger(session_id)
            if logger:
                logger.error(f">>> Event: Failed to queue - {event}")
        except Exception:
            pass

# -------------------------
# Wave 1 worker
# -------------------------
def wave1_worker(session_id: str):
    try:
        # wrap entire worker in try/except to capture unexpected errors
        _wave1_worker_impl(session_id)
    except Exception as e:
        # log unhandled exceptions to session log
        structured_log(session_id, "error", "Unhandled exception in worker", {"err": str(e), "trace": traceback.format_exc()})
        # also update session status
        try:
            session_file = os.path.join(SESSIONS_DIR, f"{session_id}.json")
            if os.path.exists(session_file):
                meta = read_json_file(session_file) or {}
                meta["status"] = "error"
                write_json_atomic(session_file, meta)
        except Exception:
            pass
    return


def _wave1_worker_impl(session_id: str):
    session_file = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    session_files_file = os.path.join(SESSIONS_DIR, f"{session_id}-files.json")
    if not os.path.exists(session_file) or not os.path.exists(session_files_file):
        structured_log(session_id, "error", "Session files missing, aborting", {"session_file": session_file, "session_files_file": session_files_file})
        return
    session_meta = read_json_file(session_file)
    files = read_json_file(session_files_file)
    source_folder = session_meta["source_folder"]
    folder_mappings = session_meta.get("folder_mappings", {}) or {}
    cfg = session_meta.get("config", DEFAULT_CONFIG)
    any_mapping = any(folder_mappings.get(k) for k in folder_mappings)
    structured_log(session_id, "info", "Wave 1 worker started", {"any_mapping": bool(any_mapping), "total_files": len(files)})

    # If no mapping provided anywhere, auto-complete wave1
    if not any_mapping:
        structured_log(session_id, "info", "No custom tags provided for any folders. Wave 1 auto-completes.")
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
        # emit folder Completed for all folders so frontend reflects final state
        rels = set([f.get("rel_dir", "") for f in files])
        for r in rels:
            send_ws_event(session_id, {"type": "folder_status", "folder": r, "status": "Completed"})
        send_ws_event(session_id, {"type":"wave_complete", "wave":"wave1"})
        structured_log(session_id, "info", "Wave 1 auto-complete done")
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

    # Emit initial Pending status for each folder so frontend shows Pending state
    for folder in folder_files.keys():
        send_ws_event(session_id, {"type": "folder_status", "folder": folder, "status": "Pending"})

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
            # No tags for this folder, mark all files complete and emit Completed status
            for f in folder_batch:
                if "wave1" not in f["waves_completed"]:
                    f["waves_completed"].append("wave1")
                    f["last_processed"] = datetime.datetime.utcnow().isoformat() + "Z"
                processed_files += 1
            write_json_atomic(session_files_file, files)
            structured_log(session_id, "info", "Folder skipped (no tags)", {"folder": folder, "total": len(folder_batch), "status": "Completed"})
            send_ws_event(session_id, {"type": "folder_status", "folder": folder, "status": "Completed"})
            continue

        # Write tags for the entire folder in one exiftool call (no per-file read)
        folder_total = len(folder_batch)
        # folder path on disk: join source_folder and rel dir
        folder_full = os.path.join(source_folder, folder) if folder else source_folder

        structured_log(session_id, "info", "Processing folder (additive recursive)", {"folder": folder, "total": folder_total, "status": "Processing", "tags": tags_norm})
        send_ws_event(session_id, {"type": "folder_status", "folder": folder, "status": "Processing"})

        # Perform one additive write for the folder. Allow exiftool to recurse when configured.
        recursive_flag = bool(cfg.get("recursive_folder_writes", False))
        try:
            run_exiftool_write_keywords_to_folder(folder_full, tags_norm, session_id=session_id, recursive=recursive_flag)
            structured_log(session_id, "info", "Ran exiftool folder write (additive)", {"folder": folder, "tags": tags_norm, "recursive": recursive_flag})
        except Exception as e:
            structured_log(session_id, "error", "Failed writing keywords for folder", {"folder": folder, "err": str(e)})

        # Update file metadata after writes. We no longer pre-read per-file keywords; just record that the wave completed
        for f in folder_batch:
            # iptc_keywords_before is not populated to avoid extra reads; leave as-is or empty
            f["iptc_keywords_before"] = f.get("iptc_keywords_before", [])
            # iptc_keywords_after will be updated conservatively as tags_norm merged with existing placeholder
            # to reflect intended new state without forcing a read
            after = list(sorted(set([normalize_tag(x) for x in (f.get("iptc_keywords_before", []) or [])] + tags_norm)))
            f["iptc_keywords_after"] = after
            if "wave1" not in f["waves_completed"]:
                f["waves_completed"].append("wave1")
            f["last_processed"] = datetime.datetime.utcnow().isoformat() + "Z"
            # collect new tags for global update (we'll dedupe globally later)
            all_new_tags.extend(tags_norm)

        # Save progress after each folder
        write_json_atomic(session_files_file, files)

        # increment processed_files by folder_total
        processed_files += folder_total

        # Mark folder completed and emit Completed status so frontend can update folder row immediately
        structured_log(session_id, "info", "Folder complete", {"folder": folder, "processed": folder_total, "status": "Completed"})
        send_ws_event(session_id, {"type": "folder_status", "folder": folder, "status": "Completed"})
    # Overall summary for wave1
    structured_log(session_id, "info", "Wave 1 folder processing complete", {"processed": processed_files, "total": total_files})

    # Update global tags
    try:
        update_global_tags(all_new_tags)
        structured_log(session_id, "info", "Global tags updated after wave1", {"added_count": len(all_new_tags)})
    except Exception as e:
        structured_log(session_id, "error", "Failed updating global tags", {"err": str(e), "trace": traceback.format_exc()})

    # Mark session completed
    try:
        session_meta["status"] = "wave1_completed"
        write_json_atomic(session_file, session_meta)
        structured_log(session_id, "info", "Wave 1 complete", {"processed": processed_files, "total": total_files})
    except Exception as e:
        structured_log(session_id, "error", "Failed writing session meta at completion", {"err": str(e), "trace": traceback.format_exc()})

    # Send final websocket events (ensure every folder gets an explicit Completed event, then wave_complete and session_meta)
    try:
        # Ensure every folder from the session folder_mappings is marked Completed so UI can't miss root or other folders
        try:
            fm = session_meta.get('folder_mappings', {}) or {}
            for folder in fm.keys():
                send_ws_event(session_id, {"type": "folder_status", "folder": folder, "status": "Completed"})
            structured_log(session_id, "info", "Emitted final folder_status=Completed for all folders", {"folders": list(fm.keys())})
        except Exception as _:
            # non-fatal: continue to send wave_complete/session_meta
            structured_log(session_id, "warning", "Failed emitting final folder_status events", {"err": str(_)} )
        send_ws_event(session_id, {
            "type": "wave_complete",
            "wave": "wave1",
            "processed": processed_files,
            "total": total_files
        })
        structured_log(session_id, "info", "Sent wave_complete event", {"processed": processed_files, "total": total_files})
    except Exception as e:
        structured_log(session_id, "error", "Failed sending wave_complete event", {"err": str(e), "trace": traceback.format_exc()})

    try:
        send_ws_event(session_id, {"type": "session_meta", "meta": session_meta})
        structured_log(session_id, "info", "Sent session_meta event", {"session_id": session_meta.get("session_id")} )
    except Exception as e:
        structured_log(session_id, "error", "Failed sending session_meta event", {"err": str(e), "trace": traceback.format_exc()})

    structured_log(session_id, "info", "Wave 1 finished - worker exiting", {})


@app.get("/api/sessions/{session_id}/log/tail")
async def api_tail_session_log(session_id: str, lines: int = 200):
    """Return the last N lines of the session log as plain text."""
    path = os.path.join(LOGS_DIR, f"{session_id}.log")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="log not found")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            all_lines = f.read().splitlines()
            return "\n".join(all_lines[-lines:])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def update_global_tags(new_tags: List[str]) -> None:
    data = read_json_file(TAGS_FILE) or {"tags": [], "updated_at": None}
    existing = set(data.get("tags", []) or [])
    added = []
    for t in new_tags:
        t_norm = normalize_tag(t)
        if not t_norm:
            continue
        if t_norm not in existing:
            existing.add(t_norm)
            added.append(t_norm)
    data["tags"] = sorted(existing)
    data["updated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    write_json_atomic(TAGS_FILE, data)
    if added:
        sample = added[:10]
        structured_log("global", "info", "Global tags updated", {"added_count": len(added), "sample": sample})

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
    return JSONResponse(content=items)

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


@app.get("/api/sessions/{session_id}/log")
async def api_get_session_log(session_id: str):
    """Return the raw text log for a session as plain text."""
    path = os.path.join(LOGS_DIR, f"{session_id}.log")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="log not found")
    try:
        # read the full file into memory and return as plain text to ensure Content-Length is correct
        with open(path, 'r', encoding='utf-8') as f:
            data = f.read()
        return PlainTextResponse(content=data, media_type='text/plain')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    structured_log(session_id, "info", "Starting Wave 1 worker")
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
    structured_log(session_id, "info", "Stop requested by user")
    return {"status":"stop_requested"}

@app.get("/api/tags")
async def api_get_tags():
    data = read_json_file(TAGS_FILE)
    return JSONResponse(content=(data or {"tags": [], "updated_at": None}))

# Websocket for session events
@app.websocket("/api/sessions/{session_id}/ws")
async def session_ws(websocket: WebSocket, session_id: str):
    # Capture the running event loop for thread-safe queuing from background workers
    global EVENT_LOOP
    try:
        EVENT_LOOP = asyncio.get_running_loop()
    except Exception:
        try:
            EVENT_LOOP = asyncio.get_event_loop()
        except Exception:
            EVENT_LOOP = None
    await websocket.accept()
    q = ensure_queue(session_id)
    try:
        logger = None
        try:
            logger = get_session_logger(session_id)
        except Exception:
            logger = None
        while True:
            event = await q.get()
            # Write a file-only diagnostic that we're about to send this event
            try:
                if logger:
                    logger.info(f">>> WS sending event: {event.get('type')}")
            except Exception:
                pass
            try:
                await websocket.send_json(event)
                try:
                    if logger:
                        logger.info(f">>> WS sent event: {event.get('type')}")
                except Exception:
                    pass
            except WebSocketDisconnect:
                try:
                    if logger:
                        logger.info('>>> WS disconnected by client')
                except Exception:
                    pass
                return
            except Exception as e:
                try:
                    if logger:
                        logger.error(f">>> WS send failed: {e}")
                except Exception:
                    pass
                # on failure, attempt to close websocket and exit
                try:
                    await websocket.close()
                except Exception:
                    pass
                return
    except WebSocketDisconnect:
        try:
            logger = get_session_logger(session_id)
            if logger:
                logger.info('>>> WS disconnected')
        except Exception:
            pass
        return

# Serve index.html directly for root
@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = os.path.join(static_dir, "index.html")
    return FileResponse(index_path)
