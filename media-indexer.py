#!/usr/bin/env python3
"""
Media Indexer for Northwoods Vault
Crawls NAS volumes, describes media with AI (Gemma 3 12B vision),
stores descriptions in SQLite with full-text search.
Optional ChromaDB for semantic/vector search (pip3 install chromadb).

Runs on the Mac Pro alongside the LLM server.
"""

import sqlite3
import json
import os
import sys
import time
import hashlib
import subprocess
import urllib.request
import urllib.error
import base64
import threading
import queue
import signal
import logging
import itertools
import uuid
import cgi
import io
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import re

STOP_WORDS = {'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been',
              'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
              'could', 'should', 'may', 'might', 'shall', 'can', 'need',
              'show', 'me', 'find', 'get', 'give', 'look', 'with', 'for',
              'in', 'on', 'at', 'to', 'of', 'and', 'or', 'but', 'not',
              'this', 'that', 'these', 'those', 'it', 'its', 'i', 'my',
              'some', 'any', 'all', 'from', 'by', 'up', 'about'}

def build_fts_query(raw_query):
    """Convert natural language query to FTS5 AND-match of meaningful keywords."""
    words = re.findall(r'\b\w+\b', raw_query.lower())
    keywords = [w for w in words if w not in STOP_WORDS and len(w) > 2]
    if not keywords:
        keywords = re.findall(r'\b\w+\b', raw_query.lower())
    return ' '.join('"' + k.replace('"', '""') + '"' for k in keywords)


# Optional: ChromaDB for semantic/vector search
try:
    import chromadb
    HAS_CHROMADB = True
except ImportError:
    HAS_CHROMADB = False

# Optional: face_recognition for face detection and identification
try:
    import face_recognition
    import numpy as np
    HAS_FACE_RECOGNITION = True
except ImportError:
    HAS_FACE_RECOGNITION = False

# Serialize all SQLite writes across threads to prevent "database is locked" stalls.
# SQLite WAL mode allows concurrent reads but only one writer at a time. Without this
# lock, two GPU/prep threads committing simultaneously can cause 30-second busy waits.
_db_write_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LLM_SERVERS = [
    "http://localhost:8090",  # GPU1 (RX 580) — Gemma
    "http://localhost:8091",  # GPU2 (RX 580) — Gemma
]
LLM_FALLBACK = "http://localhost:8080"  # Single-server mode fallback
WHISPER_SERVER = "http://localhost:8092"  # GPU0 (Pro 580X) — Whisper
TRANSCRIPTION_MAX_SECONDS = 600  # Cap at 10 minutes per file

# Pro 580X orchestrator — model-swapping between Gemma (API) and Whisper (transcription)
PRO580X_GEMMA_PORT = 8093
PRO580X_GEMMA = "http://localhost:%d" % PRO580X_GEMMA_PORT
PRO580X_WHISPER_PORT = 8092  # same as WHISPER_SERVER
MODEL_LOAD_TIMEOUT = 60          # seconds to wait for /health after starting a server
WHISPER_BATCH_MAX_TASKS = 10     # max transcriptions before forcing swap back to Gemma

DATA_DIR = Path.home() / "media-index"
DB_PATH = DATA_DIR / "index.db"
TRANSCRIBE_HEARTBEAT = DATA_DIR / "transcribe-active"  # touched while transcribing
SCANNER_STATE_FILE   = DATA_DIR / "scanner-state.json" # written by watch process, read by serve
PRO580X_STATE_FILE   = DATA_DIR / "pro580x-state.json" # written by watch, read by serve
THUMB_DIR = DATA_DIR / "thumbnails"
UPLOAD_DIR = DATA_DIR / "uploads"
CHROMA_DIR = DATA_DIR / "chroma"
LOG_PATH = DATA_DIR / "indexer.log"
FFMPEG = "/usr/bin/ffmpeg"
FFPROBE = "/usr/bin/ffprobe"

# File extensions to index
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".m4v", ".avi", ".mkv", ".r3d", ".braw"}
AUDIO_EXTS = {".wav", ".mp3", ".aac", ".m4a", ".flac", ".aif", ".aiff"}
ALL_MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

# Skip these directories
SKIP_DIRS = {".Spotlight-V100", ".Trashes", ".fseventsd", ".DS_Store",
             ".TemporaryItems", "@eaDir", "#recycle", "$RECYCLE.BIN",
             "RESOURCE.FRK"}  # macOS resource fork containers

# Scene detection: ffmpeg select filter threshold (higher = less sensitive)
# ffmpeg's scene score ranges 0-1; scores above this trigger a cut detection.
# 0.3 works well for hard cuts (switcher cuts, not dissolves).
SCENE_CHANGE_THRESHOLD = 0.3

# VAAPI hardware decode for scene detection (uses GPU decode ASIC, not compute units)
# Round-robin across all 3 GPUs — decode ASICs are separate from compute units
VAAPI_DEVICES = ["/dev/dri/renderD128", "/dev/dri/renderD129", "/dev/dri/renderD130"]
_vaapi_counter = itertools.count()

# Max concurrent LLM requests (matches --parallel 3)
MAX_CONCURRENT_LLM = len(LLM_SERVERS)  # One worker per GPU

# Max image size to send to LLM (resize if larger)
MAX_IMAGE_DIMENSION = 1280

# Whisper chunking: split audio into N-second pieces.
# On Linux/RADV there's no cross-GPU serialization, so larger chunks are fine.
# 300s = 5 minutes per chunk (reduces ffmpeg overhead vs 30s chunks).
WHISPER_CHUNK_SECONDS = 300

# Timeout constants (seconds)
WHISPER_TIMEOUT = 600           # per-chunk Whisper transcription (2x margin for 300s chunks)
VISION_TIMEOUT = 300            # Gemma vision inference (describe_image)
AUDIO_DESCRIPTION_TIMEOUT = 60  # Gemma text inference (describe_audio_filename)
AUDIO_EXTRACT_TIMEOUT = 120     # ffmpeg audio extraction
FFPROBE_TIMEOUT = 30            # ffprobe metadata calls
THUMBNAIL_TIMEOUT = 60          # ffmpeg keyframe extraction

# Raw camera format tools (R3D and BRAW)
BRAW_FRAME = "/usr/local/bin/braw-frame"   # Blackmagic RAW SDK frame extractor
REDLINE = "/usr/local/bin/REDline"          # RED Digital Cinema R3D extractor
# R3D probe frame indices: try each in sequence, stop on first failure.
# At 24fps these cover: 0s, 1s, 5s, 15s, 30s, 60s, 120s, 240s
R3D_PROBE_FRAMES = [0, 24, 120, 360, 720, 1440, 2880, 5760]

# Rescan interval for watching (seconds)
RESCAN_INTERVAL = 300  # 5 minutes

# Face recognition settings
FACE_THUMB_DIR = DATA_DIR / "face-thumbnails"
FACE_TOLERANCE = 0.5          # chinese_whispers clustering threshold (lower = stricter)
FACE_MATCH_TOLERANCE = 0.6    # compare_faces matching threshold
FACE_CROP_SIZE = 150           # Face thumbnail size in pixels
FACE_CROP_PADDING = 0.3       # Extra padding around face crop (30%)
MIN_FACE_SIZE = 40             # Skip faces smaller than 40px
FACE_WORKERS = 24              # Parallel face detection processes (spawn mode)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stdout)
        ]
    )

log = logging.getLogger("media-indexer")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    """Create database and tables if they don't exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(str(DB_PATH), timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA wal_autocheckpoint(100)")  # Prevent WAL bloat (Issue #37)

    db.executescript("""
        CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            name TEXT,
            enabled INTEGER DEFAULT 1,
            last_scan TEXT,
            file_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            folder_id TEXT,
            file_type TEXT,
            size_bytes INTEGER,
            modified_at TEXT,
            duration_seconds REAL,
            width INTEGER,
            height INTEGER,
            codec TEXT,
            ai_description TEXT,
            tags TEXT,
            indexed_at TEXT,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            FOREIGN KEY (folder_id) REFERENCES folders(id)
        );

        CREATE TABLE IF NOT EXISTS keyframes (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            timestamp_seconds REAL NOT NULL,
            thumbnail_path TEXT,
            ai_description TEXT,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
        CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
        CREATE INDEX IF NOT EXISTS idx_files_type ON files(file_type);
        CREATE INDEX IF NOT EXISTS idx_keyframes_file ON keyframes(file_id);

        -- Face recognition tables
        CREATE TABLE IF NOT EXISTS persons (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT,
            face_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS faces (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            keyframe_id TEXT,
            person_id TEXT,
            cluster_id INTEGER,
            embedding BLOB NOT NULL,
            bbox_top INTEGER,
            bbox_right INTEGER,
            bbox_bottom INTEGER,
            bbox_left INTEGER,
            thumbnail_path TEXT,
            created_at TEXT,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY (keyframe_id) REFERENCES keyframes(id) ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES persons(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_faces_file ON faces(file_id);
        CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
        CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);
        CREATE INDEX IF NOT EXISTS idx_faces_keyframe ON faces(keyframe_id);

        CREATE TABLE IF NOT EXISTS ignored_clusters (
            cluster_id INTEGER PRIMARY KEY
        );

        -- Track which files have been scanned for faces (even if 0 found)
        CREATE TABLE IF NOT EXISTS face_scanned_files (
            file_id TEXT PRIMARY KEY,
            scanned_at TEXT,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        );

        -- Perpetual Task Pipeline: one row per discrete operation on a file
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,          -- "{file_id}_{task_type}"
            file_id TEXT NOT NULL,
            task_type TEXT NOT NULL,      -- 'transcribe' | 'scene_detect' | 'visual_analysis' | 'face_detect'
            status TEXT DEFAULT 'pending',-- 'pending' | 'assigned' | 'complete' | 'failed' | 'abandoned'
            worker_id TEXT,
            created_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            error_message TEXT,
            retry_count INTEGER DEFAULT 0,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_type_status ON tasks(task_type, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_file ON tasks(file_id);
    """)

    # Migration: backfill face_scanned_files from existing faces data
    existing_scanned = db.execute(
        "SELECT COUNT(*) FROM face_scanned_files"
    ).fetchone()[0]
    if existing_scanned == 0:
        backfilled = db.execute("""
            INSERT OR IGNORE INTO face_scanned_files (file_id, scanned_at)
            SELECT DISTINCT file_id, created_at FROM faces
        """).rowcount
        if backfilled > 0:
            db.commit()
            log.info("Migrated: backfilled %d files into face_scanned_files" % backfilled)

    # Migration: add face_names column to files if it doesn't exist
    try:
        db.execute("SELECT face_names FROM files LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE files ADD COLUMN face_names TEXT")
        db.commit()
        log.info("Migrated: added face_names column to files table")

    # Migration: add transcript column to files if it doesn't exist
    try:
        db.execute("SELECT transcript FROM files LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE files ADD COLUMN transcript TEXT")
        db.commit()
        log.info("Migrated: added transcript column to files table")

    # Migration: add transcript_segments column to files if it doesn't exist
    try:
        db.execute("SELECT transcript_segments FROM files LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE files ADD COLUMN transcript_segments TEXT")
        db.commit()
        log.info("Migrated: added transcript_segments column to files table")

    # Migration: add source column to tasks (api vs crawler priority)
    try:
        db.execute("SELECT source FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE tasks ADD COLUMN source TEXT DEFAULT 'crawler'")
        db.commit()
        log.info("Migrated: added source column to tasks table")

    # Migration: add api_job_id column to tasks (links task to API job)
    try:
        db.execute("SELECT api_job_id FROM tasks LIMIT 1")
    except sqlite3.OperationalError:
        db.execute("ALTER TABLE tasks ADD COLUMN api_job_id TEXT")
        db.commit()
        log.info("Migrated: added api_job_id column to tasks table")

    # API jobs table — tracks externally submitted jobs
    db.execute("""
        CREATE TABLE IF NOT EXISTS api_jobs (
            id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            source_app TEXT,
            uploaded_filename TEXT,
            upload_path TEXT,
            result TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_api_jobs_status ON api_jobs(status)")
    db.commit()

    # Migration: add lyrics column to api_jobs
    try:
        db.execute("SELECT lyrics FROM api_jobs LIMIT 1")
    except Exception:
        db.execute("ALTER TABLE api_jobs ADD COLUMN lyrics TEXT")
        db.commit()
        log.info("Migrated: added lyrics column to api_jobs table")

    # Migration: add text_chat columns to api_jobs
    try:
        db.execute("SELECT prompt FROM api_jobs LIMIT 1")
    except Exception:
        db.execute("ALTER TABLE api_jobs ADD COLUMN prompt TEXT")
        db.execute("ALTER TABLE api_jobs ADD COLUMN max_tokens INTEGER")
        db.execute("ALTER TABLE api_jobs ADD COLUMN temperature REAL")
        db.commit()
        log.info("Migrated: added prompt/max_tokens/temperature columns to api_jobs table")

    # Migration: add match_threshold column to persons table (adaptive matching)
    try:
        db.execute("SELECT match_threshold FROM persons LIMIT 1")
    except Exception:
        db.execute("ALTER TABLE persons ADD COLUMN match_threshold REAL")
        db.commit()
        log.info("Migrated: added match_threshold column to persons table")

    # Issue #28: notifications table
    db.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            severity TEXT DEFAULT 'info',
            created_at TEXT NOT NULL,
            read INTEGER DEFAULT 0
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(read)")
    db.commit()

    # Full-text search index (with face_names support)
    # Check if FTS table needs migration to include face_names
    needs_fts_rebuild = False
    try:
        # Check if FTS table exists and has the right columns
        fts_check = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='files_fts'"
        ).fetchone()
        if fts_check:
            if "face_names" not in fts_check[0] or "transcript" not in fts_check[0] or "porter" not in fts_check[0]:
                needs_fts_rebuild = True
        else:
            needs_fts_rebuild = False  # table doesn't exist, create fresh below
    except Exception:
        pass

    if needs_fts_rebuild:
        log.info("Migrating FTS index to include face_names and transcript...")
        db.executescript("""
            DROP TRIGGER IF EXISTS files_ai;
            DROP TRIGGER IF EXISTS files_ad;
            DROP TRIGGER IF EXISTS files_au;
            DROP TABLE IF EXISTS files_fts;
        """)

    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
            filename, ai_description, tags, face_names, transcript,
            content='files',
            content_rowid='rowid',
            tokenize='porter unicode61'
        )
    """)

    # Triggers to keep FTS in sync (including face_names and transcript)
    db.executescript("""
        CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
            INSERT INTO files_fts(rowid, filename, ai_description, tags, face_names, transcript)
            VALUES (new.rowid, new.filename, new.ai_description, new.tags, new.face_names, new.transcript);
        END;

        CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, filename, ai_description, tags, face_names, transcript)
            VALUES ('delete', old.rowid, old.filename, old.ai_description, old.tags, old.face_names, old.transcript);
        END;

        CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF ai_description, tags, face_names, transcript ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, filename, ai_description, tags, face_names, transcript)
            VALUES ('delete', old.rowid, old.filename, old.ai_description, old.tags, old.face_names, old.transcript);
            INSERT INTO files_fts(rowid, filename, ai_description, tags, face_names, transcript)
            VALUES (new.rowid, new.filename, new.ai_description, new.tags, new.face_names, new.transcript);
        END;
    """)

    # Rebuild FTS if we just migrated
    if needs_fts_rebuild:
        log.info("Rebuilding FTS index...")
        rows = db.execute(
            "SELECT rowid, filename, ai_description, tags, face_names, transcript FROM files"
        ).fetchall()
        for row in rows:
            db.execute(
                "INSERT INTO files_fts(rowid, filename, ai_description, tags, face_names, transcript) "
                "VALUES (?, ?, ?, ?, ?, ?)", row
            )
        db.commit()
        log.info("FTS rebuild complete (%d rows)" % len(rows))

    # Issue #44: Reset any files stuck in "indexing" from a previous crashed run.
    # Must happen before workers start to avoid a race condition.
    stuck = db.execute("SELECT COUNT(*) FROM files WHERE status='indexing'").fetchone()[0]
    if stuck:
        db.execute("UPDATE files SET status='pending' WHERE status='indexing'")
        db.commit()
        import logging as _logging
        _logging.getLogger(__name__).info("Startup recovery: reset %d stuck 'indexing' files to pending" % stuck)

    db.commit()
    return db


def _update_api_job(db, task_id, status, result=None, error=None):
    """If this task belongs to an API job, update the job's status and result."""
    row = db.execute(
        "SELECT t.api_job_id, t.task_type, t.file_id FROM tasks t "
        "WHERE t.id=? AND t.api_job_id IS NOT NULL",
        (task_id,)
    ).fetchone()
    if not row:
        return
    job_id, task_type, file_id = row
    now = datetime.now().isoformat()
    if status == 'complete':
        # Build result from what the worker wrote to the files/keyframes tables
        if result is None:
            result = _build_api_result(db, task_type, task_id, file_id)
        db.execute(
            "UPDATE api_jobs SET status='complete', completed_at=?, result=? WHERE id=?",
            (now, json.dumps(result) if result else None, job_id)
        )
    elif status == 'failed':
        db.execute(
            "UPDATE api_jobs SET status='failed', completed_at=?, error_message=? WHERE id=?",
            (now, error, job_id)
        )
    elif status == 'assigned':
        db.execute(
            "UPDATE api_jobs SET status='processing', started_at=? WHERE id=? AND status='queued'",
            (now, job_id)
        )
    db.commit()


def _build_api_result(db, task_type, task_id, file_id):
    """Extract the result payload from DB after a task completes."""
    if task_type == 'transcribe':
        row = db.execute(
            "SELECT transcript, transcript_segments FROM files WHERE id=?", (file_id,)
        ).fetchone()
        if row:
            segments = json.loads(row[1]) if row[1] else []
            return {"transcript": row[0] or "", "segments": segments}
    elif task_type == 'visual_analysis':
        # task_id format: "{keyframe_id}_visual_analysis"
        kf_id = task_id[:-len("_visual_analysis")]
        row = db.execute(
            "SELECT ai_description FROM keyframes WHERE id=?", (kf_id,)
        ).fetchone()
        if row:
            return {"description": row[0] or ""}
    elif task_type == 'face_detect':
        kf_id = task_id[:-len("_face_detect")]
        faces = db.execute(
            "SELECT id, person_id, cluster_id FROM faces WHERE keyframe_id=?", (kf_id,)
        ).fetchall()
        return {"faces_found": len(faces), "face_ids": [f[0] for f in faces]}
    elif task_type == 'scene_detect':
        kf_count = db.execute(
            "SELECT COUNT(*) FROM keyframes WHERE file_id=?", (file_id,)
        ).fetchone()[0]
        return {"keyframe_count": kf_count}
    elif task_type == 'ala':
        # ALA result is stored directly by ALAWorker — read from api_jobs.result
        row = db.execute(
            "SELECT result FROM api_jobs WHERE id=(SELECT api_job_id FROM tasks WHERE id=?)",
            (task_id,)
        ).fetchone()
        if row and row[0]:
            return json.loads(row[0])
    elif task_type == 'text_chat':
        # text_chat result is stored directly in api_jobs.result
        row = db.execute(
            "SELECT result FROM api_jobs WHERE id=(SELECT api_job_id FROM tasks WHERE id=?)",
            (task_id,)
        ).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                return {"response": row[0]}
    return {}


def file_id(path, size, mtime):
    """Generate a stable ID from path + size + mtime."""
    raw = f"{path}|{size}|{mtime}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def folder_id(path):
    """Generate a stable folder ID."""
    return hashlib.sha256(path.encode()).hexdigest()[:16]


def write_scanner_state(state_dict):
    """Write scanner state to a shared file so the serve process can read it."""
    try:
        tmp = SCANNER_STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump({"state": state_dict, "updated": time.time()}, f)
        tmp.replace(SCANNER_STATE_FILE)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Media metadata extraction (ffprobe)
# ---------------------------------------------------------------------------

def probe_media(filepath):
    """Extract metadata using ffprobe. Returns dict or None on error."""
    try:
        cmd = [
            FFPROBE, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(filepath)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)

        info = {}
        fmt = data.get("format", {})
        info["duration"] = float(fmt.get("duration", 0))
        info["size"] = int(fmt.get("size", 0))
        info["codec"] = fmt.get("format_long_name", "")

        # Find video stream for resolution
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["width"] = stream.get("width")
                info["height"] = stream.get("height")
                info["codec"] = stream.get("codec_name", info["codec"])
                break

        return info
    except Exception as e:
        log.warning(f"ffprobe failed for {filepath}: {e}")
        return None

# ---------------------------------------------------------------------------
# Thumbnail / keyframe extraction (ffmpeg)
# ---------------------------------------------------------------------------

def _quick_duration(video_path):
    """Get video duration in seconds via ffprobe (metadata only, very fast)."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=10
        )
        return float(result.stdout.strip()) if result.returncode == 0 else 0
    except Exception:
        return 0


def extract_thumbnail(video_path, timestamp, output_path):
    """Extract a single frame from a video at the given timestamp."""
    def _file_ok(path):
        p = Path(path)
        return p.exists() and p.stat().st_size > 0

    def _run(cmd):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=THUMBNAIL_TIMEOUT)

    def _base_cmd(seek_args):
        return [FFMPEG, "-y"] + seek_args + [
            "-vframes", "1",
            "-vf", f"scale='min({MAX_IMAGE_DIMENSION},iw)':-2",
            "-q:v", "3",
            "-update", "1",
            str(output_path)
        ]

    try:
        # Attempt 1: fast seek (-ss before -i). Works for most timestamps but
        # can silently produce no output at very small timestamps — ffmpeg
        # seeks past the start, encodes nothing, exits 0. Detect via file check.
        r = _run(_base_cmd(["-ss", str(timestamp), "-i", str(video_path)]))
        if r.returncode == 0 and _file_ok(output_path):
            return True

        # Attempt 2: accurate seek (-ss after -i). Slower but handles small
        # timestamps that trip up fast seek.
        log.warning(f"extract_thumbnail: fast seek empty for {Path(video_path).name} "
                    f"at {timestamp}s, retrying with accurate seek")
        r2 = _run(_base_cmd(["-i", str(video_path), "-ss", str(timestamp)]))
        if r2.returncode == 0 and _file_ok(output_path):
            return True

        # Attempt 3: extract first frame (timestamp 0). Handles very short
        # files (e.g. 1-frame clips) where the computed timestamp falls past
        # the only decodable frame.
        if timestamp > 0:
            log.warning(f"extract_thumbnail: accurate seek also empty for {Path(video_path).name}, "
                        f"falling back to first frame")
            r3 = _run(_base_cmd(["-i", str(video_path), "-ss", "0"]))
            if r3.returncode == 0 and _file_ok(output_path):
                return True

        return False
    except Exception as e:
        log.warning(f"Thumbnail extraction failed: {e}")
        return False


def _parse_scene_cuts(stderr):
    """Parse showinfo output from ffmpeg stderr into a list of cut timestamps."""
    cuts = []
    for line in stderr.splitlines():
        if "showinfo" not in line:
            continue
        match = re.search(r'pts_time:([0-9.]+)', line)
        if match:
            cuts.append(float(match.group(1)))
    return cuts


def detect_scene_changes(video_path, duration, threshold=SCENE_CHANGE_THRESHOLD, vaapi_device=None):
    """Detect scene changes using ffmpeg's built-in scene detection.
    Uses VAAPI hardware decode (GPU decode ASIC) with CPU fallback.
    vaapi_device: explicit device path, or None for round-robin.
    Returns list of cut timestamps in seconds."""
    timeout = max(120, int(duration * 0.3))

    # Try VAAPI hardware decode first (uses dedicated decode hardware, not compute)
    if vaapi_device is None:
        vaapi_device = VAAPI_DEVICES[next(_vaapi_counter) % len(VAAPI_DEVICES)]
    if os.path.exists(vaapi_device):
        try:
            cmd = [
                FFMPEG,
                "-hwaccel", "vaapi",
                "-hwaccel_device", vaapi_device,
                "-i", str(video_path),
                "-an",
                "-vf", f"select='gt(scene,{threshold})',showinfo",
                "-f", "null", "-"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode == 0:
                cuts = _parse_scene_cuts(result.stderr)
                return cuts
            else:
                log.debug("VAAPI decode failed for %s (%s), falling back to CPU",
                          Path(video_path).name, vaapi_device)
        except subprocess.TimeoutExpired:
            log.warning("Scene detect timed out for %s (%.0fs video, VAAPI)",
                        Path(video_path).name, duration)
            return []
        except Exception:
            log.debug("VAAPI unavailable for %s (%s), falling back to CPU",
                      Path(video_path).name, vaapi_device)

    # CPU fallback
    try:
        cmd = [
            FFMPEG,
            "-i", str(video_path),
            "-an",
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return _parse_scene_cuts(result.stderr)

    except subprocess.TimeoutExpired:
        log.warning("Scene detect timed out for %s (%.0fs video)",
                    Path(video_path).name, duration)
        return []
    except Exception as e:
        log.warning("Scene detection failed for %s: %s", Path(video_path).name, e)
        return []


def select_keyframe_timestamps(cuts, duration):
    """Given cut timestamps, return one keyframe timestamp per scene.
    Picks 30% into each scene (avoids transitions at boundaries)."""
    if not cuts:
        return [duration * 0.5]

    # Build scene boundaries: [0, cut1, cut2, ..., duration]
    boundaries = [0.0] + sorted(cuts) + [duration]
    timestamps = []

    for i in range(len(boundaries) - 1):
        scene_start = boundaries[i]
        scene_end = boundaries[i + 1]
        scene_duration = scene_end - scene_start
        if scene_duration < 0.5:
            continue  # Skip tiny scenes (likely false detections)
        # Pick 30% into the scene
        ts = scene_start + scene_duration * 0.3
        timestamps.append(ts)

    return timestamps if timestamps else [duration * 0.5]


def extract_keyframes(video_path, duration, file_hash, precomputed_cuts=None):
    """Extract keyframes from a video using scene detection.
    precomputed_cuts: list of cut timestamps from SceneDetectPool, or None to detect inline.
    Returns list of (timestamp, thumbnail_path)."""
    thumb_subdir = THUMB_DIR / file_hash
    thumb_subdir.mkdir(parents=True, exist_ok=True)

    if duration <= 0:
        return []

    # Short videos: single mid-frame (not worth scene detection)
    if duration < 5:
        timestamps = [duration / 2]
        log.debug("Scene detect %s: short video (%.1fs), using mid-frame",
                  Path(video_path).name, duration)
    else:
        # Use precomputed cuts from SceneDetectPool, or detect inline (fallback)
        if precomputed_cuts is not None:
            cuts = precomputed_cuts
        else:
            cuts = detect_scene_changes(video_path, duration)
        timestamps = select_keyframe_timestamps(cuts, duration)
        if len(cuts) == 0:
            log.info("Scene detect %s: no cuts found (%.0fs), fallback to 1 keyframe",
                     Path(video_path).name, duration)
        else:
            log.info("Scene detect %s: %d cuts → %d keyframes (%.0fs video)",
                     Path(video_path).name, len(cuts), len(timestamps), duration)

    frames = []
    for i, ts in enumerate(timestamps):
        thumb_path = thumb_subdir / f"frame_{i:02d}.jpg"
        if extract_thumbnail(video_path, ts, thumb_path):
            frames.append((ts, str(thumb_path)))

    return frames


def has_audio_stream(media_path):
    """Return True if the file has at least one audio stream. Fast ffprobe check."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error",
             "-select_streams", "a:0",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(media_path)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT
        )
        return result.returncode == 0 and result.stdout.strip() == "audio"
    except Exception:
        return False


def extract_audio_for_transcription(media_path, output_path):
    """Extract audio track from video/audio file as 16kHz mono WAV for Whisper.
    Pre-checks for an audio stream via ffprobe to avoid a 120s timeout on files
    like DJI drone clips that have no audio track (fixes Issue #17)."""
    if not has_audio_stream(media_path):
        log.info("No audio stream in %s — skipping transcription" % Path(media_path).name)
        return False
    cmd = [
        FFMPEG, "-y",
        "-i", str(media_path),
        "-vn",                          # strip video
        "-ar", "16000",                 # 16kHz (Whisper optimal)
        "-ac", "1",                     # mono
        "-t", str(TRANSCRIPTION_MAX_SECONDS),
        "-f", "wav",
        str(output_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=AUDIO_EXTRACT_TIMEOUT)
        if result.returncode != 0:
            log.warning("Audio extraction failed for %s: %s" % (
                Path(media_path).name, result.stderr[:200]))
        return result.returncode == 0
    except Exception as e:
        log.warning("Audio extraction failed for %s: %s" % (media_path, e))
        return False


def _braw_info(file_path):
    """Get frame count, fps, duration from a BRAW file using braw-frame.
    Returns (frame_count, fps, duration_seconds) or None on failure."""
    if not Path(BRAW_FRAME).exists():
        return None
    try:
        result = subprocess.run(
            [BRAW_FRAME, "info", str(file_path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        m_frames = re.search(r'frames=(\d+)', result.stdout)
        m_fps    = re.search(r'fps=([\d.]+)', result.stdout)
        m_dur    = re.search(r'duration=([\d.]+)', result.stdout)
        if m_frames and m_fps and m_dur:
            return int(m_frames.group(1)), float(m_fps.group(1)), float(m_dur.group(1))
    except Exception as e:
        log.warning("braw_info failed for %s: %s" % (Path(file_path).name, e))
    return None


def _braw_extract_frame_jpeg(file_path, frame_idx, output_path):
    """Extract a single BRAW frame as JPEG via braw-frame + ffmpeg conversion.
    Returns True on success."""
    tmp_bmp = str(output_path) + ".braw_tmp.bmp"
    try:
        result = subprocess.run(
            [BRAW_FRAME, str(file_path), str(frame_idx), tmp_bmp],
            capture_output=True, timeout=90
        )
        if result.returncode != 0 or not Path(tmp_bmp).exists():
            return False
        r2 = subprocess.run(
            [FFMPEG, "-y", "-i", tmp_bmp,
             "-vf", "scale='min(%d,iw)':-2" % MAX_IMAGE_DIMENSION,
             "-q:v", "3", str(output_path)],
            capture_output=True, timeout=30
        )
        return r2.returncode == 0 and Path(output_path).exists()
    except Exception as e:
        log.warning("braw_extract_frame failed for %s frame %d: %s" % (
            Path(file_path).name, frame_idx, e))
        return False
    finally:
        try:
            Path(tmp_bmp).unlink(missing_ok=True)
        except Exception:
            pass


def _r3d_extract_frame_jpeg(file_path, frame_number, output_path):
    """Extract a single R3D frame as JPEG via REDline + ffmpeg resize.
    Returns True on success, False if frame is out-of-range or extraction fails."""
    import tempfile, shutil
    tmp_dir = tempfile.mkdtemp(prefix="r3d_")
    try:
        stem = os.path.join(tmp_dir, "frame")
        subprocess.run(
            [REDLINE, "--i", str(file_path),
             "--o", stem,
             "--format", "3",        # JPEG output
             "--res", "4",           # Quarter resolution (fast, 1/16 pixels)
             "--start", str(frame_number),
             "--end", str(frame_number)],
            capture_output=True, timeout=60
        )
        expected = Path(tmp_dir) / ("frame.%06d.jpg" % frame_number)
        if not expected.exists():
            return False
        r2 = subprocess.run(
            [FFMPEG, "-y", "-i", str(expected),
             "-vf", "scale='min(%d,iw)':-2" % MAX_IMAGE_DIMENSION,
             "-q:v", "3", str(output_path)],
            capture_output=True, timeout=30
        )
        return r2.returncode == 0 and Path(output_path).exists()
    except Exception as e:
        log.warning("r3d_extract_frame failed for %s frame %d: %s" % (
            Path(file_path).name, frame_number, e))
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def transcribe_audio(audio_path, timeout=WHISPER_TIMEOUT):
    """Send audio WAV to Whisper server, return (text, segments) or (None, []).

    segments is a list of {"start": float, "end": float, "text": str} dicts
    with timestamps relative to the start of this audio chunk.
    """
    boundary = "----WhisperBoundary7f3a9b"
    try:
        with open(audio_path, "rb") as f:
            audio_data = f.read()
    except Exception as e:
        log.warning("Cannot read audio file %s: %s" % (audio_path, e))
        return (None, [])

    body = (
        "--%s\r\n" % boundary +
        'Content-Disposition: form-data; name="file"; filename="%s"\r\n' % Path(audio_path).name +
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + audio_data + (
        "\r\n--%s\r\n" % boundary +
        'Content-Disposition: form-data; name="response_format"\r\n\r\n'
        "verbose_json"
        "\r\n--%s--\r\n" % boundary
    ).encode()

    req = urllib.request.Request(
        "%s/inference" % WHISPER_SERVER,
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary}
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(resp.read())

        # Extract segments with timestamps
        segments = []
        raw_segments = result.get("segments", [])
        for seg in raw_segments:
            segments.append({
                "start": seg.get("t0", seg.get("start", 0)),
                "end": seg.get("t1", seg.get("end", 0)),
                "text": seg.get("text", "").strip()
            })

        # Build full text from segments if available, else fall back to top-level text
        if segments:
            text = " ".join(s["text"] for s in segments if s["text"]).strip()
        else:
            text = result.get("text", "").strip()

        return (text if text else None, segments)
    except Exception as e:
        log.warning("Whisper transcription failed: %s" % e)
        return (None, [])


def get_audio_duration(audio_path):
    """Return audio duration in seconds using ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def split_audio_into_chunks(audio_path, chunk_seconds=WHISPER_CHUNK_SECONDS):
    """Split a WAV file into chunk_seconds-length pieces.

    Returns a list of (path, start_secs) tuples. If the file is shorter than
    chunk_seconds, returns [(audio_path, 0)] — no split performed.
    Caller is responsible for deleting chunk files (not the original).
    """
    duration = get_audio_duration(audio_path)
    if duration is None or duration <= chunk_seconds:
        return [(Path(audio_path), 0)]

    chunks = []
    stem = Path(audio_path).stem
    parent = Path(audio_path).parent
    start = 0
    idx = 0
    while start < int(duration):
        chunk_path = parent / ("%s_c%03d.wav" % (stem, idx))
        cmd = [FFMPEG, "-y", "-i", str(audio_path),
               "-ss", str(start), "-t", str(chunk_seconds),
               "-c", "copy", str(chunk_path)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and chunk_path.exists():
                chunks.append((chunk_path, start))
        except Exception as e:
            log.warning("Audio chunk split failed at %ds: %s" % (start, e))
        start += chunk_seconds
        idx += 1

    return chunks if chunks else [(Path(audio_path), 0)]


# ---------------------------------------------------------------------------
# Image pre-encoding (runs in CPU prep thread)
# ---------------------------------------------------------------------------

MIME_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "tif": "image/tiff", "tiff": "image/tiff", "bmp": "image/bmp",
    "webp": "image/webp"
}

# llama.cpp vision only accepts JPEG, PNG, WebP — convert these via ffmpeg first
_CONVERT_TO_JPEG = {"tif", "tiff", "bmp"}

def pre_encode_image(image_path):
    """Read and base64-encode an image file. Called in prep thread so GPU
    thread never waits for file I/O or encoding.

    TIFF and BMP are converted to JPEG via ffmpeg — llama.cpp vision rejects
    image/tiff and image/bmp with HTTP 400."""
    try:
        ext = Path(image_path).suffix.lower().lstrip(".")
        if ext in _CONVERT_TO_JPEG:
            result = subprocess.run(
                [FFMPEG, "-y", "-i", str(image_path),
                 "-vf", "scale='min(%d,iw)':-2" % MAX_IMAGE_DIMENSION,
                 "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                capture_output=True, timeout=30
            )
            if result.returncode != 0 or not result.stdout:
                log.warning("pre_encode_image: ffmpeg conversion failed for %s" % Path(image_path).name)
                return None, None
            img_b64 = base64.b64encode(result.stdout).decode()
            return img_b64, "image/jpeg"
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        mime = MIME_TYPES.get(ext, "image/jpeg")
        img_b64 = base64.b64encode(img_bytes).decode()
        return img_b64, mime
    except Exception as e:
        log.warning("Failed to pre-encode %s: %s" % (Path(image_path).name, e))
        return None, None

# ---------------------------------------------------------------------------
# LLM Vision API
# ---------------------------------------------------------------------------

def describe_image(image_path, context="", llm_server=None, image_b64=None, image_mime=None):
    """Send an image to Gemma 3 12B and get a description.
    If image_b64/image_mime are provided (pre-encoded by prep thread),
    skips file I/O and encoding."""
    if llm_server is None:
        llm_server = LLM_SERVERS[0] if LLM_SERVERS else LLM_FALLBACK
    try:
        if image_b64 is not None:
            img_b64 = image_b64
            mime = image_mime
        else:
            # Fallback: read and encode on the fly
            with open(image_path, "rb") as f:
                img_bytes = f.read()
            ext = Path(image_path).suffix.lower()
            mime = MIME_TYPES.get(ext.lstrip("."), "image/jpeg")
            img_b64 = base64.b64encode(img_bytes).decode()

        prompt = (
            "Describe this image concisely for a searchable media database. "
            "Only describe what is PRESENT — do not mention absent elements. "
            "Include: main subject, setting/location type, lighting, colors, "
            "people (count and activity if any), equipment or objects visible, "
            "camera angle, and mood. Use keywords that someone might search for. "
            "Write a plain paragraph, not a list with labels. "
            "Keep it under 100 words."
        )
        if context:
            prompt += f"\nContext: This file is from: {context}"

        req_data = json.dumps({
            "model": "gemma-3-12b",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    {"type": "text", "text": prompt}
                ]
            }],
            "max_tokens": 150
        }).encode()

        req = urllib.request.Request(
            "%s/v1/chat/completions" % llm_server,
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=VISION_TIMEOUT)
        result = json.loads(resp.read())

        content = result["choices"][0]["message"]["content"]
        return content.strip()

    except urllib.error.URLError as e:
        log.error("LLM server not reachable (%s): %s" % (llm_server, e))
        return None
    except Exception as e:
        log.error("Vision description failed for %s (server: %s): %s" % (image_path, llm_server, e))
        return None


def send_text_prompt(prompt, llm_server=None, max_tokens=200, temperature=0.3):
    """Send a text-only prompt to Gemma and return the response text.
    Used by the text_chat API task type for classification, summarization, etc."""
    if llm_server is None:
        llm_server = PRO580X_GEMMA
    try:
        req_data = json.dumps({
            "model": "gemma-3-12b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(max_tokens) if max_tokens else 200,
            "temperature": float(temperature) if temperature else 0.3,
        }).encode()
        req = urllib.request.Request(
            "%s/v1/chat/completions" % llm_server,
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read())
        return result["choices"][0]["message"]["content"].strip()
    except urllib.error.URLError as e:
        log.error("LLM server not reachable (%s): %s" % (llm_server, e))
        return None
    except Exception as e:
        log.error("Text prompt failed (server: %s): %s" % (llm_server, e))
        return None


def verify_face_match(reference_path, candidate_path, llm_server=None):
    """Ask Gemma if two face thumbnails show the same person.
    Sends both images to Gemma's vision API.
    Returns True if Gemma confirms match, False if not or on error."""
    if llm_server is None:
        llm_server = PRO580X_GEMMA
    try:
        # Load and encode both images
        images = []
        for path in (reference_path, candidate_path):
            if not path or not os.path.exists(path):
                return False
            with open(path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            images.append(img_b64)

        req_data = json.dumps({
            "model": "gemma-3-12b",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,%s" % images[0]}},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,%s" % images[1]}},
                    {"type": "text", "text":
                        "Image 1 is a reference photo of a person. Image 2 is a candidate photo. "
                        "Are these the SAME person? Consider face shape, features, and overall appearance. "
                        "Ignore differences in lighting, angle, expression, and image quality. "
                        "Reply with ONLY the word YES or NO."}
                ]
            }],
            "max_tokens": 5,
            "temperature": 0.1,
        }).encode()

        req = urllib.request.Request(
            "%s/v1/chat/completions" % llm_server,
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read())
        answer = result["choices"][0]["message"]["content"].strip().upper()
        return "YES" in answer

    except Exception as e:
        log.warning("verify_face_match failed: %s" % e)
        return False  # On error, don't assign (conservative)


def describe_audio_filename(filepath, context="", llm_server=None):
    """For audio files without vision, describe based on filename and metadata."""
    if llm_server is None:
        llm_server = LLM_SERVERS[0] if LLM_SERVERS else LLM_FALLBACK
    try:
        prompt = (
            f"Based on this audio filename and path, generate a brief description "
            f"and searchable tags for a church production media database. "
            f"Infer what you can about the content from the name.\n"
            f"Filename: {Path(filepath).name}\n"
            f"Path: {filepath}\n"
            f"Generate a one-line description and comma-separated tags."
        )

        req_data = json.dumps({
            "model": "gemma-3-12b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 100
        }).encode()

        req = urllib.request.Request(
            "%s/v1/chat/completions" % llm_server,
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=AUDIO_DESCRIPTION_TIMEOUT)
        result = json.loads(resp.read())
        return result["choices"][0]["message"]["content"].strip()

    except Exception as e:
        log.error("Audio description failed for %s (server: %s): %s" % (filepath, llm_server, e))
        return None

# ---------------------------------------------------------------------------
# Face detection and recognition
# ---------------------------------------------------------------------------

def detect_faces(image_path):
    """Detect faces in an image, return list of (encoding, bbox) tuples.

    Each encoding is a 128-D numpy float64 array.
    Each bbox is (top, right, bottom, left) in pixels.
    Returns empty list if no faces found or face_recognition not available.
    """
    if not HAS_FACE_RECOGNITION:
        return []

    try:
        image = face_recognition.load_image_file(str(image_path))
        locations = face_recognition.face_locations(image, model="hog")

        # Filter out tiny faces that are too blurry for reliable encoding
        valid = []
        for loc in locations:
            top, right, bottom, left = loc
            height = bottom - top
            width = right - left
            if height >= MIN_FACE_SIZE and width >= MIN_FACE_SIZE:
                valid.append(loc)

        if not valid:
            return []

        encodings = face_recognition.face_encodings(image, known_face_locations=valid)
        return list(zip(encodings, valid))

    except Exception as e:
        log.warning("Face detection failed for %s: %s" % (str(image_path), str(e)))
        return []


def _detect_faces_worker(image_path):
    """Spawn-safe worker for multiprocessing Pool.

    Returns (image_path, [(encoding_bytes, bbox), ...]) so results are pickle-safe.
    Encoding is converted to bytes since numpy arrays pickle fine but this is explicit.
    """
    try:
        import face_recognition as fr
        import numpy as _np
        image = fr.load_image_file(str(image_path))
        locations = fr.face_locations(image, model="hog")
        valid = []
        for loc in locations:
            top, right, bottom, left = loc
            if (bottom - top) >= 40 and (right - left) >= 40:
                valid.append(loc)
        if not valid:
            return (str(image_path), [])
        encodings = fr.face_encodings(image, known_face_locations=valid)
        results = []
        for enc, bbox in zip(encodings, valid):
            results.append((enc.tobytes(), bbox))
        return (str(image_path), results)
    except Exception:
        return (str(image_path), [])


def save_face_crop(image_path, bbox, output_path):
    """Crop and save a face thumbnail from an image using ffmpeg.

    bbox is (top, right, bottom, left).
    Returns True on success.
    """
    top, right, bottom, left = bbox
    height = bottom - top
    width = right - left

    # Add padding around the face
    pad_h = int(height * FACE_CROP_PADDING)
    pad_w = int(width * FACE_CROP_PADDING)

    crop_x = max(0, left - pad_w)
    crop_y = max(0, top - pad_h)
    crop_w = width + 2 * pad_w
    crop_h = height + 2 * pad_h

    crop_str = "crop=%d:%d:%d:%d,scale=%d:%d" % (
        crop_w, crop_h, crop_x, crop_y,
        FACE_CROP_SIZE, FACE_CROP_SIZE
    )

    try:
        cmd = [
            FFMPEG, "-y", "-i", str(image_path),
            "-vf", crop_str,
            "-q:v", "3",
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception as e:
        log.warning("Face crop failed: %s" % str(e))
        return False


def store_faces(db, file_id, image_path, faces, keyframe_id=None):
    """Store detected faces in the database.

    faces: list of (encoding, bbox) from detect_faces()
    Returns number of faces stored.
    """
    FACE_THUMB_DIR.mkdir(parents=True, exist_ok=True)
    stored = 0

    for i, (encoding, bbox) in enumerate(faces):
        face_id = "%s_%d" % (file_id, i)

        # Save face crop thumbnail
        face_dir = FACE_THUMB_DIR / file_id
        face_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = face_dir / ("face_%02d.jpg" % i)
        save_face_crop(image_path, bbox, thumb_path)

        top, right, bottom, left = bbox

        # Store embedding as BLOB (128 float64 values = 1024 bytes)
        embedding_blob = encoding.tobytes()

        db.execute("""
            INSERT OR REPLACE INTO faces
            (id, file_id, keyframe_id, embedding,
             bbox_top, bbox_right, bbox_bottom, bbox_left,
             thumbnail_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            face_id, file_id, keyframe_id, embedding_blob,
            top, right, bottom, left,
            str(thumb_path), datetime.now().isoformat()
        ))
        stored += 1

    db.commit()
    return stored


def load_embedding(blob):
    """Convert a BLOB back to a 128-D numpy array."""
    return np.frombuffer(blob, dtype=np.float64)


# ---------------------------------------------------------------------------
# File crawling
# ---------------------------------------------------------------------------

def crawl_folder(folder_path):
    """Walk a folder and yield all media files."""
    folder = Path(folder_path)
    if not folder.exists():
        log.warning(f"Folder not found: {folder_path}")
        return

    for root, dirs, files in os.walk(folder):
        # Skip hidden/system directories
        dirs[:] = [d for d in dirs if d.upper() not in {s.upper() for s in SKIP_DIRS} and not d.startswith("._")]

        for fname in files:
            if fname.startswith("._") or fname == ".DS_Store":
                continue
            ext = Path(fname).suffix.lower()
            if ext in ALL_MEDIA_EXTS:
                yield Path(root) / fname


def get_file_type(ext):
    """Classify file extension into image/video/audio."""
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "image"
    elif ext in VIDEO_EXTS:
        return "video"
    elif ext in AUDIO_EXTS:
        return "audio"
    return "unknown"

# ---------------------------------------------------------------------------
# Perpetual Task Pipeline — helpers, workers, coordinator
# ---------------------------------------------------------------------------

# Task types enabled in the current phase.
# Phase 1: transcription only.  Phase 2 adds scene_detect.
ENABLED_TASK_TYPES = {"transcribe", "scene_detect", "visual_analysis", "face_detect"}


def _assemble_file_description(db, file_id, file_path):
    """Assemble files.ai_description from all keyframe descriptions.
    Only writes if ALL keyframes for the file have been described.
    Called by GemmaWorker after each keyframe and by TaskCoordinator._finalize()."""
    rows = db.execute(
        "SELECT ai_description FROM keyframes WHERE file_id=? ORDER BY timestamp_seconds",
        (file_id,)).fetchall()
    if not rows:
        return
    descriptions = [r[0] for r in rows if r[0]]
    if len(descriptions) < len(rows):
        return  # not all done yet
    ai_desc = descriptions[0] if len(descriptions) == 1 else " | ".join(descriptions)
    path_parts = [p for p in Path(file_path).parts
                  if p not in ("/", "mnt", "vault", "Volumes", "Vault")]
    tags = ", ".join(path_parts[:-1])
    with _db_write_lock:
        db.execute("UPDATE files SET ai_description=?, tags=? WHERE id=?",
                   (ai_desc, tags, file_id))
        db.commit()
    log.info("GemmaWorker: assembled description for %s (%d keyframe%s)" % (
        Path(file_path).name, len(descriptions), "s" if len(descriptions) != 1 else ""))


def _create_task(db, file_id, task_type):
    """Insert a task row if it doesn't already exist (idempotent)."""
    task_id = "%s_%s" % (file_id, task_type)
    db.execute("""
        INSERT OR IGNORE INTO tasks (id, file_id, task_type, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
    """, (task_id, file_id, task_type, datetime.now().isoformat()))


def _create_keyframe_task(db, keyframe_id, file_id, task_type):
    """Insert a keyframe-level task (visual_analysis or face_detect).
    task_id uses keyframe_id so multiple keyframes per file each get their own task.
    file_id is the parent file so TaskCoordinator can track all tasks for one file."""
    task_id = "%s_%s" % (keyframe_id, task_type)
    db.execute("""
        INSERT OR IGNORE INTO tasks (id, file_id, task_type, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
    """, (task_id, file_id, task_type, datetime.now().isoformat()))


def _create_tasks_for_file(db, file_id, file_type, file_path=None):
    """Create all enabled tasks for a file based on its type.
    file_path is optional; used to skip scene_detect for formats ffmpeg can't decode."""
    if file_type == "video":
        if "transcribe" in ENABLED_TASK_TYPES:
            _create_task(db, file_id, "transcribe")
        if "scene_detect" in ENABLED_TASK_TYPES:
            _create_task(db, file_id, "scene_detect")
    elif file_type == "audio":
        if "transcribe" in ENABLED_TASK_TYPES:
            _create_task(db, file_id, "transcribe")
    elif file_type == "image":
        if "visual_analysis" in ENABLED_TASK_TYPES:
            _create_task(db, file_id, "visual_analysis")
        if "face_detect" in ENABLED_TASK_TYPES:
            _create_task(db, file_id, "face_detect")
    db.commit()


def _remove_file_cascade(db, file_id, file_path):
    """Remove a file and all its data from the DB and thumbnail directories.
    Tasks, keyframes, and faces cascade automatically via FK ON DELETE CASCADE.
    Thumbnails on disk must be cleaned up explicitly."""
    # Clean up thumbnail directory (thumbnails/{file_id}/)
    thumb_subdir = THUMB_DIR / file_id
    if thumb_subdir.exists():
        try:
            import shutil
            shutil.rmtree(str(thumb_subdir))
        except Exception as e:
            log.warning("Could not remove thumbnails for %s: %s" % (file_id, e))

    # Clean up face thumbnails referencing this file
    try:
        face_thumbs = db.execute(
            "SELECT thumbnail_path FROM faces WHERE file_id = ?", (file_id,)
        ).fetchall()
        for (tp,) in face_thumbs:
            if tp and Path(tp).exists():
                try:
                    Path(tp).unlink()
                except Exception:
                    pass
    except Exception:
        pass

    # Delete file record — tasks/keyframes/faces cascade
    with _db_write_lock:
        db.execute("DELETE FROM files WHERE id = ?", (file_id,))
        db.commit()

    log.info("Removed from DB: %s" % Path(file_path).name)


class CrawlerWorker:
    """Perpetual crawler — walks NAS folders on a timer, registers new files as tasks,
    and removes DB records for files that no longer exist on the NAS."""

    def __init__(self, db_path, folders, interval=RESCAN_INTERVAL):
        self.db_path = db_path
        self.folders = [str(Path(f).resolve()) for f in folders]
        self.interval = interval
        self.running = True
        self._thread = None

    def _get_db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def crawl_once(self, db):
        """One full crawl: discover new files, check for removals."""
        for folder_path in self.folders:
            self._scan_for_new(db, folder_path)
            self._check_removals(db, folder_path)
        self._backfill_tasks(db)
        self._backfill_transcribe(db)
        self._backfill_scene_detect(db)
        self._backfill_visual_analysis(db)
        self._backfill_face_detect(db)

    def _scan_for_new(self, db, folder_path):
        """Walk folder, register new files and create their tasks."""
        folder_path = Path(folder_path)
        if not folder_path.exists():
            log.warning("CrawlerWorker: folder not accessible: %s" % folder_path)
            return

        fid = folder_id(str(folder_path))
        # Ensure folder record exists
        db.execute("""
            INSERT OR IGNORE INTO folders (id, path, name)
            VALUES (?, ?, ?)
        """, (fid, str(folder_path), folder_path.name))
        db.commit()

        new_count = 0
        task_count = 0
        for media_path in crawl_folder(str(folder_path)):
            if not self.running:
                break
            try:
                stat = media_path.stat()
            except OSError:
                continue

            mid = file_id(str(media_path), stat.st_size, stat.st_mtime)
            ext = media_path.suffix.lower()
            ftype = get_file_type(ext)

            existing = db.execute(
                "SELECT status FROM files WHERE id = ?", (mid,)
            ).fetchone()

            if existing is None:
                # New file — register it
                db.execute("""
                    INSERT OR IGNORE INTO files (id, path, filename, folder_id, file_type, status)
                    VALUES (?, ?, ?, ?, ?, 'pending')
                """, (mid, str(media_path), media_path.name, fid, ftype))
                db.commit()
                _create_tasks_for_file(db, mid, ftype, file_path=str(media_path))
                new_count += 1
                task_count += len([t for t in ENABLED_TASK_TYPES
                                   if (ftype == "video" and t in {"transcribe", "scene_detect"})
                                   or (ftype == "audio" and t == "transcribe")
                                   or (ftype == "image" and t in {"visual_analysis", "face_detect"})])
                log.info("CrawlerWorker: new — %s" % media_path.name)

        if new_count:
            db.execute("""
                UPDATE folders SET last_scan = ?, file_count = file_count + ?
                WHERE id = ?
            """, (datetime.now().isoformat(), new_count, fid))
            db.commit()
            log.info("CrawlerWorker: %s — %d new files, %d tasks created" % (
                folder_path.name, new_count, task_count))
        else:
            db.execute("UPDATE folders SET last_scan = ? WHERE id = ?",
                       (datetime.now().isoformat(), fid))
            db.commit()
            log.info("CrawlerWorker: %s — no new files" % folder_path.name)

    def _check_removals(self, db, folder_path):
        """Find DB records whose file no longer exists on NAS and remove them."""
        folder_path = Path(folder_path)
        fid = folder_id(str(folder_path))
        db_files = db.execute(
            "SELECT id, path FROM files WHERE folder_id = ?", (fid,)
        ).fetchall()

        removed = 0
        for mid, fpath in db_files:
            if not Path(fpath).exists():
                _remove_file_cascade(db, mid, fpath)
                removed += 1

        if removed:
            log.info("CrawlerWorker: %s — removed %d deleted files" % (
                folder_path.name, removed))

    def _backfill_tasks(self, db):
        """Create tasks for any pending files that were registered before the
        Perpetual Task Pipeline was added (i.e., have no tasks yet).
        Processes up to 1000 files per crawl cycle to keep each cycle bounded."""
        rows = db.execute("""
            SELECT f.id, f.file_type, f.path FROM files f
            WHERE f.status = 'pending'
            AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.file_id = f.id)
            LIMIT 1000
        """).fetchall()

        if not rows:
            return

        created = 0
        for fid, ftype, fpath in rows:
            if ftype is None:
                # Old files didn't have file_type stored — infer from extension
                ftype = get_file_type(Path(fpath).suffix.lower())
            if ftype in ('video', 'audio', 'image'):
                _create_tasks_for_file(db, fid, ftype, file_path=fpath)
                created += 1

        if created:
            log.info("CrawlerWorker: backfilled %d tasks (%d files checked)" % (created, len(rows)))

    def _backfill_transcribe(self, db):
        """Create transcribe tasks for video/audio files registered before file_type was tracked.
        These are files with file_type=NULL whose extension resolves to video or audio.
        The generic _backfill_tasks picks files in insertion order and hits lots of images first;
        this method specifically targets video/audio so Whisper isn't starved.
        Fetches a large candidate batch and filters by extension in Python to avoid
        SQLite extension-parsing gymnastics. Processes up to 500 tasks per crawl cycle."""
        if "transcribe" not in ENABLED_TASK_TYPES:
            return
        # Fetch untyped files with no transcribe task — overfetch since many will be images
        candidates = db.execute("""
            SELECT f.id, f.path FROM files f
            WHERE f.file_type IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tasks t WHERE t.file_id = f.id AND t.task_type = 'transcribe'
              )
            ORDER BY f.id ASC
            LIMIT 5000
        """).fetchall()
        if not candidates:
            return
        now = datetime.now().isoformat()
        created = 0
        for fid, fpath in candidates:
            if created >= 500:
                break
            ext = Path(fpath).suffix.lower()
            ftype = get_file_type(ext)
            if ftype not in ('video', 'audio'):
                continue
            db.execute("UPDATE files SET file_type=? WHERE id=?", (ftype, fid))
            task_id = "%s_transcribe" % fid
            db.execute("""
                INSERT OR IGNORE INTO tasks (id, file_id, task_type, status, created_at)
                VALUES (?, ?, 'transcribe', 'pending', ?)
            """, (task_id, fid, now))
            created += 1
        db.commit()
        if created:
            log.info("CrawlerWorker: backfilled %d transcribe tasks for untyped video/audio" % created)

    def _backfill_scene_detect(self, db):
        """Create scene_detect tasks for indexed videos that predate Phase 2.
        These are videos fully processed by Phase 1 (transcript done, status=indexed)
        but registered before scene_detect was added to ENABLED_TASK_TYPES.
        Processes up to 200 files per crawl cycle so the queue drains gradually.
        Skips formats ffmpeg can't decode (R3D, BRAW) — they would always fail."""
        if "scene_detect" not in ENABLED_TASK_TYPES:
            return
        rows = db.execute("""
            SELECT f.id FROM files f
            WHERE f.file_type = 'video'
              AND NOT EXISTS (
                  SELECT 1 FROM tasks t
                  WHERE t.file_id = f.id AND t.task_type = 'scene_detect'
              )
            ORDER BY f.indexed_at DESC
            LIMIT 200
        """).fetchall()
        if not rows:
            return
        now = datetime.now().isoformat()
        created = 0
        for (fid,) in rows:
            task_id = "%s_scene_detect" % fid
            db.execute("""
                INSERT OR IGNORE INTO tasks (id, file_id, task_type, status, created_at)
                VALUES (?, ?, 'scene_detect', 'pending', ?)
            """, (task_id, fid, now))
            created += 1
        db.commit()
        if created:
            log.info("CrawlerWorker: backfilled %d scene_detect tasks for indexed videos" % created)

    def _backfill_visual_analysis(self, db):
        """Create visual_analysis tasks for keyframes that predate Phase 3.
        These are keyframes with no ai_description and no existing visual_analysis task.
        Processes up to 500 keyframes per crawl cycle so the queue drains gradually."""
        if "visual_analysis" not in ENABLED_TASK_TYPES:
            return
        rows = db.execute("""
            SELECT k.id, k.file_id FROM keyframes k
            WHERE k.ai_description IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tasks t WHERE t.id = k.id || '_visual_analysis'
              )
            ORDER BY k.id
            LIMIT 500
        """).fetchall()
        if not rows:
            return
        for keyframe_id, file_id in rows:
            _create_keyframe_task(db, keyframe_id, file_id, "visual_analysis")
        db.commit()
        log.info("CrawlerWorker: backfilled %d visual_analysis tasks" % len(rows))

    def _backfill_face_detect(self, db):
        if "face_detect" not in ENABLED_TASK_TYPES:
            return
        rows = db.execute("""
            SELECT k.id, k.file_id FROM keyframes k
            WHERE NOT EXISTS (
                SELECT 1 FROM tasks t WHERE t.id = k.id || '_face_detect'
            )
            ORDER BY k.id
            LIMIT 500
        """).fetchall()
        if not rows:
            return
        for keyframe_id, file_id in rows:
            _create_keyframe_task(db, keyframe_id, file_id, "face_detect")
        db.commit()
        log.info("CrawlerWorker: backfilled %d face_detect tasks" % len(rows))

    def run(self):
        """Loop forever: crawl, sleep, repeat."""
        db = self._get_db()
        log.info("CrawlerWorker: started (interval=%ds, folders=%s)" % (
            self.interval, ", ".join(self.folders)))

        idle_warned = False
        while self.running:
            try:
                log.info("CrawlerWorker: crawl cycle starting")
                t0 = time.time()
                self.crawl_once(db)
                log.info("CrawlerWorker: crawl complete (%.1fs)" % (time.time() - t0))
                idle_warned = False
                # Issue #37: periodic WAL checkpoint to prevent bloat
                try:
                    db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception:
                    pass
            except Exception as e:
                log.error("CrawlerWorker: error during crawl: %s" % e)

            # Sleep in 1s increments so shutdown is responsive
            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)

        db.close()
        log.info("CrawlerWorker: stopped")

    def start(self):
        self._thread = threading.Thread(target=self.run, name="crawler", daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False


class PerpetualWhisperWorker:
    """Perpetual Whisper worker for the task pipeline.
    Claims 'transcribe' tasks from the tasks table, processes them,
    and marks them complete. Loops forever.

    When an orchestrator is provided, requests Whisper model swap before
    processing and releases after each task (supports batching and API
    interruption).

    Prefetch: while transcribing file N, audio for file N+1 is extracted
    in a background thread so the GPU has no idle gap between jobs."""

    WORKER_ID = "whisper-%d" % os.getpid()

    def __init__(self, db_path, indexer_running, orchestrator=None):
        self.db_path = db_path
        self.indexer_running = indexer_running
        self.orchestrator = orchestrator
        self._thread = None
        self.processed = 0
        self.errors = 0
        # Prefetch state keyed by file_id:
        #   None  = extraction in progress
        #   False = no audio stream in this file
        #   Path  = ready, points to extracted .wav
        self._prefetch = {}
        self._prefetch_lock = threading.Lock()

    def _get_db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _claim_next(self, db):
        """Atomically claim the next pending transcribe task.
        Returns (task_id, file_id, file_path) or None if queue is empty."""
        now = datetime.now().isoformat()
        with db:
            row = db.execute("""
                SELECT t.id, t.file_id, f.path
                FROM tasks t
                JOIN files f ON t.file_id = f.id
                WHERE t.task_type = 'transcribe'
                  AND t.status = 'pending'
                ORDER BY (CASE WHEN t.source = 'api' THEN 0 ELSE 1 END), t.created_at ASC
                LIMIT 1
            """).fetchone()
            if row is None:
                return None
            task_id, file_id, file_path = row
            updated = db.execute("""
                UPDATE tasks SET status='assigned', worker_id=?, started_at=?
                WHERE id=? AND status='pending'
            """, (self.WORKER_ID, now, task_id)).rowcount
            if updated == 0:
                return None  # Race: another worker claimed it first
            _update_api_job(db, task_id, 'assigned')
        return task_id, file_id, file_path

    def _start_prefetch(self, skip_file_id):
        """Peek at the next pending task and pre-extract its audio in a daemon thread.
        Called immediately after claiming a task so extraction overlaps with transcription."""
        try:
            db = self._get_db()
            row = db.execute("""
                SELECT t.file_id, f.path FROM tasks t
                JOIN files f ON t.file_id = f.id
                WHERE t.task_type = 'transcribe' AND t.status = 'pending'
                  AND t.file_id != ?
                ORDER BY (CASE WHEN t.source = 'api' THEN 0 ELSE 1 END), t.created_at ASC LIMIT 1
            """, (skip_file_id,)).fetchone()
            db.close()
        except Exception:
            return

        if row is None:
            return

        next_file_id, next_file_path = row

        with self._prefetch_lock:
            if next_file_id in self._prefetch:
                return  # already prefetching or done
            self._prefetch[next_file_id] = None  # mark in-progress

        def extract():
            audio_tmp = DATA_DIR / ("tmp_pwhisper_pre_%s.wav" % next_file_id)
            try:
                if not Path(next_file_path).exists():
                    result = False
                else:
                    ok = extract_audio_for_transcription(str(next_file_path), str(audio_tmp))
                    result = audio_tmp if ok else False
            except Exception as e:
                log.debug("PerpetualWhisperWorker: prefetch error — %s: %s" % (
                    Path(next_file_path).name, e))
                result = False

            with self._prefetch_lock:
                if next_file_id in self._prefetch:
                    # Main thread hasn't claimed this file yet — store result
                    self._prefetch[next_file_id] = result
                    if result:
                        log.info("PerpetualWhisperWorker: prefetch ready — %s" % Path(next_file_path).name)
                else:
                    # Main thread already processed this file without us — clean up
                    if result and Path(result).exists():
                        try:
                            Path(result).unlink()
                        except Exception:
                            pass

        threading.Thread(target=extract, daemon=True, name="whisper-prefetch").start()

    def _process(self, db, task_id, file_id, file_path):
        """Transcribe one file. Uses prefetched audio if available."""
        fname = Path(file_path).name
        log.info("PerpetualWhisperWorker: starting — %s [task %s]" % (fname, task_id))

        audio_tmp = DATA_DIR / ("tmp_pwhisper_%s.wav" % file_id)
        try:
            if not Path(file_path).exists():
                log.warning("PerpetualWhisperWorker: file gone — %s" % fname)
                self._mark(db, task_id, "failed", "file not found on NAS")
                return

            # Check prefetch state
            with self._prefetch_lock:
                prefetch_state = self._prefetch.pop(file_id, "MISSING")

            if prefetch_state == "MISSING" or prefetch_state is None:
                # Not prefetched (or prefetch still in flight) — extract now
                TRANSCRIBE_HEARTBEAT.touch()
                if not extract_audio_for_transcription(str(file_path), str(audio_tmp)):
                    log.info("PerpetualWhisperWorker: no audio stream — %s" % fname)
                    self._mark(db, task_id, "complete")
                    return
            elif prefetch_state is False:
                # Prefetch found no audio stream
                log.info("PerpetualWhisperWorker: no audio stream — %s" % fname)
                self._mark(db, task_id, "complete")
                return
            else:
                # Prefetch ready — use the pre-extracted file, no GPU gap
                audio_tmp = prefetch_state
                log.info("PerpetualWhisperWorker: prefetch hit — %s" % fname)

            TRANSCRIBE_HEARTBEAT.touch()
            chunks = split_audio_into_chunks(audio_tmp)
            chunk_texts = []
            all_segments = []
            for i, (chunk_path, start_secs) in enumerate(chunks):
                log.info("PerpetualWhisperWorker: transcribing %s [chunk %d/%d, t=%.0fs]" % (
                    fname, i + 1, len(chunks), start_secs))
                TRANSCRIBE_HEARTBEAT.touch()
                text, segments = transcribe_audio(str(chunk_path), timeout=WHISPER_TIMEOUT)
                if text:
                    chunk_texts.append(text)
                # Offset segment timestamps by the chunk's start position
                for seg in segments:
                    seg["start"] += start_secs
                    seg["end"] += start_secs
                all_segments.extend(segments)
                if chunk_path != audio_tmp and chunk_path.exists():
                    try:
                        chunk_path.unlink()
                    except Exception:
                        pass

            transcript = " ".join(chunk_texts).strip() or None
            transcript_segments_json = json.dumps(all_segments) if all_segments else None
            if transcript:
                log.info("PerpetualWhisperWorker: %s → %d chars (%d chunks, %d segments)" % (
                    fname, len(transcript), len(chunks), len(all_segments)))
            else:
                log.info("PerpetualWhisperWorker: no speech detected — %s" % fname)

            with _db_write_lock:
                db.execute("UPDATE files SET transcript=?, transcript_segments=? WHERE id=?",
                           (transcript, transcript_segments_json, file_id))
                db.commit()

            self._mark(db, task_id, "complete")
            self.processed += 1

        except Exception as e:
            log.error("PerpetualWhisperWorker: error on %s: %s" % (fname, e))
            self._mark(db, task_id, "failed", str(e)[:500])
            self.errors += 1
        finally:
            if audio_tmp.exists():
                try:
                    audio_tmp.unlink()
                except Exception:
                    pass

    def _mark(self, db, task_id, status, error=None):
        with _db_write_lock:
            db.execute("""
                UPDATE tasks SET status=?, completed_at=?, error_message=?
                WHERE id=?
            """, (status, datetime.now().isoformat(), error, task_id))
            db.commit()
            _update_api_job(db, task_id, status, error=error)

    def run(self):
        db = self._get_db()
        log.info("PerpetualWhisperWorker: started")
        idle_logged = False

        while self.indexer_running():
            try:
                task = self._claim_next(db)
                if task is None:
                    if not idle_logged:
                        log.info("PerpetualWhisperWorker: idle — waiting for transcribe tasks")
                        idle_logged = True
                    time.sleep(2)
                    continue
                idle_logged = False
                task_id, file_id, file_path = task

                # Request Whisper model from orchestrator (swaps from Gemma if needed)
                if self.orchestrator:
                    if not self.orchestrator.request_whisper():
                        # API pending — release task back to pending and wait
                        log.info("PerpetualWhisperWorker: orchestrator refused Whisper (API pending) — releasing task")
                        with _db_write_lock:
                            db.execute("UPDATE tasks SET status='pending', worker_id=NULL WHERE id=?", (task_id,))
                            db.commit()
                        time.sleep(2)
                        continue

                # Kick off prefetch for the next job while we process this one
                threading.Thread(
                    target=self._start_prefetch, args=(file_id,),
                    daemon=True, name="whisper-prefetch-trigger"
                ).start()

                try:
                    self._process(db, task_id, file_id, file_path)
                except (ConnectionError, urllib.error.URLError, OSError) as e:
                    # Whisper server was killed (API interrupt) — re-queue the task
                    log.info("PerpetualWhisperWorker: Whisper interrupted (API preempt) — re-queuing %s" % task_id)
                    with _db_write_lock:
                        db.execute("UPDATE tasks SET status='pending', worker_id=NULL, "
                                   "started_at=NULL WHERE id=?", (task_id,))
                        db.commit()
                    time.sleep(1)
                    continue

                # Tell orchestrator we finished one task — it decides whether to continue batch
                if self.orchestrator:
                    if not self.orchestrator.release_whisper():
                        log.info("PerpetualWhisperWorker: orchestrator swapping back to Gemma")
                        # Wait a moment for Gemma to load before claiming next task
                        time.sleep(2)

            except Exception as e:
                log.warning("PerpetualWhisperWorker: transient error, retrying in 5s: %s" % e)
                time.sleep(5)

        if TRANSCRIBE_HEARTBEAT.exists():
            try:
                TRANSCRIBE_HEARTBEAT.unlink()
            except Exception:
                pass
        db.close()
        log.info("PerpetualWhisperWorker: stopped (%d transcribed, %d errors)" % (
            self.processed, self.errors))

    def start(self):
        self._thread = threading.Thread(
            target=self.run, name="whisper-perpetual", daemon=True)
        self._thread.start()


class TaskCoordinator:
    """Watches the tasks table and marks files 'indexed' when all their tasks complete.
    Also resets tasks stuck in 'assigned' (crashed workers) after a timeout."""

    STUCK_TIMEOUT_MINUTES = 30

    def __init__(self, db_path, indexer_running):
        self.db_path = db_path
        self.indexer_running = indexer_running
        self._thread = None

    def _get_db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def run(self):
        db = self._get_db()
        log.info("TaskCoordinator: started")

        while self.indexer_running():
            try:
                self._finalize_complete_files(db)
                self._reset_stuck_tasks(db)
                self._cleanup_old_uploads(db)
            except Exception as e:
                log.error("TaskCoordinator: error: %s" % e)
            time.sleep(5)

        db.close()
        log.info("TaskCoordinator: stopped")

    def _finalize_complete_files(self, db):
        """Find files where every task is complete/abandoned, then mark them indexed."""
        # Files that have tasks, are not yet indexed, and have no incomplete tasks
        rows = db.execute("""
            SELECT f.id, f.file_type, f.path
            FROM files f
            WHERE f.status NOT IN ('indexed', 'offline')
            AND EXISTS (SELECT 1 FROM tasks t WHERE t.file_id = f.id)
            AND NOT EXISTS (
                SELECT 1 FROM tasks t
                WHERE t.file_id = f.id
                  AND t.status NOT IN ('complete', 'abandoned')
            )
        """).fetchall()

        for file_id, file_type, file_path in rows:
            self._finalize(db, file_id, file_type, file_path)

    def _finalize(self, db, file_id, file_type, file_path):
        """Assemble the final file record and mark it indexed."""
        fname = Path(file_path).name

        # Assemble ai_description from keyframe descriptions if all are ready.
        # Transcript is already written directly to files.transcript by PerpetualWhisperWorker.
        _assemble_file_description(db, file_id, file_path)
        with _db_write_lock:
            db.execute("""
                UPDATE files SET status='indexed', indexed_at=?
                WHERE id=? AND status NOT IN ('indexed', 'offline')
            """, (datetime.now().isoformat(), file_id))
            db.commit()

        log.info("TaskCoordinator: indexed — %s" % fname)

    def _reset_stuck_tasks(self, db):
        """Reset tasks that have been 'assigned' too long (worker likely crashed)."""
        cutoff = (datetime.now() - timedelta(minutes=self.STUCK_TIMEOUT_MINUTES)).isoformat()
        stuck = db.execute("""
            SELECT id, task_type, file_id FROM tasks
            WHERE status = 'assigned' AND started_at < ?
        """, (cutoff,)).fetchall()

        if stuck:
            for task_id, task_type, file_id in stuck:
                log.warning("TaskCoordinator: resetting stuck %s task %s" % (task_type, task_id))
            with _db_write_lock:
                db.execute("""
                    UPDATE tasks
                    SET status='pending', worker_id=NULL, started_at=NULL,
                        retry_count=retry_count+1
                    WHERE status='assigned' AND started_at < ?
                """, (cutoff,))
                db.commit()

    def _cleanup_old_uploads(self, db):
        """Delete uploaded files and their DB records for completed/failed API jobs older than 24h."""
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        old_jobs = db.execute("""
            SELECT id, upload_path FROM api_jobs
            WHERE status IN ('complete', 'failed') AND completed_at < ?
              AND upload_path IS NOT NULL
        """, (cutoff,)).fetchall()
        if not old_jobs:
            return
        for job_id, upload_path in old_jobs:
            # Delete uploaded file
            if upload_path:
                try:
                    os.unlink(upload_path)
                except OSError:
                    pass
            # Delete the virtual file record and its tasks
            with _db_write_lock:
                db.execute("DELETE FROM tasks WHERE api_job_id=?", (job_id,))
                # Find and delete the files record by path
                db.execute("DELETE FROM files WHERE path=?", (upload_path,))
                # Clear the upload_path to mark as cleaned
                db.execute("UPDATE api_jobs SET upload_path=NULL WHERE id=?", (job_id,))
                db.commit()
        log.info("TaskCoordinator: cleaned up %d old API job uploads" % len(old_jobs))

    def start(self):
        self._thread = threading.Thread(target=self.run, name="task-coordinator", daemon=True)
        self._thread.start()


# ---------------------------------------------------------------------------
# SceneWorker — Phase 2 of the Perpetual Task Pipeline
# ---------------------------------------------------------------------------

class _WatchdogFired(Exception):
    """Raised when the ffmpeg CPU-time watchdog kills a stuck process."""
    pass


class SceneWorker:
    """Perpetual scene-detection worker for the Perpetual Task Pipeline.

    Claims 'scene_detect' tasks, runs ffmpeg with VAAPI hardware decode and
    a stuck-frame watchdog, extracts keyframe thumbnails, and emits
    visual_analysis + face_detect tasks for each keyframe.

    Each instance owns exactly one VAAPI device. Three instances run in
    parallel (renderD128/129/130), sharing the same scene_detect queue.
    VAAPI uses the GPU's dedicated decode ASIC — it does not compete with
    Vulkan compute used by Whisper or Gemma.
    """

    WATCHDOG_TIMEOUT = 30  # seconds of no CPU progress before killing ffmpeg
    SCENE_SCAN_CAP = 600   # max seconds to scan for scene cuts; long files (70+ min) stream
                           # the entire file from NAS before ffmpeg finishes, leaving the GPU
                           # idle 90% of the time. Cap at 10 min -- representative scene
                           # structure is captured without reading the full file. (Issue #29)

    def __init__(self, db_path, vaapi_device, indexer_running):
        self.db_path = db_path
        self.vaapi_device = vaapi_device  # e.g. "/dev/dri/renderD128"
        self.indexer_running = indexer_running
        self.worker_id = "scene-%s-%d" % (Path(vaapi_device).name, os.getpid())
        self._thread = None
        self.processed = 0
        self.errors = 0

    def _get_db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _claim_next(self, db):
        """Atomically claim the next pending scene_detect task.
        Returns (task_id, file_id, file_path, duration) or None."""
        now = datetime.now().isoformat()
        with db:
            row = db.execute("""
                SELECT t.id, t.file_id, f.path, f.duration_seconds
                FROM tasks t
                JOIN files f ON t.file_id = f.id
                WHERE t.task_type = 'scene_detect'
                  AND t.status = 'pending'
                ORDER BY (CASE WHEN t.source = 'api' THEN 0 ELSE 1 END), t.created_at ASC
                LIMIT 1
            """).fetchone()
            if row is None:
                return None
            task_id, file_id, file_path, duration = row
            updated = db.execute("""
                UPDATE tasks SET status='assigned', worker_id=?, started_at=?
                WHERE id=? AND status='pending'
            """, (self.worker_id, now, task_id)).rowcount
            if updated == 0:
                return None  # Race: another worker claimed it first
            _update_api_job(db, task_id, 'assigned')
        return task_id, file_id, file_path, duration

    def _run_ffmpeg_watchdog(self, cmd):
        """Run ffmpeg with a stuck-process watchdog.

        Monitors CPU time via /proc/{pid}/stat (utime + stime). If the process
        has not consumed any CPU for WATCHDOG_TIMEOUT seconds, it is considered
        genuinely stuck and killed.

        This replaces the old out_time-based watchdog, which falsely fired on
        healthy files with long sections between scene cuts: out_time in the
        -progress pipe:1 output only advances when the select filter passes a
        frame (i.e. a scene cut is detected), so a 30-second quiet section in
        a sermon video would always trip the old watchdog even though ffmpeg
        was decoding at full speed. (Issue #35)

        Returns stderr text (showinfo scene cut data) on success.
        Raises _WatchdogFired if watchdog fires.
        Raises subprocess.CalledProcessError if ffmpeg exits non-zero.
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        stderr_lines = []
        last_advance = time.monotonic()
        last_cpu_time = None
        watchdog_fired = threading.Event()

        def _get_cpu_time():
            """Return utime+stime jiffies for the ffmpeg process, or None."""
            try:
                with open("/proc/%d/stat" % proc.pid) as f:
                    fields = f.read().split()
                return int(fields[13]) + int(fields[14])  # utime + stime
            except (FileNotFoundError, IndexError, ValueError):
                return None

        def _read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)

        def _read_stdout():
            # Drain stdout so the pipe buffer never fills (ffmpeg blocks if unread).
            for _ in proc.stdout:
                pass

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()

        while proc.poll() is None:
            time.sleep(1)
            cpu_time = _get_cpu_time()
            if cpu_time is not None and cpu_time != last_cpu_time:
                last_advance = time.monotonic()
                last_cpu_time = cpu_time
            if time.monotonic() - last_advance > self.WATCHDOG_TIMEOUT:
                log.warning("SceneWorker [%s]: watchdog fired — process idle %.0fs, killing" % (
                    Path(self.vaapi_device).name, time.monotonic() - last_advance))
                proc.kill()
                watchdog_fired.set()
                break

        stderr_thread.join(timeout=5)
        stdout_thread.join(timeout=5)

        if watchdog_fired.is_set():
            raise _WatchdogFired()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)

        return "".join(stderr_lines)

    def _detect_scenes(self, file_path, duration, fname):
        """Detect scene cuts: VAAPI → CPU → None (signals fixed-interval fallback).
        Returns list of cut timestamps, or None if both attempts stall.
        For files longer than SCENE_SCAN_CAP, only the first SCENE_SCAN_CAP seconds
        are scanned to avoid streaming the full file from NAS. (Issue #29)"""
        threshold = SCENE_CHANGE_THRESHOLD

        # Cap scan duration for long files: streaming a full 70-min ProRes from
        # NAS leaves the decode GPU idle 90% of the time. First 10 min captures
        # representative scene structure.
        scan_duration = min(duration, self.SCENE_SCAN_CAP) if duration else None
        if scan_duration is not None and scan_duration < duration:
            log.info("SceneWorker [%s]: capping scan at %.0fs (file is %.0fs) -- %s" % (
                Path(self.vaapi_device).name, scan_duration, duration, fname))

        # Attempt 1: VAAPI hardware decode (uses decode ASIC, not compute units)
        if os.path.exists(self.vaapi_device):
            try:
                cmd = [
                    FFMPEG, "-progress", "pipe:1",
                    "-hwaccel", "vaapi",
                    "-hwaccel_device", self.vaapi_device,
                    "-i", str(file_path),
                ] + (["-t", str(scan_duration)] if scan_duration else []) + [
                    "-an",
                    "-vf", "select='gt(scene,%.2f)',showinfo" % threshold,
                    "-f", "null", "/dev/null",
                ]
                stderr = self._run_ffmpeg_watchdog(cmd)
                cuts = _parse_scene_cuts(stderr)
                log.info("SceneWorker [%s]: VAAPI — %s: %d cuts (%.0fs)" % (
                    Path(self.vaapi_device).name, fname, len(cuts), duration))
                return cuts
            except _WatchdogFired:
                log.warning("SceneWorker [%s]: VAAPI stalled on %s — retrying with CPU" % (
                    Path(self.vaapi_device).name, fname))
            except subprocess.CalledProcessError:
                log.debug("SceneWorker [%s]: VAAPI failed for %s — trying CPU" % (
                    Path(self.vaapi_device).name, fname))

        # Attempt 2: CPU decode fallback
        try:
            cmd = [
                FFMPEG, "-progress", "pipe:1",
                "-i", str(file_path),
            ] + (["-t", str(scan_duration)] if scan_duration else []) + [
                "-an",
                "-vf", "select='gt(scene,%.2f)',showinfo" % threshold,
                "-f", "null", "/dev/null",
            ]
            stderr = self._run_ffmpeg_watchdog(cmd)
            cuts = _parse_scene_cuts(stderr)
            log.info("SceneWorker [%s]: CPU — %s: %d cuts (%.0fs)" % (
                Path(self.vaapi_device).name, fname, len(cuts), duration))
            return cuts
        except _WatchdogFired:
            log.warning("SceneWorker [%s]: CPU also stalled on %s — using fixed-interval" % (
                Path(self.vaapi_device).name, fname))
        except subprocess.CalledProcessError as e:
            log.warning("SceneWorker [%s]: CPU ffmpeg failed for %s: %s" % (
                Path(self.vaapi_device).name, fname, e))

        # Signal caller to use fixed-interval
        return None

    def _fixed_interval_timestamps(self, duration):
        """Keyframe timestamps when scene detection fails (watchdog fired twice)."""
        if duration < 5:
            return [duration * 0.5]
        elif duration <= 1800:  # up to 30 min: 3 keyframes
            return [duration * 0.3, duration * 0.6, duration * 0.9]
        else:
            # 1 frame per max(60s, duration/15); a 67-min file gets ~15 keyframes
            interval = max(60.0, duration / 15.0)
            timestamps = []
            t = interval * 0.5
            while t < duration:
                timestamps.append(t)
                t += interval
            return timestamps if timestamps else [duration * 0.5]

    def _process(self, db, task_id, file_id, file_path, duration):
        """Process one scene_detect task end-to-end."""
        fname = Path(file_path).name
        ext = Path(file_path).suffix.lower()
        is_braw = (ext == '.braw')
        is_r3d = (ext == '.r3d')
        log.info("SceneWorker [%s]: starting — %s [task %s]" % (
            Path(self.vaapi_device).name, fname, task_id))

        if not Path(file_path).exists():
            log.warning("SceneWorker: file gone — %s" % fname)
            self._mark(db, task_id, "failed", "file not found on NAS")
            return

        # R3D: no duration metadata available via ffprobe — use probe-based extraction
        if is_r3d:
            self._process_r3d(db, task_id, file_id, file_path, fname)
            return

        # Ensure we have a duration
        braw_fps = 24.0  # default; updated below for BRAW files
        if not duration or duration <= 0:
            if is_braw:
                braw_data = _braw_info(file_path)
                if braw_data:
                    _, braw_fps, duration = braw_data
                    with _db_write_lock:
                        db.execute("UPDATE files SET duration_seconds=? WHERE id=?",
                                   (duration, file_id))
                        db.commit()
                else:
                    log.warning("SceneWorker: can't read BRAW clip info, abandoning — %s" % fname)
                    self._mark(db, task_id, "abandoned", "cannot read BRAW clip info")
                    return
            else:
                duration = _quick_duration(file_path)
                if duration > 0:
                    with _db_write_lock:
                        db.execute("UPDATE files SET duration_seconds=? WHERE id=?",
                                   (duration, file_id))
                        db.commit()
                else:
                    # Permanent failure — format unsupported by ffprobe.
                    # Mark abandoned (not failed) so TaskCoordinator can finalize the file.
                    log.warning("SceneWorker: can't determine duration, abandoning — %s" % fname)
                    self._mark(db, task_id, "abandoned", "cannot determine duration")
                    return
        elif is_braw:
            # Duration came from DB (ffprobe read the MOV container); still need fps
            braw_data = _braw_info(file_path)
            if braw_data:
                _, braw_fps, _ = braw_data

        t_start = time.monotonic()

        # BRAW: ffmpeg can't decode the brst video codec — skip scene detection,
        # go straight to fixed-interval and use braw-frame for thumbnail extraction
        if is_braw:
            cuts = None
        else:
            cuts = self._detect_scenes(file_path, duration, fname)

        # Non-BRAW stall: both VAAPI and CPU watchdogs fired.
        # Abandon (not fail) so TaskCoordinator still finalizes the file,
        # but don't emit keyframes — the file is flagged for later retry.
        if cuts is None and not is_braw:
            log.warning("SceneWorker [%s]: abandoning %s — scene detect stalled (Issue #31)" % (
                Path(self.vaapi_device).name, fname))
            self._mark(db, task_id, "abandoned", "scene_detect_stalled")
            return

        # BRAW always uses fixed-interval (ffmpeg can't decode brst codec)
        use_fixed = cuts is None  # only True for BRAW at this point
        if use_fixed:
            timestamps = self._fixed_interval_timestamps(duration)
            log.info("SceneWorker [%s]: BRAW fixed-interval — %s: %d keyframes" % (
                Path(self.vaapi_device).name, fname, len(timestamps)))
        else:
            timestamps = select_keyframe_timestamps(cuts, duration)

        # Extract keyframe thumbnails (format-specific for BRAW)
        thumb_subdir = THUMB_DIR / file_id
        thumb_subdir.mkdir(parents=True, exist_ok=True)

        keyframes = []
        for i, ts in enumerate(timestamps):
            thumb_path = thumb_subdir / ("frame_%02d.jpg" % i)
            if is_braw:
                frame_idx = max(0, int(ts * braw_fps))
                success = _braw_extract_frame_jpeg(file_path, frame_idx, str(thumb_path))
            else:
                success = extract_thumbnail(file_path, ts, thumb_path)
            if success:
                keyframes.append((ts, str(thumb_path)))

        if not keyframes:
            log.warning("SceneWorker: no keyframes extracted for %s" % fname)
            self._mark(db, task_id, "complete")
            return

        # Write keyframes and create downstream tasks in one transaction,
        # then mark scene_detect complete — all atomic so TaskCoordinator
        # never sees a window where scene_detect is done but visual_analysis
        # tasks haven't been created yet.
        now = datetime.now().isoformat()
        with _db_write_lock:
            for i, (ts, thumb_path) in enumerate(keyframes):
                keyframe_id = "%s_kf%02d" % (file_id, i)
                db.execute("""
                    INSERT OR REPLACE INTO keyframes
                        (id, file_id, timestamp_seconds, thumbnail_path)
                    VALUES (?, ?, ?, ?)
                """, (keyframe_id, file_id, ts, thumb_path))
                if "visual_analysis" in ENABLED_TASK_TYPES:
                    _create_keyframe_task(db, keyframe_id, file_id, "visual_analysis")
                if "face_detect" in ENABLED_TASK_TYPES:
                    _create_keyframe_task(db, keyframe_id, file_id, "face_detect")
            db.execute("""
                UPDATE tasks SET status='complete', completed_at=?
                WHERE id=?
            """, (now, task_id))
            db.commit()

        elapsed = time.monotonic() - t_start
        log.info("SceneWorker [%s]: done — %s: %d keyframes in %.1fs%s" % (
            Path(self.vaapi_device).name, fname, len(keyframes), elapsed,
            " (fixed-interval)" if use_fixed else ""))
        self.processed += 1

    def _process_r3d(self, db, task_id, file_id, file_path, fname):
        """Process an R3D file: probe frame-by-frame since ffprobe can't read R3D metadata.
        Tries each index in R3D_PROBE_FRAMES; stops on first failure (past end of clip)."""
        t_start = time.monotonic()
        thumb_subdir = THUMB_DIR / file_id
        thumb_subdir.mkdir(parents=True, exist_ok=True)

        keyframes = []
        for frame_num in R3D_PROBE_FRAMES:
            thumb_path = thumb_subdir / ("frame_%02d.jpg" % len(keyframes))
            if _r3d_extract_frame_jpeg(file_path, frame_num, str(thumb_path)):
                ts = frame_num / 24.0  # approximate timestamp (assumes ~24fps)
                keyframes.append((ts, str(thumb_path)))
            else:
                break  # Past end of clip or extraction error — stop probing

        if not keyframes:
            log.warning("SceneWorker [%s]: R3D no frames extracted — %s" % (
                Path(self.vaapi_device).name, fname))
            self._mark(db, task_id, "complete")
            return

        # Store approximate duration based on last successfully extracted frame
        estimated_duration = keyframes[-1][0] + 2.0
        with _db_write_lock:
            db.execute("UPDATE files SET duration_seconds=? WHERE id=?",
                       (estimated_duration, file_id))
            db.commit()

        now = datetime.now().isoformat()
        with _db_write_lock:
            for i, (ts, thumb_path) in enumerate(keyframes):
                keyframe_id = "%s_kf%02d" % (file_id, i)
                db.execute("""
                    INSERT OR REPLACE INTO keyframes
                        (id, file_id, timestamp_seconds, thumbnail_path)
                    VALUES (?, ?, ?, ?)
                """, (keyframe_id, file_id, ts, thumb_path))
                if "visual_analysis" in ENABLED_TASK_TYPES:
                    _create_keyframe_task(db, keyframe_id, file_id, "visual_analysis")
                if "face_detect" in ENABLED_TASK_TYPES:
                    _create_keyframe_task(db, keyframe_id, file_id, "face_detect")
            db.execute("""
                UPDATE tasks SET status='complete', completed_at=?
                WHERE id=?
            """, (now, task_id))
            db.commit()

        elapsed = time.monotonic() - t_start
        log.info("SceneWorker [%s]: R3D done — %s: %d keyframes in %.1fs" % (
            Path(self.vaapi_device).name, fname, len(keyframes), elapsed))
        self.processed += 1

    def _mark(self, db, task_id, status, error=None):
        with _db_write_lock:
            db.execute("""
                UPDATE tasks SET status=?, completed_at=?, error_message=?
                WHERE id=?
            """, (status, datetime.now().isoformat(), error, task_id))
            db.commit()
            _update_api_job(db, task_id, status, error=error)

    def run(self):
        db = self._get_db()
        dev_name = Path(self.vaapi_device).name
        log.info("SceneWorker [%s]: started" % dev_name)
        idle_logged = False

        while self.indexer_running():
            try:
                task = self._claim_next(db)
                if task is None:
                    if not idle_logged:
                        log.info("SceneWorker [%s]: idle — waiting for scene_detect tasks" % dev_name)
                        idle_logged = True
                    time.sleep(2)
                    continue
                idle_logged = False
                task_id, file_id, file_path, duration = task
                try:
                    self._process(db, task_id, file_id, file_path, duration)
                except Exception as e:
                    log.error("SceneWorker [%s]: unexpected error on %s: %s" % (
                        dev_name, Path(file_path).name, e))
                    try:
                        self._mark(db, task_id, "failed", str(e)[:500])
                    except Exception:
                        pass
                    self.errors += 1
            except Exception as e:
                log.warning("SceneWorker [%s]: transient error, retrying in 5s: %s" % (dev_name, e))
                time.sleep(5)

        db.close()
        log.info("SceneWorker [%s]: stopped (%d processed, %d errors)" % (
            dev_name, self.processed, self.errors))

    def start(self):
        dev_name = Path(self.vaapi_device).name
        self._thread = threading.Thread(
            target=self.run, name="scene-%s" % dev_name, daemon=True)
        self._thread.start()


# ---------------------------------------------------------------------------
# GemmaWorker — Phase 3 of the Perpetual Task Pipeline
# ---------------------------------------------------------------------------

class GemmaWorker:
    """Perpetual visual-analysis worker for the Perpetual Task Pipeline.

    Claims crawler-only 'visual_analysis' tasks, calls describe_image() against
    one Gemma 3 12B server, writes ai_description to the keyframe (or file for
    image files), and assembles the file-level description once all keyframes
    are done.

    Two instances run in parallel — one per RX 580 / LLM server port.
    API visual_analysis tasks are handled by Pro580XGemmaWorker instead.
    """

    def __init__(self, db_path, llm_server, running_fn):
        self.db_path = db_path
        self.llm_server = llm_server          # e.g. "http://localhost:8090"
        self.indexer_running = running_fn
        port = llm_server.split(":")[-1]
        self.worker_id = "gemma-%s" % port
        self.processed = 0
        self.errors = 0
        self._thread = None

    def _get_db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _claim_task(self, db):
        """Atomically claim the next pending crawler visual_analysis task.
        API tasks are handled by Pro580XGemmaWorker.
        Returns (task_id, file_id) or None."""
        now = datetime.now().isoformat()
        with _db_write_lock:
            row = db.execute("""
                SELECT t.id, t.file_id
                FROM tasks t
                WHERE t.task_type = 'visual_analysis'
                  AND t.status = 'pending'
                  AND t.source = 'crawler'
                ORDER BY t.created_at ASC
                LIMIT 1
            """).fetchone()
            if row is None:
                return None
            task_id, file_id = row
            updated = db.execute("""
                UPDATE tasks SET status='assigned', worker_id=?, started_at=?
                WHERE id=? AND status='pending'
            """, (self.worker_id, now, task_id)).rowcount
            if updated == 0:
                return None  # Race: another worker claimed it first
            db.commit()
        return task_id, file_id

    def _mark(self, db, task_id, status, error=None):
        with _db_write_lock:
            db.execute("""
                UPDATE tasks SET status=?, completed_at=?, error_message=?
                WHERE id=?
            """, (status, datetime.now().isoformat(), error, task_id))
            db.commit()

    def _process(self, db, task_id, file_id, port):
        """Process one visual_analysis task."""
        # task_id format: "{keyframe_id}_visual_analysis"
        suffix = "_visual_analysis"
        keyframe_id = task_id[:-len(suffix)]

        # Look up keyframe
        kf_row = db.execute(
            "SELECT thumbnail_path FROM keyframes WHERE id=?", (keyframe_id,)
        ).fetchone()

        file_row = db.execute(
            "SELECT path FROM files WHERE id=?", (file_id,)
        ).fetchone()
        file_path = file_row[0] if file_row else ""

        if kf_row is not None:
            # Video keyframe task
            thumb_path = kf_row[0]
            description = describe_image(thumb_path, context=file_path,
                                         llm_server=self.llm_server)
            if description is None:
                self._mark(db, task_id, "failed", "describe_image returned None")
                return
            with _db_write_lock:
                db.execute("UPDATE keyframes SET ai_description=? WHERE id=?",
                           (description, keyframe_id))
                db.execute("""
                    UPDATE tasks SET status='complete', completed_at=?
                    WHERE id=?
                """, (datetime.now().isoformat(), task_id))
                db.commit()
            log.info("GemmaWorker [%s]: described — %s" % (port, Path(file_path).name))
            _assemble_file_description(db, file_id, file_path)
        else:
            # Image file task — no keyframes, describe the file directly
            description = describe_image(file_path, context=file_path,
                                         llm_server=self.llm_server)
            if description is None:
                self._mark(db, task_id, "failed", "describe_image returned None")
                return
            path_parts = [p for p in Path(file_path).parts
                          if p not in ("/", "mnt", "vault", "Volumes", "Vault")]
            tags = ", ".join(path_parts[:-1])
            with _db_write_lock:
                db.execute("UPDATE files SET ai_description=?, tags=? WHERE id=?",
                           (description, tags, file_id))
                db.execute("""
                    UPDATE tasks SET status='complete', completed_at=?
                    WHERE id=?
                """, (datetime.now().isoformat(), task_id))
                db.commit()
            log.info("GemmaWorker [%s]: described image — %s" % (port, Path(file_path).name))

        self.processed += 1

    def run(self):
        db = self._get_db()
        port = self.llm_server.split(":")[-1]
        log.info("GemmaWorker [%s]: started" % port)
        idle_logged = False

        while self.indexer_running():
            try:
                result = self._claim_task(db)
                if result is None:
                    if not idle_logged:
                        log.info("GemmaWorker [%s]: idle — waiting for visual_analysis tasks" % port)
                        idle_logged = True
                    time.sleep(2)
                    continue
                idle_logged = False
                task_id, file_id = result
                try:
                    self._process(db, task_id, file_id, port)
                except Exception as e:
                    log.error("GemmaWorker [%s]: error on task %s: %s" % (port, task_id, e))
                    try:
                        self._mark(db, task_id, "failed", str(e)[:500])
                    except Exception:
                        pass
                    self.errors += 1
            except Exception as e:
                log.error("GemmaWorker [%s]: unexpected error: %s" % (port, e))
                time.sleep(5)

        db.close()
        log.info("GemmaWorker [%s]: stopped (%d processed, %d errors)" % (
            port, self.processed, self.errors))

    def start(self):
        port = self.llm_server.split(":")[-1]
        self._thread = threading.Thread(
            target=self.run, name="gemma-worker-%s" % port, daemon=True)
        self._thread.start()


# ---------------------------------------------------------------------------
# Pro 580X Orchestrator — model-swapping between Gemma and Whisper
# ---------------------------------------------------------------------------

class Pro580XOrchestrator:
    """Manages the Pro 580X GPU by swapping between Gemma (for API visual
    analysis) and Whisper (for transcription).  Only one model can be loaded
    at a time due to 8 GB VRAM limit.

    Default state: Gemma loaded (instant API responses).
    When transcription work is pending, swaps to Whisper, processes a batch,
    then swaps back to Gemma.  An incoming API request can interrupt Whisper
    immediately — the in-progress task is re-queued.

    Thread-safe: accessed by PerpetualWhisperWorker, Pro580XGemmaWorker, and
    the HTTP handler (via state file IPC for the serve process).
    """

    # Paths to server binaries
    LLAMA_SERVER  = "/home/mediaadmin/llama.cpp/build/bin/llama-server"
    WHISPER_SERVER_BIN = "/home/mediaadmin/whisper.cpp/build/bin/whisper-server"
    MODEL_DIR     = "/home/mediaadmin/models"

    def __init__(self, db_path):
        self.db_path = db_path
        self.state = "idle"             # idle | gemma_loading | gemma_ready | whisper_loading | whisper_busy
        self._current_model = None      # "gemma" | "whisper" | None
        self._process = None            # subprocess.Popen handle
        self._condition = threading.Condition()
        self._api_pending = False       # True when an API call is waiting for Gemma
        self._batch_count = 0           # transcriptions processed in current Whisper batch
        self._model_loaded_at = None    # ISO timestamp
        self._vulkan_device = "0"       # discovered at start()
        self._shutdown = False

    # -- Public API ----------------------------------------------------------

    def start(self):
        """Discover Vulkan device, clean up orphans, load Gemma."""
        self._discover_vulkan_device()
        self._cleanup_orphans()
        with self._condition:
            self._swap_to_gemma()
        log.info("Pro580XOrchestrator: started (Gemma loaded, Vulkan device %s)" % self._vulkan_device)

    def request_gemma(self, timeout=120):
        """Called by Pro580XGemmaWorker / API handler.
        Returns the Gemma server URL when ready, or None on timeout."""
        with self._condition:
            if self.state == "gemma_ready":
                return PRO580X_GEMMA

            # Whisper is running — signal interrupt
            self._api_pending = True
            if self.state == "whisper_busy" and self._process:
                log.info("Pro580XOrchestrator: API request — interrupting Whisper")
                self._stop_process()
                # The WhisperWorker will catch the connection error and re-queue

            # Wait for Gemma to become ready
            deadline = time.time() + timeout
            while self.state != "gemma_ready" and not self._shutdown:
                remaining = deadline - time.time()
                if remaining <= 0:
                    log.warning("Pro580XOrchestrator: request_gemma timed out after %ds" % timeout)
                    return None
                self._condition.wait(timeout=remaining)

            if self._shutdown:
                return None
            self._api_pending = False
            return PRO580X_GEMMA

    def request_whisper(self):
        """Called by PerpetualWhisperWorker before transcribing.
        Returns True if Whisper is ready, False if refused (API pending)."""
        with self._condition:
            if self._api_pending or self._shutdown:
                return False
            if self.state == "whisper_busy":
                return True  # already in Whisper mode (batching)

            # Swap from Gemma to Whisper
            if self.state == "gemma_ready":
                log.info("Pro580XOrchestrator: swapping to Whisper for transcription")
                self._stop_process()

            if self._api_pending:
                # API arrived during Gemma shutdown — abort swap
                self._swap_to_gemma()
                return False

            self.state = "whisper_loading"
            self._write_state()
            self._condition.notify_all()

            ok = self._start_whisper_server()
            if not ok:
                log.error("Pro580XOrchestrator: Whisper failed to start — swapping back to Gemma")
                self._swap_to_gemma()
                return False

            self.state = "whisper_busy"
            self._current_model = "whisper"
            self._model_loaded_at = datetime.now().isoformat()
            self._batch_count = 0
            self._write_state()
            self._condition.notify_all()
            return True

    def release_whisper(self):
        """Called after each transcription completes.
        Returns True to keep transcribing, False to stop (swap back to Gemma)."""
        with self._condition:
            if self.state != "whisper_busy":
                return False  # Not in Whisper mode — nothing to release
            self._batch_count += 1
            should_swap_back = (
                self._api_pending
                or self._batch_count >= WHISPER_BATCH_MAX_TASKS
                or self._shutdown
                or not self._has_pending_transcriptions()
            )
            if should_swap_back:
                log.info("Pro580XOrchestrator: Whisper batch done (%d tasks) — swapping to Gemma"
                         % self._batch_count)
                self._stop_process()
                self._swap_to_gemma()
                return False
            self._write_state()
            return True

    def get_status(self):
        """Return status dict and write state file for IPC."""
        with self._condition:
            status = {
                "state": self.state,
                "current_model": self._current_model,
                "model_loaded_at": self._model_loaded_at,
                "api_pending": self._api_pending,
                "whisper_batch_count": self._batch_count,
                "pending_transcribe": self._count_pending("transcribe"),
                "pending_api_visual": self._count_pending_api_visual(),
            }
            self._write_state(status)
            return status

    def shutdown(self):
        """Kill whichever process is running."""
        with self._condition:
            self._shutdown = True
            self._stop_process()
            self.state = "idle"
            self._current_model = None
            self._condition.notify_all()
        log.info("Pro580XOrchestrator: shutdown complete")

    # -- Private helpers -----------------------------------------------------

    def _swap_to_gemma(self):
        """Load Gemma on the Pro 580X.  Must be called with self._condition held."""
        self.state = "gemma_loading"
        self._write_state()
        self._condition.notify_all()

        ok = self._start_gemma_server()
        if ok:
            self.state = "gemma_ready"
            self._current_model = "gemma"
            self._model_loaded_at = datetime.now().isoformat()
            self._api_pending = False
            self._batch_count = 0
        else:
            log.error("Pro580XOrchestrator: Gemma failed to start — entering idle state")
            self.state = "idle"
            self._current_model = None
            # Insert a notification so the VaultSearch app knows
            self._insert_notification("Pro 580X: Gemma failed to start", "error")

        self._write_state()
        self._condition.notify_all()

    def _start_gemma_server(self):
        """Start llama-server for Gemma on the Pro 580X.  Returns True on success."""
        env = os.environ.copy()
        env["GGML_VK_VISIBLE_DEVICES"] = self._vulkan_device
        cmd = [
            self.LLAMA_SERVER,
            "--host", "127.0.0.1",
            "--port", str(PRO580X_GEMMA_PORT),
            "-m", os.path.join(self.MODEL_DIR, "gemma-3-12b-it-Q3_K_S.gguf"),
            "--mmproj", os.path.join(self.MODEL_DIR, "mmproj-gemma-3-12b-it-f16.gguf"),
            "--device", "Vulkan0",
            "-ngl", "99",
            "-c", "1024",
            "--parallel", "1",
        ]
        try:
            self._process = subprocess.Popen(
                cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("Pro580XOrchestrator: starting Gemma (pid %d, port %d)"
                     % (self._process.pid, PRO580X_GEMMA_PORT))
        except Exception as e:
            log.error("Pro580XOrchestrator: failed to spawn llama-server: %s" % e)
            return False
        return self._poll_health(PRO580X_GEMMA_PORT)

    def _start_whisper_server(self):
        """Start whisper-server on the Pro 580X.  Returns True on success."""
        cmd = [
            self.WHISPER_SERVER_BIN,
            "--host", "127.0.0.1",
            "--port", str(PRO580X_WHISPER_PORT),
            "-m", os.path.join(self.MODEL_DIR, "ggml-large-v3-turbo.bin"),
            "--device", "0",
        ]
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("Pro580XOrchestrator: starting Whisper (pid %d, port %d)"
                     % (self._process.pid, PRO580X_WHISPER_PORT))
        except Exception as e:
            log.error("Pro580XOrchestrator: failed to spawn whisper-server: %s" % e)
            return False
        return self._poll_health(PRO580X_WHISPER_PORT)

    def _stop_process(self):
        """Stop the currently running server process.  Safe to call if nothing is running."""
        if self._process is None:
            return
        pid = self._process.pid
        try:
            self._process.terminate()  # SIGTERM
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("Pro580XOrchestrator: SIGTERM timeout — sending SIGKILL (pid %d)" % pid)
                self._process.kill()
                self._process.wait(timeout=5)
        except Exception as e:
            log.error("Pro580XOrchestrator: error stopping process %d: %s" % (pid, e))
        self._process = None

    def _poll_health(self, port, timeout=None):
        """Poll /health until status=ok or timeout.  Returns True on success."""
        if timeout is None:
            timeout = MODEL_LOAD_TIMEOUT
        url = "http://localhost:%d/health" % port
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._shutdown:
                return False
            try:
                req = urllib.request.Request(url)
                resp = urllib.request.urlopen(req, timeout=5)
                data = json.loads(resp.read())
                if data.get("status") == "ok":
                    log.info("Pro580XOrchestrator: health ok on port %d" % port)
                    return True
            except Exception:
                pass
            time.sleep(1)
        log.error("Pro580XOrchestrator: health check timed out on port %d after %ds" % (port, timeout))
        # Kill the process that failed to start
        self._stop_process()
        return False

    def _discover_vulkan_device(self):
        """Determine which Vulkan device index the Pro 580X maps to.
        The RX 580s use GGML_VK_VISIBLE_DEVICES 1 and 2, so Pro 580X is
        most likely 0, but we verify via llama-server --list-devices."""
        try:
            result = subprocess.run(
                [self.LLAMA_SERVER, "--list-devices"],
                capture_output=True, text=True, timeout=10)
            output = result.stdout + result.stderr
            # Look for the Pro 580X — it's PCIe 07:00.0 / card0
            # Default to "0" if we can't parse
            for line in output.splitlines():
                lower = line.lower()
                if "pro 580" in lower or "polaris" in lower:
                    # Try to extract the device index from e.g. "Vulkan0: ..."
                    for part in line.split():
                        if part.startswith("Vulkan"):
                            idx = part.replace("Vulkan", "").rstrip(":")
                            if idx.isdigit():
                                self._vulkan_device = idx
                                log.info("Pro580XOrchestrator: discovered Pro 580X as Vulkan%s" % idx)
                                return
            log.info("Pro580XOrchestrator: could not identify Pro 580X in device list, defaulting to Vulkan0")
            self._vulkan_device = "0"
        except Exception as e:
            log.warning("Pro580XOrchestrator: --list-devices failed (%s), defaulting to Vulkan0" % e)
            self._vulkan_device = "0"

    def _cleanup_orphans(self):
        """Kill any stale llama-server or whisper-server on our ports from a previous crash."""
        import socket
        for port in (PRO580X_GEMMA_PORT, PRO580X_WHISPER_PORT):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", port))
                sock.close()
                # Port is in use — find and kill the process
                log.warning("Pro580XOrchestrator: port %d in use — killing orphan process" % port)
                try:
                    result = subprocess.run(
                        ["fuser", "-k", "%d/tcp" % port],
                        capture_output=True, timeout=5)
                except Exception:
                    pass
                time.sleep(1)
            except (ConnectionRefusedError, OSError):
                pass  # Port is free
            finally:
                sock.close()

    def _write_state(self, status=None):
        """Write orchestrator state to JSON file for IPC with the serve process."""
        if status is None:
            status = {
                "state": self.state,
                "current_model": self._current_model,
                "model_loaded_at": self._model_loaded_at,
                "api_pending": self._api_pending,
                "whisper_batch_count": self._batch_count,
            }
        try:
            tmp = str(PRO580X_STATE_FILE) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(status, f)
            os.replace(tmp, str(PRO580X_STATE_FILE))
        except Exception as e:
            log.warning("Pro580XOrchestrator: failed to write state file: %s" % e)

    def _has_pending_transcriptions(self):
        """Check if there are pending transcribe tasks in the DB."""
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            row = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_type='transcribe' AND status='pending'"
            ).fetchone()
            db.close()
            return row[0] > 0
        except Exception:
            return False

    def _count_pending(self, task_type):
        """Count pending tasks of a given type."""
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            row = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_type=? AND status='pending'",
                (task_type,)
            ).fetchone()
            db.close()
            return row[0]
        except Exception:
            return 0

    def _count_pending_api_visual(self):
        """Count pending API visual_analysis tasks."""
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            row = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_type='visual_analysis' "
                "AND status='pending' AND source='api'"
            ).fetchone()
            db.close()
            return row[0]
        except Exception:
            return 0

    def _insert_notification(self, title, severity="warning"):
        """Insert a notification into the DB for the VaultSearch app."""
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            db.execute(
                "INSERT INTO notifications (id, severity, title, created_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), severity, title, datetime.now().isoformat())
            )
            db.commit()
            db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Pro580XGemmaWorker — API-only visual analysis on the Pro 580X
# ---------------------------------------------------------------------------

class Pro580XGemmaWorker:
    """Dedicated worker that processes API visual_analysis tasks on the Pro 580X.
    Only claims tasks with source='api'.  Requests Gemma from the orchestrator
    before processing (will wait if Whisper is currently loaded)."""

    def __init__(self, db_path, orchestrator, running_fn):
        self.db_path = db_path
        self.orchestrator = orchestrator
        self.indexer_running = running_fn
        self.worker_id = "gemma-pro580x"
        self.processed = 0
        self.errors = 0
        self._thread = None

    def _get_db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _claim_task(self, db):
        """Claim the next pending API visual_analysis task."""
        now = datetime.now().isoformat()
        with _db_write_lock:
            row = db.execute("""
                SELECT t.id, t.file_id
                FROM tasks t
                WHERE t.task_type = 'visual_analysis'
                  AND t.status = 'pending'
                  AND t.source = 'api'
                ORDER BY t.created_at ASC
                LIMIT 1
            """).fetchone()
            if row is None:
                return None
            task_id, file_id = row
            updated = db.execute("""
                UPDATE tasks SET status='assigned', worker_id=?, started_at=?
                WHERE id=? AND status='pending'
            """, (self.worker_id, now, task_id)).rowcount
            if updated == 0:
                return None
            _update_api_job(db, task_id, 'assigned')
            db.commit()
        return task_id, file_id

    def _claim_text_chat(self, db):
        """Claim the next pending API text_chat task.
        Returns (task_id, api_job_id) or None."""
        now = datetime.now().isoformat()
        with _db_write_lock:
            row = db.execute("""
                SELECT t.id, t.api_job_id
                FROM tasks t
                WHERE t.task_type = 'text_chat'
                  AND t.status = 'pending'
                  AND t.source = 'api'
                ORDER BY t.created_at ASC
                LIMIT 1
            """).fetchone()
            if row is None:
                return None
            task_id, api_job_id = row
            updated = db.execute("""
                UPDATE tasks SET status='assigned', worker_id=?, started_at=?
                WHERE id=? AND status='pending'
            """, (self.worker_id, now, task_id)).rowcount
            if updated == 0:
                return None
            _update_api_job(db, task_id, 'assigned')
            db.commit()
        return task_id, api_job_id

    def _mark(self, db, task_id, status, error=None):
        with _db_write_lock:
            db.execute("""
                UPDATE tasks SET status=?, completed_at=?, error_message=?
                WHERE id=?
            """, (status, datetime.now().isoformat(), error, task_id))
            db.commit()
            _update_api_job(db, task_id, status, error=error)

    def run(self):
        db = self._get_db()
        while self.indexer_running():
            # Try API visual_analysis first, then text_chat
            result = self._claim_task(db)
            is_text_chat = False
            if result is None:
                result = self._claim_text_chat(db)
                if result is None:
                    time.sleep(2)
                    continue
                is_text_chat = True

            # Ensure Gemma is loaded on the Pro 580X
            gemma_url = self.orchestrator.request_gemma(timeout=120)
            if gemma_url is None:
                task_id = result[0]
                log.warning("Pro580XGemmaWorker: could not get Gemma — releasing task %s" % task_id)
                with _db_write_lock:
                    db.execute("UPDATE tasks SET status='pending', worker_id=NULL WHERE id=?", (task_id,))
                    db.commit()
                time.sleep(5)
                continue

            if is_text_chat:
                # --- text_chat processing ---
                task_id, api_job_id = result
                try:
                    # Read prompt and params from api_jobs
                    job_row = db.execute(
                        "SELECT prompt, max_tokens, temperature FROM api_jobs WHERE id=?",
                        (api_job_id,)
                    ).fetchone()
                    if not job_row or not job_row[0]:
                        self._mark(db, task_id, "failed", "No prompt found in api_jobs")
                        self.errors += 1
                        continue
                    prompt, max_tokens, temperature = job_row
                    response = send_text_prompt(
                        prompt, llm_server=gemma_url,
                        max_tokens=max_tokens or 200,
                        temperature=temperature or 0.3
                    )
                    if response is None:
                        self._mark(db, task_id, "failed", "send_text_prompt returned None")
                        self.errors += 1
                        continue
                    # Store result directly in api_jobs.result
                    result_json = json.dumps({
                        "response": response,
                        "model": "gemma-3-12b",
                        "max_tokens": max_tokens or 200,
                        "temperature": temperature or 0.3,
                    })
                    with _db_write_lock:
                        db.execute("UPDATE api_jobs SET result=? WHERE id=?",
                                   (result_json, api_job_id))
                        db.commit()
                    self._mark(db, task_id, "complete")
                    self.processed += 1
                    log.info("Pro580XGemmaWorker: text_chat complete — %s" % task_id[:20])
                except Exception as e:
                    log.error("Pro580XGemmaWorker: text_chat error on %s: %s" % (task_id, e))
                    self._mark(db, task_id, "failed", str(e)[:500])
                    self.errors += 1
                    time.sleep(2)
            else:
                # --- visual_analysis processing ---
                task_id, file_id = result
                try:
                    suffix = "_visual_analysis"
                    keyframe_id = task_id[:-len(suffix)]

                    kf_row = db.execute(
                        "SELECT thumbnail_path FROM keyframes WHERE id=?", (keyframe_id,)
                    ).fetchone()
                    file_row = db.execute(
                        "SELECT path FROM files WHERE id=?", (file_id,)
                    ).fetchone()
                    file_path = file_row[0] if file_row else ""

                    if kf_row is not None:
                        # Video keyframe task
                        thumb_path = kf_row[0]
                        description = describe_image(thumb_path, context=file_path,
                                                     llm_server=gemma_url)
                        if description is None:
                            self._mark(db, task_id, "failed", "describe_image returned None")
                            self.errors += 1
                            continue
                        with _db_write_lock:
                            db.execute("UPDATE keyframes SET ai_description=? WHERE id=?",
                                       (description, keyframe_id))
                            db.commit()
                        _assemble_file_description(db, file_id, file_path)
                        self._mark(db, task_id, "complete")
                        self.processed += 1
                    else:
                        # Image file — no keyframe
                        description = describe_image(file_path, context=file_path,
                                                     llm_server=gemma_url)
                        if description is None:
                            self._mark(db, task_id, "failed", "describe_image returned None")
                            self.errors += 1
                            continue
                        path_parts = [p for p in Path(file_path).parts
                                      if p not in ("/", "mnt", "vault", "Volumes", "Vault")]
                        tags = ", ".join(path_parts[:-1])
                        with _db_write_lock:
                            db.execute("UPDATE files SET ai_description=?, tags=? WHERE id=?",
                                       (description, tags, file_id))
                            db.commit()
                        self._mark(db, task_id, "complete")
                        self.processed += 1

                except Exception as e:
                    log.error("Pro580XGemmaWorker: error on %s: %s" % (task_id, e))
                    self._mark(db, task_id, "failed", str(e)[:500])
                    self.errors += 1
                    time.sleep(2)

        db.close()
        log.info("Pro580XGemmaWorker: stopped (%d processed, %d errors)" % (
            self.processed, self.errors))

    def start(self):
        self._thread = threading.Thread(
            target=self.run, name="gemma-worker-pro580x", daemon=True)
        self._thread.start()


# ---------------------------------------------------------------------------
# FaceWorker (Phase 4 — face detection)
# ---------------------------------------------------------------------------

class FaceWorker:
    """Perpetual face-detection worker for the Perpetual Task Pipeline.

    Claims 'face_detect' tasks, runs dlib-based face detection on each keyframe
    thumbnail, and stores face embeddings + crop thumbnails in the database.
    Single CPU-only instance — no GPU required.
    """

    def __init__(self, db_path, running_fn):
        self.db_path = db_path
        self.indexer_running = running_fn
        self.worker_id = "face-worker"
        self.processed = 0
        self.errors = 0
        self._thread = None

    def _get_db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _claim_task(self, db):
        now = datetime.now().isoformat()
        with _db_write_lock:
            row = db.execute("""
                SELECT t.id, t.file_id
                FROM tasks t
                WHERE t.task_type = 'face_detect'
                  AND t.status = 'pending'
                ORDER BY (CASE WHEN t.source = 'api' THEN 0 ELSE 1 END), t.created_at ASC
                LIMIT 1
            """).fetchone()
            if row is None:
                return None
            task_id, file_id = row
            updated = db.execute("""
                UPDATE tasks SET status='assigned', worker_id=?, started_at=?
                WHERE id=? AND status='pending'
            """, (self.worker_id, now, task_id)).rowcount
            if updated == 0:
                return None
            _update_api_job(db, task_id, 'assigned')
            db.commit()
        return task_id, file_id

    def _mark(self, db, task_id, status, error=None):
        with _db_write_lock:
            db.execute("""
                UPDATE tasks SET status=?, completed_at=?, error_message=?
                WHERE id=?
            """, (status, datetime.now().isoformat(), error, task_id))
            db.commit()
            _update_api_job(db, task_id, status, error=error)

    def _process(self, db, task_id, file_id):
        keyframe_id = task_id[:-len("_face_detect")]

        kf_row = db.execute(
            "SELECT thumbnail_path FROM keyframes WHERE id=?", (keyframe_id,)
        ).fetchone()

        if kf_row is None:
            # Keyframe gone — mark complete so it doesn't block file indexing
            self._mark(db, task_id, "complete")
            return

        thumb_path = kf_row[0]
        if not thumb_path or not Path(thumb_path).exists():
            self._mark(db, task_id, "failed",
                       "thumbnail not found: %s" % thumb_path)
            return

        faces = detect_faces(thumb_path)
        if faces:
            count = store_faces(db, file_id, thumb_path, faces,
                                keyframe_id=keyframe_id)
            log.info("FaceWorker: %d face(s) — %s" % (count, keyframe_id))

        self._mark(db, task_id, "complete")
        self.processed += 1

    def run(self):
        db = self._get_db()
        log.info("FaceWorker: started")
        idle_logged = False

        while self.indexer_running():
            try:
                result = self._claim_task(db)
                if result is None:
                    if not idle_logged:
                        log.info("FaceWorker: idle — waiting for face_detect tasks")
                        idle_logged = True
                    time.sleep(2)
                    continue
                idle_logged = False
                task_id, file_id = result
                try:
                    self._process(db, task_id, file_id)
                except Exception as e:
                    log.error("FaceWorker: error on task %s: %s" % (task_id, e))
                    try:
                        self._mark(db, task_id, "failed", str(e)[:500])
                    except Exception:
                        pass
                    self.errors += 1
            except Exception as e:
                log.error("FaceWorker: unexpected error: %s" % e)
                time.sleep(5)

        db.close()
        log.info("FaceWorker: stopped (%d processed, %d errors)" % (
            self.processed, self.errors))

    def start(self):
        self._thread = threading.Thread(
            target=self.run, name="face-worker", daemon=True)
        self._thread.start()


class ALAWorker:
    """Alignment worker — forwards jobs to ALA server on port 8085.

    API-only: the crawler never creates 'ala' tasks. Apps submit alignment
    jobs via POST /api/jobs with task_type='ala', an audio file, and lyrics.
    This worker claims those tasks and forwards them to the standalone ALA
    FastAPI server (ala-server.service) running on localhost:8085.
    """

    def __init__(self, db_path, running_fn):
        self.db_path = db_path
        self.indexer_running = running_fn
        self.worker_id = "ala-worker"
        self.ala_url = "http://127.0.0.1:8085/align"
        self.processed = 0
        self.errors = 0
        self._thread = None
        self.current_task_info = None  # for worker-status

    def _get_db(self):
        db = sqlite3.connect(str(self.db_path), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _claim_task(self, db):
        now = datetime.now().isoformat()
        with _db_write_lock:
            row = db.execute("""
                SELECT t.id, t.file_id, t.api_job_id
                FROM tasks t
                WHERE t.task_type = 'ala'
                  AND t.status = 'pending'
                ORDER BY (CASE WHEN t.source = 'api' THEN 0 ELSE 1 END), t.created_at ASC
                LIMIT 1
            """).fetchone()
            if row is None:
                return None
            task_id, file_id, api_job_id = row
            updated = db.execute("""
                UPDATE tasks SET status='assigned', worker_id=?, started_at=?
                WHERE id=? AND status='pending'
            """, (self.worker_id, now, task_id)).rowcount
            if updated == 0:
                return None
            _update_api_job(db, task_id, 'assigned')
            db.commit()
        return task_id, file_id, api_job_id

    def _mark(self, db, task_id, status, error=None):
        with _db_write_lock:
            db.execute("""
                UPDATE tasks SET status=?, completed_at=?, error_message=?
                WHERE id=?
            """, (status, datetime.now().isoformat(), error, task_id))
            db.commit()
            _update_api_job(db, task_id, status, error=error)

    def _process(self, db, task_id, file_id, api_job_id):
        # Get the upload path and lyrics
        file_row = db.execute("SELECT path, filename FROM files WHERE id=?", (file_id,)).fetchone()
        if not file_row:
            self._mark(db, task_id, "failed", "File record not found")
            return

        upload_path, filename = file_row
        self.current_task_info = {"source": "api", "file": filename, "task_type": "ala"}

        # Get lyrics from api_jobs
        lyrics = ""
        if api_job_id:
            lyrics_row = db.execute("SELECT lyrics FROM api_jobs WHERE id=?", (api_job_id,)).fetchone()
            if lyrics_row and lyrics_row[0]:
                lyrics = lyrics_row[0]

        if not lyrics:
            self._mark(db, task_id, "failed", "No lyrics provided")
            self.current_task_info = None
            return

        if not Path(upload_path).exists():
            self._mark(db, task_id, "failed", "Audio file not found: %s" % upload_path)
            self.current_task_info = None
            return

        # POST to ALA server
        try:
            import urllib.request
            boundary = "----ALABoundary%s" % uuid.uuid4().hex[:12]
            body = b""
            # Add lyrics field
            body += b"--%s\r\n" % boundary.encode()
            body += b"Content-Disposition: form-data; name=\"lyrics\"\r\n\r\n"
            body += lyrics.encode("utf-8") + b"\r\n"
            # Add audio file
            body += b"--%s\r\n" % boundary.encode()
            body += b"Content-Disposition: form-data; name=\"audio\"; filename=\"%s\"\r\n" % filename.encode()
            body += b"Content-Type: application/octet-stream\r\n\r\n"
            with open(upload_path, "rb") as f:
                body += f.read()
            body += b"\r\n--%s--\r\n" % boundary.encode()

            req = urllib.request.Request(
                self.ala_url,
                data=body,
                headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
                method="POST"
            )
            resp = urllib.request.urlopen(req, timeout=660)  # 11 min (ALA has 10 min timeout)
            result = json.loads(resp.read())

            # Store result directly in api_jobs
            if api_job_id:
                with _db_write_lock:
                    db.execute(
                        "UPDATE api_jobs SET result=? WHERE id=?",
                        (json.dumps(result), api_job_id)
                    )
                    db.commit()

            self._mark(db, task_id, "complete")
            self.processed += 1
            log.info("ALAWorker: complete — %s (%d words)" % (filename, len(result.get("words", []))))

        except Exception as e:
            log.error("ALAWorker: error on %s: %s" % (filename, e))
            self._mark(db, task_id, "failed", str(e)[:500])
            self.errors += 1

        self.current_task_info = None

    def run(self):
        db = self._get_db()
        log.info("ALAWorker: started (forwarding to %s)" % self.ala_url)
        idle_logged = False

        while self.indexer_running():
            try:
                result = self._claim_task(db)
                if result is None:
                    if not idle_logged:
                        log.info("ALAWorker: idle — waiting for ala tasks")
                        idle_logged = True
                    time.sleep(5)
                    continue
                idle_logged = False
                task_id, file_id, api_job_id = result
                try:
                    self._process(db, task_id, file_id, api_job_id)
                except Exception as e:
                    log.error("ALAWorker: error on task %s: %s" % (task_id, e))
                    try:
                        self._mark(db, task_id, "failed", str(e)[:500])
                    except Exception:
                        pass
                    self.errors += 1
                    self.current_task_info = None
            except Exception as e:
                log.error("ALAWorker: unexpected error: %s" % e)
                time.sleep(5)

        db.close()
        log.info("ALAWorker: stopped (%d processed, %d errors)" % (self.processed, self.errors))

    def start(self):
        self._thread = threading.Thread(
            target=self.run, name="ala-worker", daemon=True)
        self._thread.start()


# ---------------------------------------------------------------------------
# Pipeline Functions (for GPU optimization)
# ---------------------------------------------------------------------------

def prepare_media_tasks(db, fid, fpath, folder_path, cuts=None):
    """
    Phase 1: CPU-only preparation work.
    Extracts keyframes (if video), prepares metadata.
    cuts: precomputed scene cuts from SceneDetectPool (or None).
    Returns list of vision tasks to process on GPU.

    Returns: list of task dicts, each with:
        - task_type: "image", "keyframe", or "audio"
        - fid: file ID
        - image_path: path to image/keyframe/audio file
        - context: folder context string
        - metadata: dict with file_type, duration, width, height, codec, etc.
        - keyframe_id: (for keyframes only) keyframe ID
        - is_first: (for keyframes only) True if first keyframe
        - total_frames: (for keyframes only) total keyframes for this video
    """
    filepath = Path(fpath)
    ext = filepath.suffix.lower()
    ftype = get_file_type(ext)

    # Build folder context
    context = ""
    if folder_path:
        rel = os.path.relpath(os.path.dirname(fpath), folder_path)
        context = "%s/%s" % (Path(folder_path).name, rel)

    # Check file exists
    if not filepath.exists():
        with _db_write_lock:
            db.execute("UPDATE files SET status = 'offline' WHERE id = ?", (fid,))
            db.commit()
        return []  # No tasks

    try:
        # Extract metadata
        stat = filepath.stat()
        meta = probe_media(filepath) if ftype in ("video", "audio") else {}
        if meta is None:
            meta = {}

        # Update DB with metadata
        with _db_write_lock:
            db.execute("""
                UPDATE files SET
                    file_type = ?, size_bytes = ?, modified_at = ?,
                    duration_seconds = ?, width = ?, height = ?, codec = ?,
                    status = 'indexing'
                WHERE id = ?
            """, (
                ftype, stat.st_size, datetime.fromtimestamp(stat.st_mtime).isoformat(),
                meta.get("duration"), meta.get("width"), meta.get("height"),
                meta.get("codec"), fid
            ))
            db.commit()

        tasks = []

        if ftype == "image":
            img_b64, img_mime = pre_encode_image(str(filepath))
            tasks.append({
                "task_type": "image",
                "fid": fid,
                "image_path": str(filepath),
                "context": context,
                "metadata": {"file_type": ftype},
                "image_b64": img_b64,
                "image_mime": img_mime
            })

        elif ftype == "video":
            # Extract keyframes (CPU-intensive ffmpeg work)
            # Transcription is handled by WhisperWorker (async, doesn't block prep).
            duration = meta.get("duration", 0)
            frames = extract_keyframes(str(filepath), duration, fid, precomputed_cuts=cuts)

            if frames:
                # Insert all keyframes into DB upfront
                with _db_write_lock:
                    for ts, thumb_path in frames:
                        kf_id = "%s_%.1f" % (fid, ts)
                        db.execute("""
                            INSERT OR REPLACE INTO keyframes (id, file_id, timestamp_seconds, thumbnail_path)
                            VALUES (?, ?, ?, ?)
                        """, (kf_id, fid, ts, thumb_path))
                    db.commit()

                # Create vision tasks for each keyframe (pre-encode while GPU is busy)
                for idx, (ts, thumb_path) in enumerate(frames):
                    kf_id = "%s_%.1f" % (fid, ts)
                    img_b64, img_mime = pre_encode_image(thumb_path)
                    tasks.append({
                        "task_type": "keyframe",
                        "fid": fid,
                        "image_path": thumb_path,
                        "context": context,
                        "metadata": {"file_type": ftype, "duration": duration},
                        "keyframe_id": kf_id,
                        "is_first": (idx == 0),
                        "total_frames": len(frames),
                        "image_b64": img_b64,
                        "image_mime": img_mime,
                    })
            else:
                # No keyframes extracted — fallback to filename-based description
                tasks.append({
                    "task_type": "video_fallback",
                    "fid": fid,
                    "image_path": str(filepath),
                    "context": context,
                    "metadata": {"file_type": ftype},
                })

        elif ftype == "audio":
            # Transcription is handled by WhisperWorker (async, doesn't block prep).
            # Just create a task for Gemma to generate a filename-based description.
            tasks.append({
                "task_type": "audio",
                "fid": fid,
                "image_path": str(filepath),
                "context": context,
                "metadata": {"file_type": ftype},
            })

        return tasks

    except Exception as e:
        log.error("Error preparing %s: %s" % (filepath.name, e))
        try:
            with _db_write_lock:
                db.execute("""
                    UPDATE files SET status = 'error', error_message = ? WHERE id = ?
                """, (str(e), fid))
                db.commit()
        except Exception:
            pass
        return []


def process_vision_task(db, task, llm_server):
    """
    Phase 2: GPU inference + DB write.
    Takes a prepared task, sends to LLM, writes result to DB.

    Returns: "success", "error", or "offline"
    """
    task_type = task["task_type"]
    fid = task["fid"]
    image_path = task["image_path"]
    context = task["context"]

    try:
        # GPU inference (base64 pre-encoded by prep thread)
        if task_type == "audio":
            description = describe_audio_filename(image_path, context=context, llm_server=llm_server)
        elif task_type == "video_fallback":
            description = "Video file: %s" % Path(image_path).name
        else:  # image or keyframe
            description = describe_image(
                image_path, context=context, llm_server=llm_server,
                image_b64=task.get("image_b64"), image_mime=task.get("image_mime")
            )

        # Face detection moved to separate post-processing pass to avoid blocking GPU

        # Write description to DB — use _db_write_lock to prevent "database is locked"
        # stalls when two GPU threads commit simultaneously (WAL mode only allows one writer)
        if task_type == "keyframe":
            kf_id = task["keyframe_id"]
            with _db_write_lock:
                db.execute("""
                    UPDATE keyframes SET ai_description = ? WHERE id = ?
                """, (description, kf_id))

                # If this is the first keyframe, also set the main file description + transcript
                if task.get("is_first"):
                    filepath = Path(image_path)
                    path_parts = [p for p in filepath.parts if p not in ("/", "Volumes", "Vault", "mnt", "vault")]
                    tags = ", ".join(path_parts[:-1])
                    db.execute("""
                        UPDATE files SET ai_description = ?, tags = ? WHERE id = ?
                    """, (description, tags, fid))

                db.commit()

            # Check completion outside the lock (read-only)
            is_complete = db.execute("""
                SELECT COUNT(*) FROM keyframes
                WHERE file_id = ? AND ai_description IS NULL
            """, (fid,)).fetchone()[0] == 0

            if is_complete:
                with _db_write_lock:
                    db.execute("""
                        UPDATE files SET status = 'indexed', indexed_at = ? WHERE id = ?
                    """, (datetime.now().isoformat(), fid))
                    db.commit()

        else:
            # Image, audio, or video_fallback — single task, mark complete
            filepath = Path(image_path)
            path_parts = [p for p in filepath.parts if p not in ("/", "Volumes", "Vault", "mnt", "vault")]
            tags = ", ".join(path_parts[:-1])

            with _db_write_lock:
                db.execute("""
                    UPDATE files SET
                        ai_description = ?, tags = ?,
                        indexed_at = ?, status = 'indexed'
                    WHERE id = ?
                """, (description, tags, datetime.now().isoformat(), fid))
                db.commit()

        return "success"

    except Exception as e:
        log.error("Error processing task for %s: %s" % (Path(image_path).name, e))
        try:
            with _db_write_lock:
                db.execute("""
                    UPDATE files SET status = 'error', error_message = ? WHERE id = ?
                """, (str(e), fid))
                db.commit()
        except Exception:
            pass
        return "error"


class WhisperWorker:
    """
    Dedicated thread for Whisper transcription, independent of Gemma GPUs.
    Prep threads feed (fid, filepath) into the queue; this worker extracts audio,
    sends to Whisper server, and writes transcripts directly to DB.
    This prevents Whisper from blocking keyframe prep and starving the Gemma GPUs.
    """
    def __init__(self, whisper_queue, indexer_running, llm_servers=None):
        self.queue = whisper_queue
        self.indexer_running = indexer_running
        self.llm_servers = llm_servers or []
        self.thread = None
        self.processed = 0
        self.errors = 0

    def run(self):
        db = sqlite3.connect(str(DB_PATH), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        log.info("WhisperWorker started")

        while self.indexer_running():
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is None:  # Sentinel = done
                break

            fid, filepath = item
            fname = Path(filepath).name
            audio_tmp = DATA_DIR / ("tmp_whisper_%s.wav" % fid)
            try:
                log.info("WhisperWorker: extracting audio from %s" % fname)
                TRANSCRIBE_HEARTBEAT.touch()
                if extract_audio_for_transcription(str(filepath), str(audio_tmp)):
                    TRANSCRIBE_HEARTBEAT.touch()
                    chunks = split_audio_into_chunks(audio_tmp)
                    chunk_texts = []
                    for i, (chunk_path, start_secs) in enumerate(chunks):
                        is_last = (i == len(chunks) - 1)
                        log.info("WhisperWorker: transcribing %s [chunk %d/%d, t=%.0fs]" % (
                            fname, i + 1, len(chunks), start_secs))
                        TRANSCRIBE_HEARTBEAT.touch()
                        text = transcribe_audio(str(chunk_path), timeout=WHISPER_TIMEOUT)
                        if text:
                            chunk_texts.append(text)
                        # Delete chunk temp file (but not the original audio_tmp)
                        if chunk_path != audio_tmp and chunk_path.exists():
                            try:
                                chunk_path.unlink()
                            except Exception:
                                pass
                    transcript = " ".join(chunk_texts).strip() or None
                    if transcript:
                        log.info("WhisperWorker: %s -> %d chars (%d chunks)" % (
                            fname, len(transcript), len(chunks)))
                        with _db_write_lock:
                            db.execute("UPDATE files SET transcript = ? WHERE id = ?",
                                       (transcript, fid))
                            db.commit()
                        self.processed += 1
                    else:
                        log.info("WhisperWorker: no speech detected in %s" % fname)
                else:
                    log.warning("WhisperWorker: audio extraction failed for %s" % fname)
                    self.errors += 1
            except Exception as e:
                log.error("WhisperWorker error on %s: %s" % (fname, e))
                self.errors += 1
            finally:
                if audio_tmp.exists():
                    audio_tmp.unlink()

            self.queue.task_done()

        db.close()
        if TRANSCRIBE_HEARTBEAT.exists():
            TRANSCRIBE_HEARTBEAT.unlink()
        log.info("WhisperWorker done (%d transcribed, %d errors)" % (self.processed, self.errors))

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name="whisper-worker")
        self.thread.start()

    def join(self):
        if self.thread:
            self.thread.join()


class SceneDetectPool:
    """Pool of scene detection workers, one per GPU decode ASIC.
    Runs between global_queue and prep threads so all 3 GPUs decode in parallel.

    Flow: global_queue → [3 scene workers] → prepped_queue → [2 prep threads]
    """
    def __init__(self, global_queue, prepped_queue, indexer_running, num_prep_threads):
        self.global_queue = global_queue
        self.prepped_queue = prepped_queue
        self.indexer_running = indexer_running
        self.num_prep_threads = num_prep_threads
        self.threads = []

    def _worker_loop(self, vaapi_device, worker_id):
        """Pull files, run scene detection on assigned GPU, pass to prep threads."""
        while self.indexer_running():
            try:
                fid, fpath, folder_path = self.global_queue.get(timeout=0.5)
            except queue.Empty:
                break

            cuts = None  # None = not a video or too short, prep thread skips scene detect
            filepath = Path(fpath)
            ext = filepath.suffix.lower()

            if ext in VIDEO_EXTS:
                duration = _quick_duration(fpath)
                if duration >= 5:
                    cuts = detect_scene_changes(fpath, duration, vaapi_device=vaapi_device)
                    name = filepath.name
                    if cuts:
                        log.info("Scene detect %s: %d cuts (%.0fs) [gpu %d]",
                                 name, len(cuts), duration, worker_id)
                    else:
                        log.info("Scene detect %s: no cuts (%.0fs) [gpu %d]",
                                 name, duration, worker_id)

            self.prepped_queue.put((fid, fpath, folder_path, cuts))
            self.global_queue.task_done()

        log.info("SceneDetectPool worker %d done", worker_id)

    def start(self):
        """Start one worker thread per VAAPI device."""
        for i, device in enumerate(VAAPI_DEVICES):
            t = threading.Thread(
                target=self._worker_loop, args=(device, i),
                name="scene-detect-%d" % i, daemon=True
            )
            t.start()
            self.threads.append(t)
        log.info("SceneDetectPool: %d workers started (3-GPU parallel decode)", len(self.threads))

    def join(self):
        """Wait for all workers, then send sentinels to prep threads."""
        for t in self.threads:
            t.join()
        for _ in range(self.num_prep_threads):
            self.prepped_queue.put(None)
        log.info("SceneDetectPool: all workers done, sentinels sent")


class PipelineWorker:
    """
    Pipeline per GPU: CPU thread prepares work, GPU thread processes.
    This keeps the GPU fed with pre-extracted keyframes so it never waits for ffmpeg.
    """
    def __init__(self, gpu_id, llm_server, prepped_queue, stats_lock, stats_dict, indexer_running):
        self.gpu_id = gpu_id
        self.llm_server = llm_server
        self.prepped_queue = prepped_queue  # From SceneDetectPool: (fid, fpath, folder_path, cuts)
        self.task_queue = queue.Queue(maxsize=12)  # Per-GPU prefetch buffer (3-4 videos worth)
        self.stats_lock = stats_lock
        self.stats_dict = stats_dict
        self.indexer_running = indexer_running
        self.db_prep = None
        self.db_gpu = None
        self.prep_thread = None
        self.gpu_thread = None
        self.processed_count = 0

    def prep_loop(self):
        """CPU thread: extract thumbnails, prepare tasks, queue for GPU.
        Scene detection is already done by SceneDetectPool — cuts arrive via prepped_queue."""
        port = self.llm_server.split(":")[-1]
        self.db_prep = sqlite3.connect(str(DB_PATH), timeout=30)
        self.db_prep.execute("PRAGMA journal_mode=WAL")
        self.db_prep.execute("PRAGMA busy_timeout=30000")

        log.info("GPU %s prep thread started" % port)

        while self.indexer_running():
            try:
                item = self.prepped_queue.get(timeout=1.0)
            except queue.Empty:
                continue  # SceneDetectPool may still be producing

            if item is None:  # Sentinel from SceneDetectPool
                break

            fid, fpath, folder_path, cuts = item

            try:
                # CPU work: extract thumbnails, prepare metadata (scene detect already done)
                fname = Path(fpath).name
                log.info("GPU %s prep: extracting %s" % (port, fname))
                start_time = time.time()
                tasks = prepare_media_tasks(self.db_prep, fid, fpath, folder_path, cuts=cuts)
                prep_time = time.time() - start_time

                log.info("GPU %s prep: %s -> %d tasks (%.1fs, queue depth: %d)" %
                        (port, fname, len(tasks), prep_time, self.task_queue.qsize()))

                # Queue each vision task for GPU thread
                for task in tasks:
                    self.task_queue.put(task)  # Blocks if queue full (backpressure)

                if not tasks:
                    # File was offline or had errors — still count it
                    with self.stats_lock:
                        self.stats_dict["errors"] += 1

            except Exception as e:
                log.error("GPU %s prep error on %s: %s" % (port, Path(fpath).name, e))
                with self.stats_lock:
                    self.stats_dict["errors"] += 1

        # Signal inference thread we're done
        self.task_queue.put(None)
        self.db_prep.close()
        log.info("GPU %s prep thread done" % port)

    def gpu_loop(self):
        """GPU thread: pull tasks from queue, run vision inference, write results."""
        port = self.llm_server.split(":")[-1]
        thread_name = threading.current_thread().name
        # Each thread gets its own DB connection (SQLite requires this)
        db = sqlite3.connect(str(DB_PATH), timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")

        log.info("GPU %s inference thread [%s] started" % (port, thread_name))

        while self.indexer_running():
            try:
                task = self.task_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if task is None:  # Sentinel from prep thread
                break

            try:
                task_type = task.get("task_type", "unknown")
                fname = Path(task.get("image_path", "")).name
                queue_depth = self.task_queue.qsize()

                log.info("GPU %s [%s]: %s [%s] (queue: %d)" %
                        (port, thread_name, fname, task_type, queue_depth))

                start_time = time.time()
                result = process_vision_task(db, task, self.llm_server)
                inference_time = time.time() - start_time

                log.info("GPU %s [%s]: %s done (%.1fs)" % (port, thread_name, fname, inference_time))
                with self.stats_lock:
                    self.processed_count += 1
                    if result == "success":
                        self.stats_dict["indexed"] += 1
                    else:
                        self.stats_dict["errors"] += 1

            except Exception as e:
                log.error("GPU %s [%s] inference error: %s" % (port, thread_name, e))
                with self.stats_lock:
                    self.processed_count += 1
                    self.stats_dict["errors"] += 1

        db.close()
        log.info("GPU %s [%s] inference thread done" % (port, thread_name))

    def start(self):
        """Start prep thread + inference thread."""
        self.prep_thread = threading.Thread(target=self.prep_loop, daemon=True)
        self.gpu_thread = threading.Thread(target=self.gpu_loop, daemon=True,
                                            name="gpu-%s" % self.gpu_id)
        self.prep_thread.start()
        self.gpu_thread.start()

    def join(self):
        """Wait for all threads to complete."""
        if self.prep_thread:
            self.prep_thread.join()
        if self.gpu_thread:
            self.gpu_thread.join()

    def get_queue_depth(self):
        """Return current number of tasks waiting for GPU."""
        return self.task_queue.qsize()


# ---------------------------------------------------------------------------
# Main indexer
# ---------------------------------------------------------------------------

class MediaIndexer:
    def __init__(self):
        self.db = init_db()
        self.running = True
        self.stats = {"scanned": 0, "indexed": 0, "errors": 0, "skipped": 0}
        self.pipeline_workers = []  # Will be populated during process_pending()
        self.scanner = {
            "state": "idle",       # "scanning" | "processing" | "sleeping" | "idle"
            "current_folder": None,
            "files_scanned": 0,
            "files_new": 0,
            "next_scan_in": None,  # seconds remaining (int, while sleeping)
        }

        # Initialize ChromaDB for semantic search
        if HAS_CHROMADB:
            try:
                CHROMA_DIR.mkdir(parents=True, exist_ok=True)
                self.chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
                self.chroma_collection = self.chroma_client.get_or_create_collection(
                    name="media_descriptions",
                    metadata={"hnsw:space": "cosine"}
                )
                count = self.chroma_collection.count()
                log.info("ChromaDB ready (%d embeddings)" % count)
            except Exception as e:
                log.warning("ChromaDB init failed: %s" % str(e))
                self.chroma_client = None
                self.chroma_collection = None
        else:
            self.chroma_client = None
            self.chroma_collection = None

    def add_folder(self, folder_path):
        """Add a folder to be indexed."""
        fpath = str(Path(folder_path).resolve())
        fid = folder_id(fpath)
        self.db.execute("""
            INSERT OR IGNORE INTO folders (id, path, name)
            VALUES (?, ?, ?)
        """, (fid, fpath, Path(fpath).name))
        self.db.commit()
        log.info(f"Added folder: {fpath}")
        return fid

    def scan_folder(self, folder_path):
        """Scan a folder and register new/changed files."""
        fpath = str(Path(folder_path).resolve())
        fid = folder_id(fpath)
        count = 0
        files_walked = 0

        self.scanner["state"] = "scanning"
        self.scanner["current_folder"] = fpath
        self.scanner["files_scanned"] = 0
        self.scanner["files_new"] = 0
        self.scanner["next_scan_in"] = None
        write_scanner_state(self.scanner)

        log.info(f"Scanning: {fpath}")

        for media_path in crawl_folder(fpath):
            if not self.running:
                break

            files_walked += 1
            self.scanner["files_scanned"] = files_walked
            self.scanner["current_folder"] = str(media_path.parent)
            if files_walked % 200 == 0:
                write_scanner_state(self.scanner)

            try:
                stat = media_path.stat()
                mid = file_id(str(media_path), stat.st_size, stat.st_mtime)

                # Check if already indexed with same hash
                existing = self.db.execute(
                    "SELECT status FROM files WHERE id = ?", (mid,)
                ).fetchone()

                if existing and existing[0] == "indexed":
                    self.stats["skipped"] += 1
                    continue

                # Insert or update file record
                self.db.execute("""
                    INSERT OR IGNORE INTO files (id, path, filename, folder_id, status)
                    VALUES (?, ?, ?, ?, 'pending')
                """, (mid, str(media_path), media_path.name, fid))
                count += 1
                self.scanner["files_new"] = count
                self.stats["scanned"] += 1

            except (OSError, PermissionError) as e:
                log.warning(f"Cannot access: {media_path}: {e}")
                continue

        self.db.execute("""
            UPDATE folders SET last_scan = ?, file_count = ?
            WHERE id = ?
        """, (datetime.now().isoformat(), count, fid))
        self.db.commit()

        self.scanner["state"] = "idle"
        self.scanner["current_folder"] = None
        write_scanner_state(self.scanner)

        log.info(f"Scan complete: {count} new files found")
        return count

    def _check_server_health(self, server_url, timeout=5):
        """Check if a llama-server instance is healthy."""
        try:
            req = urllib.request.Request("%s/health" % server_url)
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read())
            return data.get("status") == "ok"
        except Exception:
            return False

    def process_pending(self):
        """Process all pending files with concurrent LLM requests.

        Uses a shared queue so whichever GPU finishes first grabs the
        next task immediately (work-stealing), instead of waiting for
        the slowest GPU to finish its pre-assigned batch.
        """
        # Reset any files left in "indexing" from a previous crashed run
        stuck = self.db.execute(
            "SELECT COUNT(*) FROM files WHERE status = 'indexing'"
        ).fetchone()[0]
        if stuck:
            self.db.execute("UPDATE files SET status = 'pending' WHERE status = 'indexing'")
            self.db.commit()
            log.info("Reset %d stuck 'indexing' files back to pending" % stuck)

        # Discover available LLM servers
        available_servers = []
        for server in LLM_SERVERS:
            if self._check_server_health(server):
                available_servers.append(server)

        # Fallback: try single-server mode
        if not available_servers:
            if self._check_server_health(LLM_FALLBACK):
                available_servers = [LLM_FALLBACK]
                log.info("Using single-server mode (%s)" % LLM_FALLBACK)
            else:
                log.error("No LLM servers available! Start servers first.")
                log.error("  Multi-GPU: ~/start-indexer-gpus.sh")
                log.error("  Single:    ~/start-llm-server.sh gemma")
                return self.stats

        num_workers = len(available_servers)
        log.info("Found %d LLM server(s): %s" % (num_workers, ", ".join(available_servers)))

        pending = self.db.execute("""
            SELECT f.id, f.path, fo.path as folder_path
            FROM files f
            LEFT JOIN folders fo ON f.folder_id = fo.id
            WHERE f.status = 'pending'
            ORDER BY f.path
        """).fetchall()

        total = len(pending)
        log.info("Processing %d pending files (workers: %d)" % (total, num_workers))

        # Shared queue — each GPU worker pulls the next task when free
        work_queue = queue.Queue()
        for fid, fpath, folder_path in pending:
            work_queue.put((fid, fpath, folder_path))

        # Thread-safe stats
        stats_lock = threading.Lock()

        # Check if Whisper server is available for async transcription
        whisper_queue = None
        whisper_worker = None
        whisper_ok = self._check_server_health(WHISPER_SERVER)
        if whisper_ok:
            whisper_queue = queue.Queue()
            whisper_worker = WhisperWorker(whisper_queue, lambda: self.running,
                                           llm_servers=available_servers)
            whisper_worker.start()
            log.info("WhisperWorker started (async transcription on %s)" % WHISPER_SERVER)

            # Pre-filter whisper queue: only queue files that actually have
            # an audio stream.  Without this, WhisperWorker wastes time doing
            # sequential ffprobe checks on thousands of R3D/BRAW files with
            # no audio.  The filtering runs in a background thread with 4
            # parallel ffprobe workers so it doesn't block startup.
            whisper_candidates = [
                (fid, fpath) for fid, fpath, folder_path in pending
                if Path(fpath).suffix.lower() in VIDEO_EXTS | AUDIO_EXTS
            ]

            def _prefilter_whisper():
                count = 0
                skipped = 0
                def _check(item):
                    fid, fpath = item
                    return (fid, fpath, has_audio_stream(fpath))
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for fid, fpath, has_audio in pool.map(_check, whisper_candidates, chunksize=10):
                        if not self.running:
                            break
                        if has_audio:
                            whisper_queue.put((fid, fpath))
                            count += 1
                        else:
                            skipped += 1
                log.info("WhisperWorker: pre-filtered %d files with audio (%d skipped)" %
                         (count, skipped))

            prefilter_thread = threading.Thread(
                target=_prefilter_whisper, name="whisper-prefilter", daemon=True)
            prefilter_thread.start()
            log.info("WhisperWorker: pre-filtering %d candidates in background" %
                     len(whisper_candidates))
        else:
            log.warning("Whisper server not available — skipping transcription")

        # Scene detection pool: 3 workers (one per GPU decode ASIC) run ahead of prep threads
        prepped_queue = queue.Queue(maxsize=30)
        scene_pool = SceneDetectPool(
            global_queue=work_queue,
            prepped_queue=prepped_queue,
            indexer_running=lambda: self.running,
            num_prep_threads=num_workers,
        )
        scene_pool.start()

        # Create pipeline workers (2 threads per GPU: prep + inference)
        # Prep threads pull from prepped_queue (scene cuts already computed)
        self.pipeline_workers = []
        for gpu_id, server in enumerate(available_servers):
            worker = PipelineWorker(
                gpu_id=gpu_id,
                llm_server=server,
                prepped_queue=prepped_queue,
                stats_lock=stats_lock,
                stats_dict=self.stats,
                indexer_running=lambda: self.running,
            )
            worker.start()
            self.pipeline_workers.append(worker)

        log.info("Pipeline started: %d GPU workers + SceneDetectPool(3) + WhisperWorker" %
                 len(self.pipeline_workers))

        # Wait for scene detect pool to finish and send sentinels to prep threads
        # (runs in background so we can proceed to join pipeline workers)
        scene_sentinel_thread = threading.Thread(
            target=scene_pool.join, daemon=True, name="scene-sentinel")
        scene_sentinel_thread.start()

        # Wait for all Gemma workers to complete
        for worker in self.pipeline_workers:
            worker.join()

        scene_sentinel_thread.join()

        # Signal WhisperWorker to finish and wait
        if whisper_worker:
            prefilter_thread.join()  # Ensure all audio files have been queued
            whisper_queue.put(None)  # Sentinel
            whisper_worker.join()
            log.info("WhisperWorker: %d transcribed, %d errors" %
                     (whisper_worker.processed, whisper_worker.errors))

        # Batch-sync new descriptions to ChromaDB (CPU embedding, no GPU needed)
        if self.chroma_collection is not None and self.stats["indexed"] > 0:
            log.info("Syncing %d new descriptions to ChromaDB..." % self.stats["indexed"])
            try:
                rows = self.db.execute("""
                    SELECT id, ai_description, filename, file_type, tags, path
                    FROM files WHERE status = 'indexed' AND ai_description IS NOT NULL
                    AND id NOT IN (SELECT id FROM files WHERE ai_description IS NULL)
                """).fetchall()
                # Get existing IDs in ChromaDB to find what's new
                existing = set()
                try:
                    existing = set(self.chroma_collection.get()["ids"])
                except Exception:
                    pass
                batch_ids = []
                batch_docs = []
                batch_meta = []
                for fid, desc, fname, ftype, tags, fpath in rows:
                    if fid not in existing:
                        batch_ids.append(fid)
                        batch_docs.append(desc)
                        batch_meta.append({
                            "filename": fname or "",
                            "file_type": ftype or "",
                            "tags": tags or "",
                            "path": fpath or ""
                        })
                if batch_ids:
                    # Upsert in chunks of 100
                    for start in range(0, len(batch_ids), 100):
                        end = start + 100
                        self.chroma_collection.upsert(
                            ids=batch_ids[start:end],
                            documents=batch_docs[start:end],
                            metadatas=batch_meta[start:end]
                        )
                    log.info("ChromaDB synced: %d new embeddings" % len(batch_ids))
            except Exception as e:
                log.warning("ChromaDB batch sync failed: %s" % str(e))

        return self.stats

    def get_status(self):
        """Get current indexing status."""
        counts = {}
        for status in ("pending", "indexing", "indexed", "error", "offline"):
            row = self.db.execute(
                "SELECT COUNT(*) FROM files WHERE status = ?", (status,)
            ).fetchone()
            counts[status] = row[0]

        # Per-type breakdown (video, image, audio)
        type_rows = self.db.execute(
            "SELECT file_type, status, COUNT(*) FROM files GROUP BY file_type, status"
        ).fetchall()
        by_type = {}
        for ftype, fstatus, cnt in type_rows:
            t = ftype or "other"
            if t not in by_type:
                by_type[t] = {"pending": 0, "indexing": 0, "indexed": 0, "error": 0, "offline": 0}
            if fstatus in by_type[t]:
                by_type[t][fstatus] = cnt
        # Transcription progress (video + audio combined, regardless of indexing status)
        trans_indexed = self.db.execute(
            "SELECT COUNT(*) FROM files WHERE file_type IN ('video', 'audio')"
            " AND transcript IS NOT NULL AND transcript <> ''"
        ).fetchone()[0]
        trans_total = self.db.execute(
            "SELECT COUNT(*) FROM files WHERE file_type IN ('video', 'audio')"
        ).fetchone()[0]
        by_type["transcription"] = {
            "pending": trans_total - trans_indexed,
            "indexing": 0,
            "indexed": trans_indexed,
            "error": 0,
            "offline": 0,
        }
        counts["by_type"] = by_type

        folders = self.db.execute(
            "SELECT path, file_count, last_scan FROM folders WHERE enabled = 1"
        ).fetchall()

        # Include per-GPU queue depths if pipeline is active
        gpu_queues = []
        if hasattr(self, 'pipeline_workers') and self.pipeline_workers:
            for worker in self.pipeline_workers:
                gpu_queues.append({
                    "gpu_id": worker.gpu_id,
                    "server": worker.llm_server,
                    "queue_depth": worker.get_queue_depth(),
                    "processed": worker.processed_count
                })

        # Detect if a separate transcribe process is actively running
        transcribing = False
        if TRANSCRIBE_HEARTBEAT.exists():
            age = time.time() - TRANSCRIBE_HEARTBEAT.stat().st_mtime
            transcribing = age < 90  # active if touched within 90 seconds

        # Read scanner state written by the watch process (separate process)
        scanner_out = dict(self.scanner)
        if SCANNER_STATE_FILE.exists():
            try:
                with open(SCANNER_STATE_FILE) as f:
                    saved = json.load(f)
                age = time.time() - saved.get("updated", 0)
                if age < 45:  # stale after 45s (writer thread refreshes every 15s)
                    scanner_out = saved["state"]
            except Exception:
                pass
        scanner_out["transcribing"] = transcribing

        result = {"counts": counts, "folders": folders, "stats": self.stats, "scanner": scanner_out}
        if gpu_queues:
            result["gpu_queues"] = gpu_queues
        return result

    def _get_match_markers(self, db, file_id, keywords, transcript_segments_json=None):
        """Return (matched_keyframes, matched_segments, matched_faces) for a file.

        Used by search to highlight which keyframes, transcript segments, and
        faces matched the query so the client can jump directly to them.
        """
        matched_keyframes = []
        matched_segments = []
        matched_faces = []

        # Keyframes whose description contains a keyword
        seen_timestamps = set()
        for kw in keywords:
            rows = db.execute("""
                SELECT timestamp_seconds, ai_description
                FROM keyframes WHERE file_id=? AND ai_description LIKE ?
                ORDER BY timestamp_seconds
            """, (file_id, "%%%s%%" % kw)).fetchall()
            for ts, desc in rows:
                if ts not in seen_timestamps:
                    seen_timestamps.add(ts)
                    idx_row = db.execute(
                        "SELECT COUNT(*) FROM keyframes WHERE file_id=? AND timestamp_seconds <= ?",
                        (file_id, ts)).fetchone()
                    idx = (idx_row[0] - 1) if idx_row else 0
                    snippet = desc[:120] if desc else ""
                    matched_keyframes.append({"index": idx, "timestamp": ts, "description": snippet})
        matched_keyframes.sort(key=lambda x: x["timestamp"])

        # Transcript segments containing a keyword
        if transcript_segments_json:
            try:
                segments = json.loads(transcript_segments_json)
                for seg in segments:
                    seg_text = seg.get("text", "")
                    if any(kw.lower() in seg_text.lower() for kw in keywords):
                        matched_segments.append({
                            "start": seg.get("start", 0),
                            "end": seg.get("end", 0),
                            "text": seg_text.strip()
                        })
            except Exception:
                pass

        # Named faces whose name matches a keyword
        rows = db.execute("""
            SELECT DISTINCT p.name, k.timestamp_seconds
            FROM faces f
            JOIN keyframes k ON f.keyframe_id = k.id
            JOIN persons p ON f.person_id = p.id
            WHERE f.file_id = ? AND p.name IS NOT NULL
            ORDER BY k.timestamp_seconds
        """, (file_id,)).fetchall()
        for name, ts in rows:
            name_words = name.lower().split()
            if any(any(nw in kw.lower() for nw in name_words) for kw in keywords):
                matched_faces.append({"name": name, "timestamp": ts})

        return matched_keyframes, matched_segments, matched_faces

    def search(self, query, limit=20):
        """Search using semantic search (ChromaDB) with FTS keyword fallback."""
        # Use semantic search if ChromaDB is available and populated
        if self.chroma_collection is not None:
            try:
                count = self.chroma_collection.count()
            except Exception:
                count = 0

            if count > 0:
                return self._semantic_search(query, limit)

        # Fall back to keyword-only search
        return self._fts_search(query, limit)

    def _semantic_search(self, query, limit=20):
        """Hybrid semantic + keyword search."""
        scored = {}  # file_id -> score

        # 1. ChromaDB semantic search (cosine similarity)
        try:
            fetch_count = min(limit * 3, 200)
            chroma_results = self.chroma_collection.query(
                query_texts=[query],
                n_results=fetch_count
            )

            for i, fid in enumerate(chroma_results["ids"][0]):
                distance = chroma_results["distances"][0][i]
                scored[fid] = 1.0 - distance  # convert distance to similarity
        except Exception as e:
            log.warning("ChromaDB query failed: %s" % str(e))

        # 2. Boost results that also match FTS keywords
        try:
            # Quote the query for FTS5 safety
            safe_query = build_fts_query(query)
            fts_rows = self.db.execute("""
                SELECT f.id FROM files_fts fts
                JOIN files f ON f.rowid = fts.rowid
                WHERE files_fts MATCH ?
                LIMIT ?
            """, (safe_query, limit * 3)).fetchall()

            fts_ids = set(r[0] for r in fts_rows)
            for fid in scored:
                if fid in fts_ids:
                    scored[fid] += 0.1  # small boost for keyword match

            # Add FTS-only results that ChromaDB missed
            for fid in fts_ids:
                if fid not in scored:
                    scored[fid] = 0.5
        except Exception:
            pass  # FTS might fail on unusual queries

        if not scored:
            return []

        # 3. Sort by combined score and fetch full metadata
        sorted_ids = sorted(scored.keys(), key=lambda x: scored[x], reverse=True)[:limit]

        results = []
        keywords = [w for w in query.lower().split() if len(w) > 2]
        for fid in sorted_ids:
            row = self.db.execute("""
                SELECT path, filename, file_type, ai_description, tags,
                       duration_seconds, width, height, face_names,
                       (SELECT COUNT(*) FROM keyframes WHERE file_id = f.id) AS keyframe_count
                FROM files f WHERE id = ?
            """, (fid,)).fetchone()

            if row:
                result = {
                    "id": fid,
                    "path": row[0], "filename": row[1], "type": row[2],
                    "description": row[3], "tags": row[4],
                    "duration": row[5], "width": row[6], "height": row[7],
                    "face_names": row[8], "keyframe_count": row[9]
                }
                ts_row = self.db.execute(
                    "SELECT transcript_segments FROM files WHERE id=?", (fid,)
                ).fetchone()
                ts_json = ts_row[0] if ts_row else None
                kf, seg, faces = self._get_match_markers(self.db, fid, keywords, ts_json)
                result["matched_keyframes"] = kf
                result["matched_segments"] = seg
                result["matched_faces"] = faces
                results.append(result)

        return results

    def _fts_search(self, query, limit=20):
        """Keyword-only search via SQLite FTS5 (fallback)."""
        try:
            safe_query = build_fts_query(query)
            results = self.db.execute("""
                SELECT f.id, f.path, f.filename, f.file_type, f.ai_description, f.tags,
                       f.duration_seconds, f.width, f.height, f.face_names,
                       (SELECT COUNT(*) FROM keyframes WHERE file_id = f.id) AS keyframe_count
                FROM files_fts fts
                JOIN files f ON f.rowid = fts.rowid
                WHERE files_fts MATCH ?
                ORDER BY bm25(files_fts, 1, 10, 3, 1, 5)
                LIMIT ?
            """, (safe_query, limit)).fetchall()
        except Exception:
            results = []

        keywords = [w for w in query.lower().split() if len(w) > 2]
        out = []
        for r in results:
            fid = r[0]
            result = {
                "id": fid,
                "path": r[1], "filename": r[2], "type": r[3],
                "description": r[4], "tags": r[5],
                "duration": r[6], "width": r[7], "height": r[8],
                "face_names": r[9], "keyframe_count": r[10]
            }
            ts_row = self.db.execute(
                "SELECT transcript_segments FROM files WHERE id=?", (fid,)
            ).fetchone()
            ts_json = ts_row[0] if ts_row else None
            kf, seg, faces = self._get_match_markers(self.db, fid, keywords, ts_json)
            result["matched_keyframes"] = kf
            result["matched_segments"] = seg
            result["matched_faces"] = faces
            out.append(result)
        return out

    def get_thumbnail(self, file_id):
        """Get or generate a thumbnail for a file. Returns (jpeg_bytes, mime) or (None, None)."""
        # Look up file info
        row = self.db.execute(
            "SELECT path, file_type FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        if not row:
            return None, None

        filepath, file_type = row

        # Infer file_type from extension for files registered before the column existed
        if file_type is None:
            _IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.tif', '.tiff', '.bmp', '.webp'}
            _VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.mxf', '.r3d', '.braw', '.ts', '.m4v', '.m4a'}
            ext = Path(filepath).suffix.lower()
            if ext in _IMAGE_EXTS:
                file_type = 'image'
            elif ext in _VIDEO_EXTS:
                file_type = 'video'

        # For videos: use existing extracted keyframe, or generate one on-demand
        if file_type == "video":
            thumb_dir = THUMB_DIR / file_id
            thumb_path = thumb_dir / "frame_00.jpg"
            if thumb_path.exists():
                return thumb_path.read_bytes(), "image/jpeg"
            # No pre-extracted keyframe — generate from source file at t=0
            if Path(filepath).exists():
                thumb_path.parent.mkdir(parents=True, exist_ok=True)
                if extract_thumbnail(filepath, 0, thumb_path):
                    return thumb_path.read_bytes(), "image/jpeg"
            return None, None

        # For images: generate a cached thumbnail
        if file_type == "image":
            cache_dir = THUMB_DIR / file_id
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached = cache_dir / "thumb.jpg"

            if cached.exists():
                return cached.read_bytes(), "image/jpeg"

            # Generate thumbnail with ffmpeg
            if not Path(filepath).exists():
                return None, None

            try:
                cmd = [
                    FFMPEG, "-y", "-i", filepath,
                    "-vf", "scale='min(320,iw)':-2",
                    "-q:v", "5",
                    str(cached)
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode == 0 and cached.exists():
                    return cached.read_bytes(), "image/jpeg"
            except Exception as e:
                log.warning("Thumbnail generation failed for %s: %s" % (file_id, str(e)))

            return None, None

        return None, None

    def reembed_all(self):
        """Embed all existing descriptions into ChromaDB for semantic search."""
        if self.chroma_collection is None:
            log.error("ChromaDB not available — install with: pip3 install chromadb")
            return 0

        rows = self.db.execute("""
            SELECT id, path, filename, file_type, ai_description, tags
            FROM files
            WHERE status = 'indexed' AND ai_description IS NOT NULL
        """).fetchall()

        total = len(rows)
        log.info("Embedding %d descriptions into ChromaDB..." % total)

        batch_size = 100
        embedded = 0

        for i in range(0, total, batch_size):
            if not self.running:
                break

            batch = rows[i:i + batch_size]
            ids = []
            documents = []
            metadatas = []

            for row in batch:
                fid, path, filename, file_type, description, tags = row
                ids.append(fid)
                documents.append(description)
                metadatas.append({
                    "filename": filename or "",
                    "file_type": file_type or "",
                    "tags": tags or "",
                    "path": path
                })

            try:
                self.chroma_collection.upsert(
                    ids=ids,
                    documents=documents,
                    metadatas=metadatas
                )
                embedded += len(batch)
                log.info("  Embedded %d / %d..." % (embedded, total))
            except Exception as e:
                log.error("Batch embed failed: %s" % str(e))

        log.info("Done! Embedded %d descriptions." % embedded)
        return embedded

    # -------------------------------------------------------------------
    # Face recognition methods
    # -------------------------------------------------------------------

    def detect_all_faces(self):
        """Detect faces in all indexed files that haven't been scanned yet."""
        if not HAS_FACE_RECOGNITION:
            log.error("face_recognition not installed")
            return 0

        # Images without face scans
        images = self.db.execute("""
            SELECT f.id, f.path
            FROM files f
            WHERE f.status = 'indexed' AND f.file_type = 'image'
            AND f.id NOT IN (SELECT DISTINCT file_id FROM faces)
        """).fetchall()

        # Video keyframes without face scans
        keyframes = self.db.execute("""
            SELECT k.id, k.file_id, k.thumbnail_path
            FROM keyframes k
            JOIN files f ON k.file_id = f.id
            WHERE f.status = 'indexed'
            AND k.id NOT IN (
                SELECT DISTINCT keyframe_id FROM faces WHERE keyframe_id IS NOT NULL
            )
        """).fetchall()

        total = len(images) + len(keyframes)
        log.info("Scanning %d images + %d keyframes for faces..." % (len(images), len(keyframes)))

        processed = 0
        found_total = 0

        # Process images
        for fid, path in images:
            if not self.running:
                break
            if not Path(path).exists():
                continue
            faces = detect_faces(path)
            if faces:
                count = store_faces(self.db, fid, path, faces)
                found_total += count
                log.info("  %s: %d face(s)" % (Path(path).name, count))
            processed += 1
            if processed % 100 == 0:
                log.info("  Progress: %d / %d (%d faces found)" % (processed, total, found_total))

        # Process keyframes
        for kf_id, fid, thumb_path in keyframes:
            if not self.running:
                break
            if not Path(thumb_path).exists():
                continue
            faces = detect_faces(thumb_path)
            if faces:
                count = store_faces(self.db, fid, thumb_path, faces, keyframe_id=kf_id)
                found_total += count
            processed += 1
            if processed % 100 == 0:
                log.info("  Progress: %d / %d (%d faces found)" % (processed, total, found_total))

        log.info("Face detection complete: scanned %d items, found %d faces" % (processed, found_total))
        return processed

    def cluster_faces(self, tolerance=None, full=False, db=None):
        """Cluster face embeddings using Chinese Whispers.

        By default, only clusters NEW faces (cluster_id IS NULL) to preserve
        existing merges and naming. Use full=True to re-cluster everything.
        Pass db= for thread-safe operation from background threads.

        Returns dict of {cluster_id: [face_ids]}.
        """
        if not HAS_FACE_RECOGNITION:
            log.error("face_recognition not installed")
            return {}

        import dlib  # Available since face_recognition depends on it

        if tolerance is None:
            tolerance = FACE_TOLERANCE

        # Use provided db or create a thread-safe connection
        own_db = False
        if db is None:
            try:
                self.db.execute("SELECT 1")
                target_db = self.db
            except Exception:
                target_db = sqlite3.connect(str(DB_PATH), timeout=30)
                target_db.execute("PRAGMA journal_mode=WAL")
                target_db.execute("PRAGMA busy_timeout=30000")
                own_db = True
        else:
            target_db = db

        try:
            if full:
                rows = target_db.execute(
                    "SELECT id, embedding FROM faces ORDER BY id"
                ).fetchall()
                label_offset = 0
            else:
                rows = target_db.execute(
                    "SELECT id, embedding FROM faces WHERE cluster_id IS NULL ORDER BY id"
                ).fetchall()
                max_existing = target_db.execute(
                    "SELECT COALESCE(MAX(cluster_id), -1) FROM faces"
                ).fetchone()[0]
                label_offset = max_existing + 1

            if not rows:
                log.info("No faces to cluster")
                return {}

            face_ids = [r[0] for r in rows]
            descriptors = [dlib.vector(load_embedding(r[1])) for r in rows]

            log.info("Clustering %d faces (tolerance=%.2f, mode=%s)..." % (
                len(descriptors), tolerance, "full" if full else "incremental"))

            labels = dlib.chinese_whispers_clustering(descriptors, tolerance)

            clusters = {}
            for face_id, label in zip(face_ids, labels):
                cluster_id = int(label) + label_offset
                if cluster_id not in clusters:
                    clusters[cluster_id] = []
                clusters[cluster_id].append(face_id)

            for face_id, label in zip(face_ids, labels):
                cluster_id = int(label) + label_offset
                target_db.execute(
                    "UPDATE faces SET cluster_id = ? WHERE id = ?",
                    (cluster_id, face_id)
                )

            if full:
                target_db.execute("UPDATE faces SET person_id = NULL")

            target_db.commit()

            log.info("Found %d clusters from %d faces" % (len(clusters), len(face_ids)))
            return clusters
        finally:
            if own_db:
                target_db.close()

    def assign_new_faces(self, orchestrator=None):
        """Assign unclustered faces to existing named persons by distance matching.

        Borderline matches (distance 0.3–threshold) are verified by Gemma vision
        if an orchestrator is provided. High-confidence matches (< 0.3) skip
        verification. Faces that don't match remain unclustered.
        Returns count of newly assigned faces.
        """
        if not HAS_FACE_RECOGNITION:
            return 0

        # Load per-person thresholds
        person_thresholds = {}
        for row in self.db.execute("SELECT id, match_threshold FROM persons WHERE match_threshold IS NOT NULL"):
            person_thresholds[row[0]] = row[1]

        # Load best reference thumbnail per person (largest bbox area)
        person_ref_thumbs = {}
        for row in self.db.execute("""
            SELECT person_id, thumbnail_path,
                   (bbox_right - bbox_left) * (bbox_bottom - bbox_top) as area
            FROM faces
            WHERE person_id IS NOT NULL AND thumbnail_path IS NOT NULL
            ORDER BY area DESC
        """).fetchall():
            if row[0] not in person_ref_thumbs:
                person_ref_thumbs[row[0]] = row[1]

        # Load all embeddings for named persons
        named = self.db.execute("""
            SELECT f.id, f.embedding, f.person_id
            FROM faces f
            WHERE f.person_id IS NOT NULL
        """).fetchall()

        if not named:
            return 0

        known_encodings = [load_embedding(r[1]) for r in named]
        known_person_ids = [r[2] for r in named]

        # Load unclustered/unassigned faces
        unclustered = self.db.execute(
            "SELECT id, embedding, thumbnail_path FROM faces WHERE person_id IS NULL"
        ).fetchall()

        if not unclustered:
            return 0

        # Phase 1: Find all candidate matches via embedding distance
        GEMMA_VERIFY_FLOOR = 0.3  # Below this = high confidence, skip Gemma
        candidates = []  # (face_id, person_id, min_dist, needs_gemma)
        for face_id, embedding_blob, thumb_path in unclustered:
            encoding = load_embedding(embedding_blob)
            distances = face_recognition.face_distance(
                known_encodings, encoding
            )
            best_idx = int(np.argmin(distances))
            min_dist = float(distances[best_idx])
            person_id = known_person_ids[best_idx]

            threshold = person_thresholds.get(person_id, FACE_MATCH_TOLERANCE)

            if min_dist <= threshold:
                needs_gemma = min_dist > GEMMA_VERIFY_FLOOR
                candidates.append((face_id, person_id, min_dist, needs_gemma, thumb_path))

        if not candidates:
            log.info("assign_new_faces: no candidate matches found")
            return 0

        # Phase 2: Verify borderline matches with Gemma
        borderline = [c for c in candidates if c[3]]
        gemma_url = None
        verified = set()  # face_ids that passed Gemma verification
        rejected = set()  # face_ids that failed

        if borderline and orchestrator:
            gemma_url = orchestrator.request_gemma(timeout=120)
            if gemma_url:
                log.info("assign_new_faces: verifying %d borderline matches with Gemma" % len(borderline))
                for face_id, person_id, dist, _, thumb_path in borderline:
                    ref_thumb = person_ref_thumbs.get(person_id)
                    if ref_thumb and thumb_path:
                        match = verify_face_match(ref_thumb, thumb_path, llm_server=gemma_url)
                        if match:
                            verified.add(face_id)
                            log.info("  Gemma verified YES: face %s → %s (dist=%.3f)"
                                     % (face_id[:8], person_id[:8], dist))
                        else:
                            rejected.add(face_id)
                            log.info("  Gemma verified NO:  face %s → %s (dist=%.3f)"
                                     % (face_id[:8], person_id[:8], dist))
                    else:
                        # No thumbnails — fall back to embedding-only
                        verified.add(face_id)
            else:
                log.warning("assign_new_faces: could not get Gemma — skipping verification, using embedding only")

        # Phase 3: Assign faces
        assigned = 0
        for face_id, person_id, min_dist, needs_gemma, _ in candidates:
            if needs_gemma and face_id in rejected:
                continue  # Gemma said no
            if needs_gemma and gemma_url and face_id not in verified:
                continue  # Gemma was available but didn't verify this face

            self.db.execute(
                "UPDATE faces SET person_id = ? WHERE id = ?",
                (person_id, face_id)
            )
            assigned += 1

        if assigned:
            self.db.commit()
            self._update_face_names()

        log.info("Assigned %d new faces to known persons (%d high-confidence, %d Gemma-verified, %d rejected)"
                 % (assigned, len([c for c in candidates if not c[3]]),
                    len(verified - rejected), len(rejected)))
        return assigned

    def deduplicate_faces(self, db=None):
        """Remove duplicate faces per file — keep one face per person per file.

        Two passes:
        1. Same cluster + same file: keep largest bbox (cheap, no embedding comparison)
        2. Same file, different clusters but similar embeddings (distance < 0.4):
           keep the largest bbox, merge the duplicate into the keeper's cluster

        Returns count of faces removed.
        """
        if db is None:
            db = self.db

        removed = 0

        # Pass 1: Same cluster + same file (fast — no embedding comparison needed)
        dupes = db.execute("""
            SELECT file_id, cluster_id, COUNT(*) as cnt
            FROM faces
            WHERE cluster_id IS NOT NULL
            GROUP BY file_id, cluster_id
            HAVING cnt > 1
        """).fetchall()

        for file_id, cluster_id, count in dupes:
            faces = db.execute("""
                SELECT id, thumbnail_path,
                       (bbox_right - bbox_left) * (bbox_bottom - bbox_top) as area
                FROM faces
                WHERE file_id = ? AND cluster_id = ?
                ORDER BY area DESC
            """, (file_id, cluster_id)).fetchall()

            for face_id, thumb_path, _ in faces[1:]:
                db.execute("DELETE FROM faces WHERE id = ?", (face_id,))
                if thumb_path:
                    try:
                        os.unlink(thumb_path)
                    except OSError:
                        pass
                removed += 1

        # Pass 2: Same file, different clusters but similar embeddings
        files_with_many = db.execute("""
            SELECT file_id, COUNT(*) as cnt
            FROM faces
            GROUP BY file_id
            HAVING cnt > 1
        """).fetchall()

        for file_id, _ in files_with_many:
            faces = db.execute("""
                SELECT id, embedding, cluster_id,
                       (bbox_right - bbox_left) * (bbox_bottom - bbox_top) as area
                FROM faces
                WHERE file_id = ?
                ORDER BY area DESC
            """, (file_id,)).fetchall()

            if len(faces) < 2:
                continue

            # Compare each pair — mark smaller ones for deletion
            keep_ids = set()
            delete_ids = set()
            for i in range(len(faces)):
                if faces[i][0] in delete_ids:
                    continue
                enc_i = load_embedding(faces[i][1])
                for j in range(i + 1, len(faces)):
                    if faces[j][0] in delete_ids:
                        continue
                    enc_j = load_embedding(faces[j][1])
                    dist = float(np.linalg.norm(enc_i - enc_j))
                    if dist < 0.4:
                        # Same person — delete the smaller one (j, since sorted by area desc)
                        delete_ids.add(faces[j][0])

            for face_id in delete_ids:
                thumb = db.execute("SELECT thumbnail_path FROM faces WHERE id=?", (face_id,)).fetchone()
                db.execute("DELETE FROM faces WHERE id = ?", (face_id,))
                if thumb and thumb[0]:
                    try:
                        os.unlink(thumb[0])
                    except OSError:
                        pass
                removed += 1

        # Pass 3: Within-cluster dedup — remove near-identical faces across files
        # For large clusters (e.g. 1000+ faces of the same person), many are
        # visually identical from different video keyframes. Keep one representative
        # face per visually-distinct appearance (distance < 0.3 = near-identical).
        clusters_with_many = db.execute("""
            SELECT cluster_id, COUNT(*) as cnt
            FROM faces
            WHERE cluster_id IS NOT NULL
            GROUP BY cluster_id
            HAVING cnt > 5
        """).fetchall()

        for cluster_id, count in clusters_with_many:
            faces = db.execute("""
                SELECT id, embedding,
                       (bbox_right - bbox_left) * (bbox_bottom - bbox_top) as area
                FROM faces
                WHERE cluster_id = ?
                ORDER BY area DESC
            """, (cluster_id,)).fetchall()

            if len(faces) < 2:
                continue

            # Greedy dedup: iterate sorted by area (best first), skip any face
            # that's within 0.3 distance of an already-kept face
            kept_encodings = []
            kept_ids = set()
            delete_ids = []

            for face_id, emb_blob, area in faces:
                enc = load_embedding(emb_blob)
                is_dup = False
                for kept_enc in kept_encodings:
                    if float(np.linalg.norm(enc - kept_enc)) < 0.3:
                        is_dup = True
                        break
                if is_dup:
                    delete_ids.append(face_id)
                else:
                    kept_encodings.append(enc)
                    kept_ids.add(face_id)

            for face_id in delete_ids:
                thumb = db.execute("SELECT thumbnail_path FROM faces WHERE id=?", (face_id,)).fetchone()
                db.execute("DELETE FROM faces WHERE id = ?", (face_id,))
                if thumb and thumb[0]:
                    try:
                        os.unlink(thumb[0])
                    except OSError:
                        pass
                removed += 1

            if delete_ids:
                log.info("deduplicate_faces: cluster %d — kept %d, removed %d"
                         % (cluster_id, len(kept_ids), len(delete_ids)))

        if removed:
            db.commit()
            # Update person face counts
            db.execute("""
                UPDATE persons SET face_count = (
                    SELECT COUNT(*) FROM faces WHERE person_id = persons.id
                )
            """)
            db.commit()

        log.info("deduplicate_faces: removed %d duplicate faces total" % removed)
        return removed

    def update_person_threshold(self, person_id, db=None):
        """Compute and store the adaptive match threshold for a person.

        Uses the 95th percentile of pairwise distances between the person's
        confirmed faces. This threshold is used by assign_new_faces() to
        decide if an unknown face matches this person.
        """
        if db is None:
            db = self.db

        rows = db.execute(
            "SELECT embedding FROM faces WHERE person_id = ?", (person_id,)
        ).fetchall()

        if len(rows) < 2:
            # Not enough faces to compute a meaningful threshold
            return

        embeddings = [load_embedding(r[0]) for r in rows]

        # Compute pairwise distances (sample if too many faces)
        if len(embeddings) > 200:
            import random
            sample = random.sample(embeddings, 200)
        else:
            sample = embeddings

        distances = []
        for i in range(len(sample)):
            for j in range(i + 1, len(sample)):
                dist = float(np.linalg.norm(sample[i] - sample[j]))
                distances.append(dist)

        if not distances:
            return

        # Use 95th percentile as the threshold — allows for variation but
        # excludes extreme outliers (which may be mislabeled faces)
        distances.sort()
        idx_95 = int(len(distances) * 0.95)
        threshold = distances[min(idx_95, len(distances) - 1)]

        # Clamp to reasonable range [0.3, 0.8]
        threshold = max(0.3, min(0.8, threshold))

        db.execute(
            "UPDATE persons SET match_threshold = ? WHERE id = ?",
            (round(threshold, 4), person_id)
        )
        db.commit()
        name = db.execute("SELECT name FROM persons WHERE id = ?", (person_id,)).fetchone()
        log.info("Updated match threshold for %s: %.4f (from %d faces)"
                 % (name[0] if name else person_id, threshold, len(rows)))

    def audit_person_faces(self, person_id, llm_server=None):
        """Verify all faces for a named person using Gemma vision.

        Compares each face against the person's best reference thumbnail.
        Faces that Gemma rejects are removed from the person and put back
        in the unnamed pool. Returns count of faces rejected.

        Uses short-lived DB connections to avoid locking the database during
        the slow Gemma verification calls.
        """
        if llm_server is None:
            llm_server = PRO580X_GEMMA

        # Quick read — get all face data, then release DB
        adb = sqlite3.connect(str(DB_PATH), timeout=10)
        adb.execute("PRAGMA journal_mode=WAL")
        adb.execute("PRAGMA busy_timeout=10000")

        faces = adb.execute("""
            SELECT id, thumbnail_path,
                   (bbox_right - bbox_left) * (bbox_bottom - bbox_top) as area
            FROM faces
            WHERE person_id = ?
            ORDER BY area DESC
        """, (person_id,)).fetchall()

        if len(faces) < 2:
            adb.close()
            return 0

        ref_id, ref_thumb, _ = faces[0]
        if not ref_thumb or not os.path.exists(ref_thumb):
            adb.close()
            log.warning("audit_person_faces: no reference thumbnail for person %s" % person_id)
            return 0

        name = adb.execute("SELECT name FROM persons WHERE id=?", (person_id,)).fetchone()
        person_name = name[0] if name else person_id[:8]

        min_row = adb.execute("SELECT MIN(cluster_id) FROM faces").fetchone()
        next_cluster = min(min_row[0] or 0, 0) - 1
        adb.close()  # Release DB before slow Gemma calls

        log.info("Auditing %s (%d faces)..." % (person_name, len(faces)))

        rejected = 0
        for face_id, thumb_path, _ in faces[1:]:  # Skip reference face
            if not thumb_path or not os.path.exists(thumb_path):
                continue

            # Slow Gemma call — no DB held
            match = verify_face_match(ref_thumb, thumb_path, llm_server=llm_server)

            if not match:
                # Quick DB write — open, update, commit, close
                wdb = sqlite3.connect(str(DB_PATH), timeout=10)
                wdb.execute("PRAGMA busy_timeout=10000")
                wdb.execute(
                    "UPDATE faces SET person_id=NULL, cluster_id=? WHERE id=?",
                    (next_cluster, face_id)
                )
                wdb.commit()
                wdb.close()
                next_cluster -= 1
                rejected += 1
                log.info("  Audit %s: rejected face %s" % (person_name, face_id[:8]))

        if rejected:
            # Update person face count
            wdb = sqlite3.connect(str(self.db_path), timeout=10)
            wdb.execute("PRAGMA busy_timeout=10000")
            remaining = wdb.execute(
                "SELECT COUNT(*) FROM faces WHERE person_id=?", (person_id,)
            ).fetchone()[0]
            wdb.execute(
                "UPDATE persons SET face_count=? WHERE id=?", (remaining, person_id)
            )
            wdb.commit()
            wdb.close()

        log.info("Audit %s: %d/%d faces rejected" % (person_name, rejected, len(faces) - 1))
        return rejected

    def audit_all_persons(self, orchestrator=None):
        """Audit all named persons using Gemma verification.

        Returns dict with results per person and totals.
        """
        persons = self.db.execute(
            "SELECT id, name, face_count FROM persons ORDER BY face_count DESC"
        ).fetchall()

        if not persons:
            return {"persons_audited": 0, "faces_rejected": 0, "details": []}

        # Request Gemma from orchestrator
        gemma_url = None
        if orchestrator:
            gemma_url = orchestrator.request_gemma(timeout=120)
        if not gemma_url:
            gemma_url = PRO580X_GEMMA

        total_rejected = 0
        details = []
        for person_id, name, face_count in persons:
            if face_count < 2:
                continue
            rejected = self.audit_person_faces(person_id, llm_server=gemma_url)
            total_rejected += rejected
            if rejected > 0:
                details.append({
                    "name": name,
                    "original_count": face_count,
                    "rejected": rejected,
                    "remaining": face_count - rejected,
                })

        self._update_face_names()
        log.info("Audit complete: %d persons audited, %d faces rejected total"
                 % (len(persons), total_rejected))

        return {
            "persons_audited": len(persons),
            "faces_rejected": total_rejected,
            "details": details,
        }

    def name_cluster(self, cluster_id, name):
        """Assign a person name to all faces in a cluster.

        Creates the person record if it doesn't exist.
        Returns the person_id.
        """
        name = name.strip()
        pid = hashlib.sha256(name.lower().encode()).hexdigest()[:16]

        # Create or get person
        self.db.execute("""
            INSERT OR IGNORE INTO persons (id, name, created_at)
            VALUES (?, ?, ?)
        """, (pid, name, datetime.now().isoformat()))

        # Assign all faces in this cluster
        self.db.execute("""
            UPDATE faces SET person_id = ? WHERE cluster_id = ?
        """, (pid, cluster_id))

        # Update person's face count
        count = self.db.execute(
            "SELECT COUNT(*) FROM faces WHERE person_id = ?", (pid,)
        ).fetchone()[0]
        self.db.execute(
            "UPDATE persons SET face_count = ? WHERE id = ?", (count, pid)
        )

        self.db.commit()
        self._update_face_names()
        self.update_person_threshold(pid)
        log.info("Named cluster %d as '%s' (%d faces)" % (cluster_id, name, count))
        return pid

    def merge_clusters(self, source_cluster_id, target_cluster_id):
        """Merge source cluster into target cluster.

        All faces from source get target's cluster_id and person_id (if named).
        """
        # Get target's person_id (if named)
        target_person = self.db.execute(
            "SELECT person_id FROM faces WHERE cluster_id = ? AND person_id IS NOT NULL LIMIT 1",
            (target_cluster_id,)
        ).fetchone()

        person_id = target_person[0] if target_person else None

        if person_id:
            self.db.execute("""
                UPDATE faces SET cluster_id = ?, person_id = ? WHERE cluster_id = ?
            """, (target_cluster_id, person_id, source_cluster_id))
        else:
            self.db.execute("""
                UPDATE faces SET cluster_id = ? WHERE cluster_id = ?
            """, (target_cluster_id, source_cluster_id))

        self.db.commit()
        self._update_face_names()
        if person_id:
            self.update_person_threshold(person_id)
        log.info("Merged cluster %d into cluster %d" % (source_cluster_id, target_cluster_id))

    def rename_person(self, person_id, new_name):
        """Rename a person."""
        new_name = new_name.strip()
        self.db.execute(
            "UPDATE persons SET name = ? WHERE id = ?", (new_name, person_id)
        )
        self.db.commit()
        self._update_face_names()

    def unname_cluster(self, cluster_id):
        """Remove person assignment from all faces in a cluster."""
        self.db.execute(
            "UPDATE faces SET person_id = NULL WHERE cluster_id = ?",
            (cluster_id,)
        )
        self.db.commit()
        self._update_face_names()

    def ignore_cluster(self, cluster_id):
        """Hide a cluster from the active view."""
        self.db.execute(
            "INSERT OR IGNORE INTO ignored_clusters (cluster_id) VALUES (?)",
            (cluster_id,)
        )
        self.db.commit()

    def unignore_cluster(self, cluster_id):
        """Restore a cluster to the active view."""
        self.db.execute(
            "DELETE FROM ignored_clusters WHERE cluster_id = ?",
            (cluster_id,)
        )
        self.db.commit()

    def _update_face_names(self):
        """Rebuild files.face_names from faces+persons tables.

        This is what makes 'Jon red shirt' work in search:
        files.face_names gets set to 'Jon Smith, Sarah Lee' etc,
        and FTS includes face_names, so search just works.
        """
        # Get all files that have faces with person assignments
        rows = self.db.execute("""
            SELECT f.file_id, GROUP_CONCAT(DISTINCT p.name)
            FROM faces f
            JOIN persons p ON f.person_id = p.id
            GROUP BY f.file_id
        """).fetchall()

        # Update face_names for files that have named faces
        for fid, names in rows:
            self.db.execute(
                "UPDATE files SET face_names = ? WHERE id = ?",
                (names, fid)
            )

        # Clear face_names for files that no longer have named faces
        self.db.execute("""
            UPDATE files SET face_names = NULL
            WHERE face_names IS NOT NULL
            AND id NOT IN (
                SELECT DISTINCT file_id FROM faces WHERE person_id IS NOT NULL
            )
        """)

        self.db.commit()

    def _update_person_counts(self):
        """Refresh all person face_count values."""
        self.db.execute("""
            UPDATE persons SET face_count = (
                SELECT COUNT(*) FROM faces WHERE faces.person_id = persons.id
            )
        """)
        self.db.commit()

    def mark_face_scanned(self, file_id, db=None):
        """Mark a file as scanned for faces (even if 0 faces found)."""
        target_db = db or self.db
        target_db.execute("""
            INSERT OR IGNORE INTO face_scanned_files (file_id, scanned_at)
            VALUES (?, ?)
        """, (file_id, datetime.now().isoformat()))

    def get_face_status(self):
        """Get face recognition statistics."""
        total = self.db.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        clustered = self.db.execute(
            "SELECT COUNT(*) FROM faces WHERE cluster_id IS NOT NULL"
        ).fetchone()[0]
        named = self.db.execute(
            "SELECT COUNT(*) FROM faces WHERE person_id IS NOT NULL"
        ).fetchone()[0]
        persons_count = self.db.execute(
            "SELECT COUNT(*) FROM persons"
        ).fetchone()[0]
        # Exclude ignored clusters from unnamed count
        unnamed_clusters = self.db.execute("""
            SELECT COUNT(DISTINCT cluster_id) FROM faces
            WHERE cluster_id IS NOT NULL AND person_id IS NULL
            AND cluster_id NOT IN (SELECT cluster_id FROM ignored_clusters)
        """).fetchone()[0]
        files_with_faces = self.db.execute(
            "SELECT COUNT(DISTINCT file_id) FROM faces"
        ).fetchone()[0]

        # Files that haven't been scanned for faces yet
        # Uses face_scanned_files table so files with 0 faces count as scanned
        files_without_scan = self.db.execute("""
            SELECT COUNT(*) FROM files
            WHERE status = 'indexed' AND file_type IN ('image', 'video')
            AND id NOT IN (SELECT file_id FROM face_scanned_files)
        """).fetchone()[0]

        return {
            "total_faces": total,
            "clustered_faces": clustered,
            "named_faces": named,
            "unnamed_clusters": unnamed_clusters,
            "named_persons": persons_count,
            "files_with_faces": files_with_faces,
            "files_without_face_scan": files_without_scan,
            "face_recognition_available": HAS_FACE_RECOGNITION
        }

    def get_face_clusters(self, show="active"):
        """Get face clusters with sample thumbnails for the management UI.

        show: "active" (default, excludes ignored), "ignored" (only ignored), "all" (everything)
        """
        clusters = []

        if show == "ignored":
            where_clause = "AND f.cluster_id IN (SELECT cluster_id FROM ignored_clusters)"
        elif show == "all":
            where_clause = ""
        else:  # "active"
            where_clause = "AND f.cluster_id NOT IN (SELECT cluster_id FROM ignored_clusters)"

        rows = self.db.execute("""
            SELECT
                f.cluster_id,
                f.person_id,
                p.name as person_name,
                COUNT(*) as face_count
            FROM faces f
            LEFT JOIN persons p ON f.person_id = p.id
            WHERE f.cluster_id IS NOT NULL
            %s
            GROUP BY f.cluster_id
            ORDER BY face_count DESC
        """ % where_clause).fetchall()

        for cluster_id, person_id, person_name, face_count in rows:
            # Get up to 4 sample face thumbnails for this cluster
            samples = self.db.execute("""
                SELECT id, thumbnail_path FROM faces
                WHERE cluster_id = ?
                ORDER BY created_at
                LIMIT 4
            """, (cluster_id,)).fetchall()

            sample_faces = []
            for fid, thumb_path in samples:
                sample_faces.append({
                    "face_id": fid,
                    "thumbnail": "/faces/thumbnail?id=" + fid
                })

            clusters.append({
                "cluster_id": cluster_id,
                "person_id": person_id,
                "person_name": person_name,
                "face_count": face_count,
                "sample_faces": sample_faces
            })

        return clusters

    def get_face_thumbnail(self, face_id):
        """Get a face crop thumbnail. Returns (jpeg_bytes, mime) or (None, None)."""
        row = self.db.execute(
            "SELECT thumbnail_path FROM faces WHERE id = ?", (face_id,)
        ).fetchone()
        if not row or not row[0]:
            return None, None

        thumb_path = Path(row[0])
        if thumb_path.exists():
            return thumb_path.read_bytes(), "image/jpeg"
        return None, None

    def get_persons(self):
        """Get all named persons."""
        rows = self.db.execute(
            "SELECT id, name, face_count FROM persons ORDER BY name"
        ).fetchall()
        return [{"id": r[0], "name": r[1], "face_count": r[2]} for r in rows]

    def shutdown(self):
        """Graceful shutdown."""
        self.running = False
        log.info("Shutting down...")

# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------

def print_status(indexer):
    status = indexer.get_status()
    counts = status["counts"]
    print("\n=== Media Indexer Status ===")
    print(f"  Indexed:  {counts.get('indexed', 0)}")
    print(f"  Pending:  {counts.get('pending', 0)}")
    print(f"  Errors:   {counts.get('error', 0)}")
    print(f"  Offline:  {counts.get('offline', 0)}")
    print(f"\n  Folders:")
    for path, count, last_scan in status["folders"]:
        print(f"    {path} ({count} files, last scan: {last_scan or 'never'})")
    print()


def main():
    setup_logging()

    if len(sys.argv) < 2:
        print("Usage:")
        print(f"  {sys.argv[0]} index <folder> [<folder>...]  — Index folders")
        print(f"  {sys.argv[0]} search <query>                — Search the database")
        print(f"  {sys.argv[0]} status                        — Show indexing status")
        print(f"  {sys.argv[0]} watch <folder> [<folder>...]  — Index + watch for changes")
        print(f"  {sys.argv[0]} serve [port]                  — Run HTTP search API (default: 8081)")
        print(f"  {sys.argv[0]} transcribe                    — Transcribe all video/audio without transcripts")
        print(f"  {sys.argv[0]} reembed                       — Embed all descriptions into ChromaDB")
        print(f"  {sys.argv[0]} faces detect                  — Detect faces in indexed files")
        print(f"  {sys.argv[0]} faces cluster [tolerance]     — Cluster detected faces")
        print(f"  {sys.argv[0]} faces assign                  — Match new faces to known persons")
        print(f"  {sys.argv[0]} faces name <id> <name>        — Name a face cluster")
        print(f"  {sys.argv[0]} faces merge <src> <dst>        — Merge two clusters")
        print(f"  {sys.argv[0]} faces persons                 — List named persons")
        print(f"  {sys.argv[0]} faces status                  — Face detection stats")
        print(f"  {sys.argv[0]} faces reset                   — Clear all face data")
        sys.exit(1)

    command = sys.argv[1]
    indexer = MediaIndexer()

    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        indexer.shutdown()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if command == "index":
        if len(sys.argv) < 3:
            print("Specify one or more folders to index")
            sys.exit(1)

        folders = sys.argv[2:]
        for folder in folders:
            indexer.add_folder(folder)
            new_files = indexer.scan_folder(folder)
            # Process each folder immediately after scanning so GPUs start
            # working while remaining folders are still being walked over SMB.
            if new_files > 0:
                indexer.process_pending()

        # Final pass picks up any pending files left from a previous crashed run.
        stats = indexer.process_pending()
        print(f"\nDone! Indexed: {stats['indexed']}, "
              f"Errors: {stats['errors']}, Skipped: {stats['skipped']}")
        print_status(indexer)

    elif command == "search":
        if len(sys.argv) < 3:
            print("Specify a search query")
            sys.exit(1)

        query = " ".join(sys.argv[2:])
        results = indexer.search(query)

        if not results:
            print(f"No results for: {query}")
        else:
            print(f"\n=== {len(results)} results for: {query} ===\n")
            for r in results:
                print(f"  [{r['type']}] {r['filename']}")
                if r['description']:
                    # Truncate long descriptions
                    desc = r['description'][:200]
                    print(f"    {desc}")
                if r['duration']:
                    mins = int(r['duration'] // 60)
                    secs = int(r['duration'] % 60)
                    print(f"    Duration: {mins}:{secs:02d}")
                if r['width'] and r['height']:
                    print(f"    Resolution: {r['width']}x{r['height']}")
                print(f"    Path: {r['path']}")
                print()

    elif command == "status":
        print_status(indexer)

    elif command == "watch":
        if len(sys.argv) < 3:
            print("Specify one or more folders to watch")
            sys.exit(1)

        folders = sys.argv[2:]
        for folder in folders:
            indexer.add_folder(folder)

        log.info("=" * 60)
        log.info("Perpetual Task Pipeline starting")
        log.info("Folders: %s" % ", ".join(folders))
        log.info("Enabled task types: %s" % ", ".join(sorted(ENABLED_TASK_TYPES)))
        log.info("Crawl interval: %ds" % RESCAN_INTERVAL)
        log.info("=" * 60)

        # Background thread: keeps scanner-state.json fresh so the
        # serve process (separate PID) always sees current state.
        def _state_writer():
            while indexer.running:
                write_scanner_state(indexer.scanner)
                time.sleep(15)
        threading.Thread(target=_state_writer, daemon=True).start()

        # ── Crawler ────────────────────────────────────────────────────
        crawler = CrawlerWorker(DB_PATH, folders, interval=RESCAN_INTERVAL)
        crawler.start()

        # ── Pro 580X Orchestrator (model-swapping GPU) ────────────────
        orchestrator = Pro580XOrchestrator(DB_PATH)
        orchestrator.start()  # Loads Gemma by default

        # ── Whisper worker (uses orchestrator for model swaps) ────────
        if "transcribe" in ENABLED_TASK_TYPES:
            whisper_worker = PerpetualWhisperWorker(DB_PATH, lambda: indexer.running, orchestrator)
            whisper_worker.start()
            log.info("PerpetualWhisperWorker: online (orchestrator-managed Whisper)")

        # ── Pro 580X API Gemma worker (API visual_analysis only) ──────
        pro580x_gemma = Pro580XGemmaWorker(DB_PATH, orchestrator, lambda: indexer.running)
        pro580x_gemma.start()
        log.info("Pro580XGemmaWorker: online (API visual_analysis on Pro 580X)")

        # ── Scene workers (Phase 2) ─────────────────────────────────────
        if "scene_detect" in ENABLED_TASK_TYPES:
            for vaapi_dev in VAAPI_DEVICES:
                scene_worker = SceneWorker(DB_PATH, vaapi_dev, lambda: indexer.running)
                scene_worker.start()
                log.info("SceneWorker: online (%s)" % vaapi_dev)

        # ── Gemma workers (Phase 3 — visual analysis) ───────────────────
        if "visual_analysis" in ENABLED_TASK_TYPES:
            for llm_server in LLM_SERVERS:
                try:
                    req = urllib.request.Request("%s/health" % llm_server)
                    resp = urllib.request.urlopen(req, timeout=5)
                    ok = json.loads(resp.read()).get("status") == "ok"
                except Exception:
                    ok = False
                if ok:
                    gemma_worker = GemmaWorker(DB_PATH, llm_server, lambda: indexer.running)
                    gemma_worker.start()
                    log.info("GemmaWorker: online (%s)" % llm_server)
                else:
                    log.warning("LLM server not reachable (%s) — visual analysis disabled for this GPU" % llm_server)

        # ── Face worker (Phase 4 — face detection) ──────────────────────────
        if "face_detect" in ENABLED_TASK_TYPES:
            if HAS_FACE_RECOGNITION:
                face_worker = FaceWorker(DB_PATH, lambda: indexer.running)
                face_worker.start()
                log.info("FaceWorker: online (CPU-based dlib face detection)")
            else:
                log.warning("face_recognition not installed — face detection disabled")
                log.warning("Install with: pip3 install face_recognition")

        # ── ALA worker (alignment — API-only, no crawler tasks) ────────
        ala_worker = ALAWorker(DB_PATH, lambda: indexer.running)
        ala_worker.start()
        log.info("ALAWorker: online (forwarding to localhost:8085)")

        # ── Task Coordinator ───────────────────────────────────────────
        coordinator = TaskCoordinator(DB_PATH, lambda: indexer.running)
        coordinator.start()

        # ── Main thread: keep alive, update scanner state ───────────────
        log.info("Perpetual Task Pipeline running. Ctrl+C or SIGTERM to stop.")
        while indexer.running:
            indexer.scanner["state"] = "running"
            write_scanner_state(indexer.scanner)
            time.sleep(10)

        orchestrator.shutdown()
        log.info("Perpetual Task Pipeline stopped.")

    elif command == "transcribe":
        # Backfill transcripts for existing indexed video/audio files
        indexer.db.execute("PRAGMA busy_timeout = 120000")  # 2 min — watcher holds locks during scans
        rows = indexer.db.execute("""
            SELECT id, path, file_type FROM files
            WHERE file_type IN ('video', 'audio')
              AND status = 'indexed'
              AND (transcript IS NULL OR transcript = '')
            ORDER BY indexed_at DESC
        """).fetchall()

        print("Found %d files without transcripts" % len(rows))
        done = 0
        for fid, fpath, ftype in rows:
            filepath = Path(fpath)
            if not filepath.exists():
                continue
            audio_tmp = DATA_DIR / ("tmp_audio_%s.wav" % fid)
            try:
                TRANSCRIBE_HEARTBEAT.touch()  # signal to serve API that we're active
                print("  [%d/%d] %s" % (done + 1, len(rows), filepath.name), end="", flush=True)
                if extract_audio_for_transcription(str(filepath), str(audio_tmp)):
                    TRANSCRIBE_HEARTBEAT.touch()  # still active during extraction
                    chunks = split_audio_into_chunks(audio_tmp)
                    chunk_texts = []
                    for i, (chunk_path, start_secs) in enumerate(chunks):
                        TRANSCRIBE_HEARTBEAT.touch()
                        text = transcribe_audio(str(chunk_path), timeout=WHISPER_TIMEOUT)
                        if text:
                            chunk_texts.append(text)
                        if chunk_path != audio_tmp and chunk_path.exists():
                            try:
                                chunk_path.unlink()
                            except Exception:
                                pass
                    transcript = " ".join(chunk_texts).strip() or None
                    if transcript:
                        indexer.db.execute(
                            "UPDATE files SET transcript = ? WHERE id = ?", (transcript, fid)
                        )
                        indexer.db.commit()
                        print(" — %d chars (%d chunks)" % (len(transcript), len(chunks)))
                        done += 1
                    else:
                        print(" — (empty)")
                else:
                    print(" — (audio extraction failed)")
            except Exception as e:
                print(" — ERROR: %s" % e)
            finally:
                if audio_tmp.exists():
                    audio_tmp.unlink()

        print("\nDone: %d/%d files transcribed" % (done, len(rows)))
        if TRANSCRIBE_HEARTBEAT.exists():
            TRANSCRIBE_HEARTBEAT.unlink()

    elif command == "reembed":
        if not HAS_CHROMADB:
            print("ChromaDB not installed. Run: pip3 install chromadb")
            sys.exit(1)
        count = indexer.reembed_all()
        print("Embedded %d descriptions into ChromaDB." % count)

    elif command == "faces":
        if not HAS_FACE_RECOGNITION:
            print("face_recognition not installed. Run on the Mac Pro:")
            print("  brew install cmake boost eigen")
            print("  pip3 install face_recognition")
            sys.exit(1)

        subcommand = sys.argv[2] if len(sys.argv) > 2 else "status"

        if subcommand == "detect":
            count = indexer.detect_all_faces()
            print("Scanned %d files for faces." % count)

        elif subcommand == "cluster":
            tolerance = float(sys.argv[3]) if len(sys.argv) > 3 else None
            clusters = indexer.cluster_faces(tolerance=tolerance)
            if clusters:
                print("\nFace Clusters:")
                for cid, face_ids in sorted(clusters.items(), key=lambda x: -len(x[1])):
                    print("  Cluster %d: %d faces" % (cid, len(face_ids)))
                print("\nUse 'faces name <cluster_id> <name>' to name a cluster.")
            else:
                print("No faces to cluster. Run 'faces detect' first.")

        elif subcommand == "assign":
            count = indexer.assign_new_faces()
            print("Assigned %d new faces to known persons." % count)

        elif subcommand == "name":
            if len(sys.argv) < 5:
                print("Usage: faces name <cluster_id> <name>")
                sys.exit(1)
            cluster_id = int(sys.argv[3])
            name = " ".join(sys.argv[4:])
            pid = indexer.name_cluster(cluster_id, name)
            print("Named cluster %d as '%s' (person_id: %s)" % (cluster_id, name, pid))

        elif subcommand == "merge":
            if len(sys.argv) < 5:
                print("Usage: faces merge <source_cluster_id> <target_cluster_id>")
                sys.exit(1)
            source = int(sys.argv[3])
            target = int(sys.argv[4])
            indexer.merge_clusters(source, target)
            print("Merged cluster %d into cluster %d." % (source, target))

        elif subcommand == "persons":
            persons = indexer.get_persons()
            if not persons:
                print("No named persons yet. Run 'faces cluster' then 'faces name'.")
            else:
                print("\nNamed Persons:")
                for p in persons:
                    print("  %s (%d faces)" % (p["name"], p["face_count"]))

        elif subcommand == "status":
            fs = indexer.get_face_status()
            print("\n=== Face Recognition Status ===")
            print("  Total faces detected:   %d" % fs["total_faces"])
            print("  Clustered:              %d" % fs["clustered_faces"])
            print("  Named:                  %d" % fs["named_faces"])
            print("  Named persons:          %d" % fs["named_persons"])
            print("  Unnamed clusters:       %d" % fs["unnamed_clusters"])
            print("  Files with faces:       %d" % fs["files_with_faces"])
            print("  Files not yet scanned:  %d" % fs["files_without_face_scan"])
            print()

        elif subcommand == "reset":
            confirm = input("This will delete ALL face data. Type 'yes' to confirm: ")
            if confirm.strip().lower() == "yes":
                indexer.db.executescript("""
                    DELETE FROM faces;
                    DELETE FROM persons;
                    UPDATE files SET face_names = NULL;
                """)
                print("All face data cleared.")
            else:
                print("Cancelled.")

        else:
            print("Unknown faces subcommand: %s" % subcommand)
            print("Available: detect, cluster, assign, name, merge, persons, status, reset")
            sys.exit(1)

    elif command == "serve":
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import urllib.parse as urlparse

        try:
            serve_port = int(sys.argv[2]) if len(sys.argv) > 2 else 8081
        except ValueError:
            print("Invalid port number: %s" % sys.argv[2])
            sys.exit(1)

        # Face management web UI (self-contained HTML page)
        FACE_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Face Management - Media Indexer</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: #1a1a2e; color: #e0e0e0; padding: 20px; }
h1 { color: #fff; margin-bottom: 5px; }
.subtitle { color: #888; margin-bottom: 20px; font-size: 14px; }
.stats-bar { display: flex; gap: 20px; margin-bottom: 25px; flex-wrap: wrap; }
.stat { background: #16213e; padding: 12px 18px; border-radius: 8px; }
.stat-num { font-size: 24px; font-weight: bold; color: #4fc3f7; }
.stat-label { font-size: 12px; color: #888; margin-top: 2px; }
.actions { margin-bottom: 25px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
button { background: #0a3d62; color: #fff; border: none; padding: 8px 16px;
  border-radius: 6px; cursor: pointer; font-size: 14px; }
button:hover { background: #1e5f8a; }
button:disabled { opacity: 0.4; cursor: not-allowed; }
button.danger { background: #8b0000; }
button.danger:hover { background: #b22222; }
.section-title { font-size: 18px; margin: 20px 0 12px; color: #ccc; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px; margin-bottom: 30px; }
.card { background: #16213e; border-radius: 8px; padding: 10px; text-align: center;
  cursor: pointer; border: 2px solid transparent; transition: border-color 0.2s; position: relative; }
.card:hover { border-color: #4fc3f7; }
.card.selected { border-color: #ff9800; }
.card.named { border-color: #2e7d32; }
.face-img { width: 100px; height: 100px; border-radius: 50%; object-fit: cover;
  background: #0a0a1a; display: block; margin: 0 auto 8px; }
.card-name { font-size: 14px; font-weight: bold; color: #fff; margin-bottom: 2px; }
.card-count { font-size: 12px; color: #888; }
.card-id { font-size: 10px; color: #555; }
.name-input { background: #0a0a1a; border: 1px solid #333; color: #fff; padding: 4px 8px;
  border-radius: 4px; width: 120px; font-size: 13px; }
.name-input:focus { outline: none; border-color: #4fc3f7; }
.tolerance-input { width: 60px; background: #0a0a1a; border: 1px solid #333; color: #fff;
  padding: 4px 8px; border-radius: 4px; font-size: 13px; }
.msg { padding: 10px 16px; border-radius: 6px; margin-bottom: 15px; display: none; }
.msg.ok { background: #1b5e20; display: block; }
.msg.err { background: #8b0000; display: block; }
.empty { color: #555; font-style: italic; padding: 20px; }
.samples { display: flex; gap: 4px; justify-content: center; margin-bottom: 6px; }
.samples img { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; }
.loading { text-align: center; padding: 40px; color: #555; }
</style>
</head>
<body>
<h1>Face Management</h1>
<p class="subtitle">Detect, cluster, and name faces in your media vault</p>

<div id="msg" class="msg"></div>

<div id="stats-bar" class="stats-bar"></div>

<div class="actions">
  <button onclick="recluster()">Re-cluster Faces</button>
  <label style="color:#888;font-size:13px;">Tolerance:</label>
  <input type="number" class="tolerance-input" id="tolerance" value="0.5" step="0.05" min="0.1" max="1.0">
  <button onclick="assignNew()">Auto-assign New Faces</button>
  <button id="mergeBtn" onclick="mergeSelected()" disabled>Merge Selected (0)</button>
</div>

<div id="content"><div class="loading">Loading faces...</div></div>

<script>
const API = window.location.origin;
let selected = [];

async function api(path, method, body) {
  const opts = {method: method || 'GET', headers: {'Content-Type': 'application/json'}};
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(API + path, opts);
  return r.json();
}

function showMsg(text, ok) {
  const el = document.getElementById('msg');
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  setTimeout(() => { el.className = 'msg'; }, 4000);
}

function toggleSelect(clusterId) {
  const idx = selected.indexOf(clusterId);
  if (idx >= 0) selected.splice(idx, 1);
  else if (selected.length < 2) selected.push(clusterId);
  else { selected.shift(); selected.push(clusterId); }
  updateSelection();
}

function updateSelection() {
  document.querySelectorAll('.card').forEach(c => {
    c.classList.toggle('selected', selected.includes(parseInt(c.dataset.cluster)));
  });
  const btn = document.getElementById('mergeBtn');
  btn.textContent = 'Merge Selected (' + selected.length + ')';
  btn.disabled = selected.length < 2;
}

async function nameCluster(clusterId) {
  const input = document.getElementById('name-' + clusterId);
  const name = input.value.trim();
  if (!name) return;
  const r = await api('/faces/name', 'POST', {cluster_id: clusterId, name: name});
  if (r.ok) { showMsg('Named cluster ' + clusterId + ' as "' + name + '"', true); loadAll(); }
  else showMsg(r.error || 'Failed', false);
}

async function recluster() {
  const tol = parseFloat(document.getElementById('tolerance').value) || 0.5;
  showMsg('Clustering faces (tolerance=' + tol + ')...', true);
  const r = await api('/faces/cluster', 'POST', {tolerance: tol});
  if (r.ok) { showMsg('Found ' + r.cluster_count + ' clusters from ' + r.face_count + ' faces', true); selected = []; loadAll(); }
  else showMsg(r.error || 'Failed', false);
}

async function assignNew() {
  const r = await api('/faces/assign', 'POST', {});
  if (r.ok) { showMsg('Assigned ' + r.assigned + ' new faces to known persons', true); loadAll(); }
  else showMsg(r.error || 'Failed', false);
}

async function mergeSelected() {
  if (selected.length < 2) return;
  const r = await api('/faces/merge', 'POST', {source_cluster_id: selected[0], target_cluster_id: selected[1]});
  if (r.ok) { showMsg('Merged cluster ' + selected[0] + ' into ' + selected[1], true); selected = []; loadAll(); }
  else showMsg(r.error || 'Failed', false);
}

function handleNameKeydown(e, clusterId) {
  if (e.key === 'Enter') nameCluster(clusterId);
}

async function renamePerson(personId, clusterId) {
  const input = document.getElementById('rename-' + clusterId);
  const name = input.value.trim();
  if (!name) return;
  const r = await api('/faces/rename', 'POST', {person_id: personId, name: name});
  if (r.ok) { showMsg('Renamed to "' + name + '"', true); loadAll(); }
  else showMsg(r.error || 'Failed', false);
}

function handleRenameKeydown(e, personId, clusterId) {
  if (e.key === 'Enter') renamePerson(personId, clusterId);
}

async function loadAll() {
  const [statusData, clusterData] = await Promise.all([
    api('/faces/status'),
    api('/faces/clusters')
  ]);

  // Stats bar
  const sb = document.getElementById('stats-bar');
  sb.innerHTML = [
    {n: statusData.total_faces, l: 'Total Faces'},
    {n: statusData.named_faces, l: 'Named'},
    {n: statusData.unnamed_clusters, l: 'Unnamed Clusters'},
    {n: statusData.named_persons, l: 'Persons'},
    {n: statusData.files_with_faces, l: 'Files w/ Faces'},
    {n: statusData.files_without_face_scan, l: 'Not Scanned'}
  ].map(s => '<div class="stat"><div class="stat-num">' + s.n + '</div><div class="stat-label">' + s.l + '</div></div>').join('');

  const clusters = clusterData.clusters || [];
  const named = clusters.filter(c => c.person_name);
  const unnamed = clusters.filter(c => !c.person_name);

  let html = '';

  // Named persons section
  html += '<div class="section-title">Named Persons (' + named.length + ')</div>';
  if (named.length === 0) {
    html += '<div class="empty">No named persons yet. Cluster faces first, then name them.</div>';
  } else {
    html += '<div class="grid">';
    for (const c of named) {
      const img = c.sample_faces.length > 0 ? '<img class="face-img" src="' + c.sample_faces[0].thumbnail + '">' : '<div class="face-img"></div>';
      html += '<div class="card named" data-cluster="' + c.cluster_id + '" onclick="toggleSelect(' + c.cluster_id + ')">';
      html += img;
      html += '<div class="card-name">' + escHtml(c.person_name) + '</div>';
      html += '<div style="margin:6px 0">';
      html += '<input class="name-input" id="rename-' + c.cluster_id + '" placeholder="Rename..." value="" onkeydown="handleRenameKeydown(event,\'' + c.person_id + '\',' + c.cluster_id + ')" onclick="event.stopPropagation()">';
      html += '</div>';
      html += '<button onclick="event.stopPropagation();renamePerson(\'' + c.person_id + '\',' + c.cluster_id + ')" style="font-size:12px;padding:4px 10px">Rename</button>';
      html += '<div class="card-count" style="margin-top:6px">' + c.face_count + ' faces</div>';
      html += '<div class="card-id">Cluster ' + c.cluster_id + '</div>';
      html += '</div>';
    }
    html += '</div>';
  }

  // Unnamed clusters section
  html += '<div class="section-title">Unnamed Clusters (' + unnamed.length + ')</div>';
  if (unnamed.length === 0) {
    html += '<div class="empty">No unnamed clusters. All clusters have been named!</div>';
  } else {
    html += '<div class="grid">';
    for (const c of unnamed) {
      const img = c.sample_faces.length > 0 ? '<img class="face-img" src="' + c.sample_faces[0].thumbnail + '">' : '<div class="face-img"></div>';
      html += '<div class="card" data-cluster="' + c.cluster_id + '" onclick="toggleSelect(' + c.cluster_id + ')">';
      html += img;
      html += '<div style="margin:6px 0">';
      html += '<input class="name-input" id="name-' + c.cluster_id + '" placeholder="Name..." onkeydown="handleNameKeydown(event,' + c.cluster_id + ')" onclick="event.stopPropagation()">';
      html += '</div>';
      html += '<button onclick="event.stopPropagation();nameCluster(' + c.cluster_id + ')" style="font-size:12px;padding:4px 10px">Name</button>';
      html += '<div class="card-count" style="margin-top:6px">' + c.face_count + ' faces</div>';
      html += '<div class="card-id">Cluster ' + c.cluster_id + '</div>';
      html += '</div>';
    }
    html += '</div>';
  }

  document.getElementById('content').innerHTML = html;
  updateSelection();
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

loadAll();
</script>
</body>
</html>"""

        # Shared progress tracker for background face detection
        detect_progress = {"running": False, "processed": 0, "total": 0, "faces_found": 0, "current_file": ""}
        audit_progress = {"running": False, "processed": 0, "total": 0, "rejected": 0, "current_person": "", "details": []}

        class SearchHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse.urlparse(self.path)
                params = urlparse.parse_qs(parsed.query)

                if parsed.path == "/search":
                    q = params.get("q", [""])[0]
                    persons = params.get("persons", [None])[0]
                    topics = params.get("topics", [None])[0]
                    try:
                        limit = int(params.get("limit", ["20"])[0])
                    except ValueError:
                        limit = 20
                    limit = max(1, min(limit, 200))

                    # Append topics to query for broader keyword matching
                    search_query = q
                    if topics:
                        search_query = ("%s %s" % (q, topics)).strip()

                    if not search_query:
                        self._json({"error": "Missing ?q= parameter"}, 400)
                        return

                    # Fetch extra results when filtering by person so we have
                    # enough after post-filtering
                    fetch_limit = limit * 5 if persons else limit
                    results = indexer.search(search_query, fetch_limit)

                    # Post-filter by person name if requested
                    if persons:
                        person_lower = persons.lower()
                        results = [
                            r for r in results
                            if r.get("face_names") and person_lower in r["face_names"].lower()
                        ][:limit]

                    self._json({"query": q, "count": len(results), "results": results})

                elif parsed.path == "/status":
                    self._json(indexer.get_status())

                elif parsed.path == "/health":
                    self._json({"status": "ok"})

                elif parsed.path == "/gpu-status":
                    # Proxy endpoint: queries GPU servers on localhost (127.0.0.1-only ports)
                    # and returns their online/processing state. Lets the Mac app check GPU
                    # status without needing direct access to ports 8090/8091/8092.
                    _GPU_SERVERS = [
                        {"id": 0, "port": 8090, "name": "Gemma0"},
                        {"id": 1, "port": 8091, "name": "Gemma1"},
                    ]

                    def _check_gpu(srv):
                        port = srv["port"]
                        base = "http://localhost:%d" % port
                        try:
                            req = urllib.request.Request("%s/health" % base)
                            resp = urllib.request.urlopen(req, timeout=5)
                            code = resp.getcode()
                            if code == 503:
                                # whisper.cpp reports busy via 503
                                return dict(srv, online=True, processing=True)
                            if code != 200:
                                return dict(srv, online=False, processing=False)
                            # Try /slots for accurate llama.cpp processing state
                            try:
                                sreq = urllib.request.Request("%s/slots" % base)
                                sresp = urllib.request.urlopen(sreq, timeout=3)
                                slots = json.loads(sresp.read())
                                processing = any(s.get("is_processing", False) for s in slots)
                                return dict(srv, online=True, processing=processing)
                            except Exception:
                                return dict(srv, online=True, processing=False)
                        except urllib.error.URLError as e:
                            # llama.cpp blocks ALL HTTP during CLIP vision encoding (~32s);
                            # a timeout means the server is alive but busy.
                            if "timed out" in str(e).lower():
                                return dict(srv, online=True, processing=True)
                            return dict(srv, online=False, processing=False)
                        except Exception:
                            return dict(srv, online=False, processing=False)

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(_check_gpu, _GPU_SERVERS))

                    # Add Pro 580X status from orchestrator state file
                    pro580x_entry = {"id": 2, "port": 0, "name": "Pro 580X",
                                     "online": False, "processing": False,
                                     "current_model": None, "state": "unknown"}
                    try:
                        with open(str(PRO580X_STATE_FILE)) as f:
                            orch_state = json.loads(f.read())
                        state = orch_state.get("state", "unknown")
                        model = orch_state.get("current_model")
                        pro580x_entry["state"] = state
                        pro580x_entry["current_model"] = model
                        if model == "gemma":
                            pro580x_entry["port"] = PRO580X_GEMMA_PORT
                            pro580x_entry["online"] = state == "gemma_ready"
                            pro580x_entry["processing"] = orch_state.get("api_pending", False)
                        elif model == "whisper":
                            pro580x_entry["port"] = PRO580X_WHISPER_PORT
                            pro580x_entry["online"] = state == "whisper_busy"
                            pro580x_entry["processing"] = True
                        else:
                            pro580x_entry["online"] = state in ("gemma_loading", "whisper_loading")
                    except Exception:
                        pass
                    results.append(pro580x_entry)
                    self._json({"servers": results})

                elif parsed.path == "/orchestrator-status":
                    # Pro 580X orchestrator status — read from state file (IPC)
                    try:
                        with open(str(PRO580X_STATE_FILE)) as f:
                            state = json.loads(f.read())
                        self._json(state)
                    except FileNotFoundError:
                        self._json({"state": "unknown", "error": "Orchestrator not started (no state file)"})
                    except Exception as e:
                        self._json({"state": "unknown", "error": str(e)})

                elif parsed.path == "/thumbnail":
                    fid = params.get("id", [""])[0]
                    if not fid:
                        self._json({"error": "Missing ?id= parameter"}, 400)
                        return
                    img_bytes, mime = indexer.get_thumbnail(fid)
                    if img_bytes:
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        self.send_header("Content-Length", str(len(img_bytes)))
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(img_bytes)
                    else:
                        self._json({"error": "Thumbnail not available"}, 404)

                elif parsed.path == "/keyframe":
                    fid = params.get("file_id", [""])[0]
                    idx = params.get("index", ["0"])[0]
                    if not fid:
                        self._json({"error": "Missing ?file_id= parameter"}, 400)
                        return
                    try:
                        frame_path = THUMB_DIR / fid / ("frame_%02d.jpg" % int(idx))
                    except (ValueError, TypeError):
                        self._json({"error": "Invalid index"}, 400)
                        return
                    if frame_path.exists():
                        img_bytes = frame_path.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(img_bytes)))
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(img_bytes)
                    else:
                        self._json({"error": "Keyframe not available"}, 404)

                elif parsed.path == "/folders":
                    rows = indexer.db.execute(
                        "SELECT path, name, file_count, last_scan FROM folders WHERE enabled = 1"
                    ).fetchall()
                    folders_list = [{"path": r[0], "name": r[1], "count": r[2], "last_scan": r[3]} for r in rows]
                    self._json({"folders": folders_list})

                # --- Face management endpoints ---
                elif parsed.path == "/faces/clusters":
                    params = urllib.parse.parse_qs(parsed.query)
                    show = params.get("show", ["active"])[0]
                    clusters = indexer.get_face_clusters(show=show)
                    self._json({"clusters": clusters})

                elif parsed.path == "/faces/persons":
                    persons = indexer.get_persons()
                    self._json({"persons": persons})

                elif parsed.path == "/faces/detect/progress":
                    self._json(detect_progress)

                elif parsed.path == "/faces/audit/progress":
                    self._json(audit_progress)

                elif parsed.path == "/faces/status":
                    self._json(indexer.get_face_status())

                elif parsed.path.startswith("/faces/cluster/") and parsed.path.endswith("/faces"):
                    # GET /faces/cluster/<id>/faces — all faces in a cluster
                    parts = parsed.path.split("/")
                    try:
                        cluster_id = int(parts[3])
                    except (IndexError, ValueError):
                        self._json({"error": "Invalid cluster ID"}, 400)
                        return
                    fdb = sqlite3.connect(str(DB_PATH), timeout=10)
                    fdb.execute("PRAGMA journal_mode=WAL")
                    rows = fdb.execute("""
                        SELECT f.id, f.thumbnail_path, p.name
                        FROM faces f
                        LEFT JOIN persons p ON f.person_id = p.id
                        WHERE f.cluster_id = ?
                        ORDER BY f.created_at ASC
                    """, (cluster_id,)).fetchall()
                    person_name = rows[0][2] if rows and rows[0][2] else None
                    faces = []
                    for fid, thumb, _ in rows:
                        faces.append({
                            "id": fid,
                            "thumbnail_url": "/faces/thumbnail?id=%s" % fid,
                            "has_thumbnail": bool(thumb and os.path.exists(thumb)),
                        })
                    fdb.close()
                    self._json({
                        "cluster_id": cluster_id,
                        "person_name": person_name,
                        "face_count": len(faces),
                        "faces": faces,
                    })

                elif parsed.path == "/faces/thumbnail":
                    fid = params.get("id", [""])[0]
                    if not fid:
                        self._json({"error": "Missing ?id= parameter"}, 400)
                        return
                    img_bytes, mime = indexer.get_face_thumbnail(fid)
                    if img_bytes:
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        self.send_header("Content-Length", str(len(img_bytes)))
                        self.send_header("Cache-Control", "public, max-age=86400")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(img_bytes)
                    else:
                        self._json({"error": "Face thumbnail not available"}, 404)

                elif parsed.path == "/faces/ui":
                    body = FACE_UI_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                elif parsed.path == "/notifications":
                    ndb = sqlite3.connect(str(DB_PATH), timeout=10)
                    ndb.row_factory = sqlite3.Row
                    rows = ndb.execute(
                        "SELECT id, title, message, severity, created_at, read "
                        "FROM notifications ORDER BY id DESC LIMIT 50"
                    ).fetchall()
                    unread = ndb.execute(
                        "SELECT COUNT(*) FROM notifications WHERE read = 0"
                    ).fetchone()[0]
                    ndb.close()
                    notifications = [
                        {"id": r["id"], "title": r["title"], "message": r["message"],
                         "severity": r["severity"], "created_at": r["created_at"],
                         "read": bool(r["read"])}
                        for r in rows
                    ]
                    self._json({"notifications": notifications, "unread_count": unread})

                elif parsed.path == "/file-keyframes":
                    fid = params.get("id", [None])[0]
                    if not fid:
                        self._json({"error": "Missing ?id= parameter"}, 400)
                        return
                    rows = indexer.db.execute("""
                        SELECT timestamp_seconds FROM keyframes
                        WHERE file_id=? ORDER BY timestamp_seconds
                    """, (fid,)).fetchall()
                    keyframes = []
                    for i, (ts,) in enumerate(rows):
                        keyframes.append({
                            "index": i,
                            "timestamp": ts,
                            "thumbnail_url": "/keyframe?file_id=%s&index=%d" % (fid, i)
                        })
                    self._json({"keyframes": keyframes})

                elif parsed.path == "/transcripts":
                    # List files that have transcripts, filterable by folder path
                    # ?folder=Weekend Service  — substring match on file path
                    # ?limit=50               — max results (default 50, max 500)
                    folder_filter = params.get("folder", [""])[0]
                    try:
                        limit = int(params.get("limit", ["50"])[0])
                    except ValueError:
                        limit = 50
                    limit = max(1, min(limit, 500))

                    if folder_filter:
                        rows = indexer.db.execute(
                            "SELECT id, path, filename, duration_seconds, file_type "
                            "FROM files WHERE transcript IS NOT NULL AND transcript != '' "
                            "AND path LIKE ? ORDER BY filename LIMIT ?",
                            ("%%%s%%" % folder_filter, limit)
                        ).fetchall()
                    else:
                        rows = indexer.db.execute(
                            "SELECT id, path, filename, duration_seconds, file_type "
                            "FROM files WHERE transcript IS NOT NULL AND transcript != '' "
                            "ORDER BY filename LIMIT ?",
                            (limit,)
                        ).fetchall()

                    results = []
                    for r in rows:
                        results.append({
                            "id": r[0],
                            "path": r[1],
                            "filename": r[2],
                            "duration_seconds": r[3],
                            "file_type": r[4],
                        })
                    self._json({"count": len(results), "files": results})

                elif parsed.path == "/transcript":
                    # Get full transcript for a file by ID
                    # ?id=<file_id>
                    fid = params.get("id", [""])[0]
                    if not fid:
                        self._json({"error": "Missing ?id= parameter"}, 400)
                        return

                    row = indexer.db.execute(
                        "SELECT id, path, filename, duration_seconds, transcript, transcript_segments "
                        "FROM files WHERE id = ?", (fid,)
                    ).fetchone()

                    if not row:
                        self._json({"error": "File not found"}, 404)
                        return

                    segments = []
                    if row[5]:
                        try:
                            segments = json.loads(row[5])
                        except (json.JSONDecodeError, TypeError):
                            pass

                    self._json({
                        "id": row[0],
                        "path": row[1],
                        "filename": row[2],
                        "duration_seconds": row[3],
                        "transcript": row[4] or "",
                        "segments": segments,
                    })

                # --- API Job endpoints ---
                elif parsed.path.startswith("/api/jobs/"):
                    # GET /api/jobs/<job_id> — single job status
                    job_id = parsed.path[len("/api/jobs/"):]
                    adb = sqlite3.connect(str(DB_PATH), timeout=10)
                    adb.execute("PRAGMA journal_mode=WAL")
                    row = adb.execute("""
                        SELECT id, task_type, status, source_app, uploaded_filename,
                               result, error_message, created_at, started_at, completed_at
                        FROM api_jobs WHERE id=?
                    """, (job_id,)).fetchone()
                    if not row:
                        adb.close()
                        self._json({"error": "Job not found"}, 404)
                    else:
                        queue_position = None
                        if row[2] == 'queued':
                            queue_position = adb.execute("""
                                SELECT COUNT(*) FROM tasks
                                WHERE source = 'api' AND task_type = ? AND status = 'pending'
                                  AND created_at < ?
                            """, (row[1], row[7])).fetchone()[0]
                        result_data = None
                        if row[5]:
                            try:
                                result_data = json.loads(row[5])
                            except (json.JSONDecodeError, TypeError):
                                result_data = row[5]
                        adb.close()
                        self._json({
                            "job_id": row[0],
                            "task_type": row[1],
                            "status": row[2],
                            "source_app": row[3],
                            "uploaded_filename": row[4],
                            "result": result_data,
                            "error_message": row[6],
                            "created_at": row[7],
                            "started_at": row[8],
                            "completed_at": row[9],
                            "queue_position": queue_position,
                        })

                elif parsed.path == "/api/jobs":
                    # GET /api/jobs — list jobs
                    status_filter = params.get("status", [None])[0]
                    type_filter = params.get("task_type", [None])[0]
                    app_filter = params.get("source_app", [None])[0]
                    try:
                        limit = int(params.get("limit", ["20"])[0])
                    except ValueError:
                        limit = 20
                    limit = max(1, min(limit, 100))

                    conditions = []
                    bind_vals = []
                    if status_filter:
                        conditions.append("status = ?")
                        bind_vals.append(status_filter)
                    if type_filter:
                        conditions.append("task_type = ?")
                        bind_vals.append(type_filter)
                    if app_filter:
                        conditions.append("source_app = ?")
                        bind_vals.append(app_filter)

                    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
                    bind_vals.append(limit)

                    adb = sqlite3.connect(str(DB_PATH), timeout=10)
                    adb.execute("PRAGMA journal_mode=WAL")
                    rows = adb.execute(
                        "SELECT id, task_type, status, source_app, uploaded_filename, "
                        "created_at, started_at, completed_at "
                        "FROM api_jobs%s ORDER BY created_at DESC LIMIT ?" % where,
                        bind_vals
                    ).fetchall()
                    adb.close()
                    jobs = [{
                        "job_id": r[0], "task_type": r[1], "status": r[2],
                        "source_app": r[3], "uploaded_filename": r[4],
                        "created_at": r[5], "started_at": r[6], "completed_at": r[7],
                    } for r in rows]
                    self._json({"jobs": jobs, "count": len(jobs)})

                elif parsed.path == "/api/queue":
                    # GET /api/queue — queue overview
                    adb = sqlite3.connect(str(DB_PATH), timeout=10)
                    adb.execute("PRAGMA journal_mode=WAL")

                    # API job status counts
                    api_counts = {}
                    for row in adb.execute(
                        "SELECT status, COUNT(*) FROM api_jobs GROUP BY status"
                    ).fetchall():
                        api_counts[row[0]] = row[1]

                    # Per-type breakdown: API vs crawler pending/processing
                    by_type = {}
                    for row in adb.execute("""
                        SELECT task_type, source,
                               SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),
                               SUM(CASE WHEN status='assigned' THEN 1 ELSE 0 END)
                        FROM tasks
                        WHERE status IN ('pending', 'assigned')
                        GROUP BY task_type, source
                    """).fetchall():
                        tt = row[0]
                        if tt not in by_type:
                            by_type[tt] = {"api_queued": 0, "api_processing": 0,
                                           "crawler_pending": 0, "crawler_processing": 0}
                        if row[1] == 'api':
                            by_type[tt]["api_queued"] = row[2]
                            by_type[tt]["api_processing"] = row[3]
                        else:
                            by_type[tt]["crawler_pending"] = row[2]
                            by_type[tt]["crawler_processing"] = row[3]
                    adb.close()

                    self._json({
                        "api_jobs": api_counts,
                        "by_type": by_type,
                    })

                elif parsed.path == "/worker-status":
                    self._handle_worker_status()

                else:
                    self._json({"error": "Not found"}, 404)

            def do_POST(self):
                parsed = urlparse.urlparse(self.path)
                length = int(self.headers.get("Content-Length", 0))

                # --- /api/jobs: multipart file upload ---
                if parsed.path == "/api/jobs":
                    self._handle_api_job_submit(length)
                    return

                body = self.rfile.read(length) if length else b"{}"
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    self._json({"error": "Invalid JSON"}, 400)
                    return

                if parsed.path == "/faces/name":
                    cluster_id = data.get("cluster_id")
                    name = data.get("name", "").strip()
                    if cluster_id is None or not name:
                        self._json({"error": "cluster_id and name required"}, 400)
                        return
                    pid = indexer.name_cluster(int(cluster_id), name)
                    self._json({"ok": True, "person_id": pid})

                elif parsed.path == "/faces/merge":
                    source = data.get("source_cluster_id")
                    target = data.get("target_cluster_id")
                    if source is None or target is None:
                        self._json({"error": "source_cluster_id and target_cluster_id required"}, 400)
                        return
                    indexer.merge_clusters(int(source), int(target))
                    self._json({"ok": True})

                elif parsed.path == "/faces/cluster":
                    tolerance = data.get("tolerance")
                    if tolerance is not None:
                        tolerance = float(tolerance)
                    full = data.get("full", False)
                    clusters = indexer.cluster_faces(tolerance=tolerance, full=full)
                    self._json({
                        "ok": True,
                        "cluster_count": len(clusters),
                        "face_count": sum(len(v) for v in clusters.values())
                    })

                elif parsed.path == "/faces/assign":
                    count = indexer.assign_new_faces()
                    self._json({"ok": True, "assigned": count})

                elif parsed.path == "/faces/rename":
                    person_id = data.get("person_id", "")
                    name = data.get("name", "").strip()
                    if not person_id or not name:
                        self._json({"error": "person_id and name required"}, 400)
                        return
                    indexer.rename_person(person_id, name)
                    self._json({"ok": True})

                elif parsed.path == "/faces/unname":
                    cluster_id = data.get("cluster_id")
                    if cluster_id is None:
                        self._json({"error": "cluster_id required"}, 400)
                        return
                    indexer.unname_cluster(int(cluster_id))
                    self._json({"ok": True})

                elif parsed.path == "/faces/remove-face":
                    face_id = data.get("face_id", "").strip()
                    if not face_id:
                        self._json({"error": "face_id required"}, 400)
                        return
                    fdb = sqlite3.connect(str(DB_PATH), timeout=10)
                    fdb.execute("PRAGMA journal_mode=WAL")
                    fdb.execute("PRAGMA busy_timeout=10000")
                    # Get current cluster info before removing
                    row = fdb.execute(
                        "SELECT cluster_id, person_id FROM faces WHERE id=?", (face_id,)
                    ).fetchone()
                    if not row:
                        fdb.close()
                        self._json({"error": "Face not found"}, 404)
                        return
                    old_cluster_id, old_person_id = row
                    # Find next available negative cluster_id for unclustered faces
                    min_row = fdb.execute("SELECT MIN(cluster_id) FROM faces").fetchone()
                    new_cluster = min(min_row[0] or 0, 0) - 1
                    # Move face to its own unclustered cluster
                    fdb.execute(
                        "UPDATE faces SET person_id=NULL, cluster_id=? WHERE id=?",
                        (new_cluster, face_id)
                    )
                    # If old cluster is now empty of that person, clean up person record face count
                    if old_person_id:
                        remaining = fdb.execute(
                            "SELECT COUNT(*) FROM faces WHERE person_id=?", (old_person_id,)
                        ).fetchone()[0]
                        fdb.execute(
                            "UPDATE persons SET face_count=? WHERE id=?",
                            (remaining, old_person_id)
                        )
                    fdb.commit()
                    fdb.close()
                    self._json({"ok": True, "new_cluster_id": new_cluster})

                elif parsed.path == "/faces/ignore":
                    cluster_id = data.get("cluster_id")
                    if cluster_id is None:
                        self._json({"error": "cluster_id required"}, 400)
                        return
                    indexer.ignore_cluster(int(cluster_id))
                    self._json({"ok": True})

                elif parsed.path == "/faces/unignore":
                    cluster_id = data.get("cluster_id")
                    if cluster_id is None:
                        self._json({"error": "cluster_id required"}, 400)
                        return
                    indexer.unignore_cluster(int(cluster_id))
                    self._json({"ok": True})

                elif parsed.path == "/notifications/mark-read":
                    ndb = sqlite3.connect(str(DB_PATH), timeout=10)
                    updated = ndb.execute(
                        "UPDATE notifications SET read = 1 WHERE read = 0"
                    ).rowcount
                    ndb.commit()
                    ndb.close()
                    self._json({"ok": True, "marked_read": updated})

                elif parsed.path == "/faces/deduplicate":
                    removed = indexer.deduplicate_faces()
                    self._json({"ok": True, "removed": removed})

                elif parsed.path == "/faces/audit":
                    if audit_progress["running"]:
                        self._json({"ok": True, "message": "Audit already running", **audit_progress})
                        return
                    person_id = data.get("person_id")

                    def _run_audit():
                        try:
                            audit_progress["running"] = True
                            audit_progress["processed"] = 0
                            audit_progress["rejected"] = 0
                            audit_progress["details"] = []

                            adb = sqlite3.connect(str(DB_PATH), timeout=10)
                            adb.execute("PRAGMA journal_mode=WAL")
                            if person_id:
                                persons = adb.execute(
                                    "SELECT id, name, face_count FROM persons WHERE id=?",
                                    (person_id,)).fetchall()
                            else:
                                persons = adb.execute(
                                    "SELECT id, name, face_count FROM persons WHERE face_count > 1 ORDER BY face_count DESC"
                                ).fetchall()
                            adb.close()

                            audit_progress["total"] = len(persons)
                            for pid, pname, fcount in persons:
                                audit_progress["current_person"] = pname
                                rejected = indexer.audit_person_faces(pid)
                                audit_progress["processed"] += 1
                                audit_progress["rejected"] += rejected
                                if rejected > 0:
                                    audit_progress["details"].append({
                                        "name": pname, "rejected": rejected,
                                        "original": fcount, "remaining": fcount - rejected
                                    })
                        except Exception as e:
                            log.error("Audit error: %s" % e)
                        finally:
                            audit_progress["running"] = False
                            audit_progress["current_person"] = ""

                    threading.Thread(target=_run_audit, daemon=True, name="face-audit").start()
                    self._json({"ok": True, "message": "Audit started in background", "persons": audit_progress["total"]})

                elif parsed.path == "/faces/detect":
                    if not HAS_FACE_RECOGNITION:
                        self._json({"error": "face_recognition not installed"}, 500)
                        return
                    if detect_progress["running"]:
                        self._json({"ok": True, "message": "Face detection already running"})
                        return
                    def _run_detect():
                        import multiprocessing as mp
                        try:
                            detect_progress["running"] = True
                            detect_progress["processed"] = 0
                            detect_progress["faces_found"] = 0
                            detect_progress["current_file"] = ""

                            thread_db = sqlite3.connect(str(DB_PATH), timeout=30)
                            thread_db.execute("PRAGMA journal_mode=WAL")
                            thread_db.execute("PRAGMA busy_timeout=30000")

                            # Use face_scanned_files to find unscanned files
                            images = thread_db.execute("""
                                SELECT f.id, f.path FROM files f
                                WHERE f.status = 'indexed' AND f.file_type = 'image'
                                AND f.id NOT IN (SELECT file_id FROM face_scanned_files)
                            """).fetchall()

                            keyframes = thread_db.execute("""
                                SELECT k.id, k.file_id, k.thumbnail_path
                                FROM keyframes k JOIN files f ON k.file_id = f.id
                                WHERE f.status = 'indexed'
                                AND f.id NOT IN (SELECT file_id FROM face_scanned_files)
                            """).fetchall()

                            work = []
                            for fid, fpath in images:
                                if Path(fpath).exists():
                                    work.append((fid, fpath, None))
                            for kf_id, fid, thumb_path in keyframes:
                                if Path(thumb_path).exists():
                                    work.append((fid, thumb_path, kf_id))

                            total = len(work)
                            detect_progress["total"] = total
                            processed = 0
                            found_total = 0

                            ctx = mp.get_context("spawn")
                            pool = ctx.Pool(processes=FACE_WORKERS)
                            paths = [item[1] for item in work]

                            for idx, (img_path, face_data) in enumerate(
                                zip(paths, pool.imap(_detect_faces_worker, paths))
                            ):
                                fid, fpath, kf_id = work[idx]
                                _, raw_faces = face_data
                                detect_progress["current_file"] = Path(fpath).name

                                if raw_faces:
                                    faces_result = []
                                    for enc_bytes, bbox in raw_faces:
                                        enc = np.frombuffer(enc_bytes, dtype=np.float64).copy()
                                        faces_result.append((enc, bbox))
                                    cnt = store_faces(thread_db, fid, fpath, faces_result, keyframe_id=kf_id)
                                    found_total += cnt
                                    detect_progress["faces_found"] = found_total
                                    log.info("  %s: %d face(s)" % (Path(fpath).name, cnt))

                                # Mark file as scanned (even if 0 faces)
                                indexer.mark_face_scanned(fid, db=thread_db)

                                processed += 1
                                detect_progress["processed"] = processed
                                if processed % 50 == 0:
                                    log.info("  Progress: %d / %d (%d faces found)" % (processed, total, found_total))
                                    thread_db.commit()

                            pool.close()
                            pool.join()
                            thread_db.commit()
                            log.info("Face detection complete: scanned %d items, found %d faces" % (processed, found_total))

                            # Auto-cluster new faces (incremental - preserves existing merges)
                            if found_total > 0:
                                log.info("Auto-clustering new faces...")
                                clusters = indexer.cluster_faces(db=thread_db)
                                log.info("Auto-clustering complete: %d new clusters" % len(clusters))

                            thread_db.close()
                        except Exception as e:
                            log.error("Face detection error: %s" % e)
                            import traceback
                            log.error(traceback.format_exc())
                        finally:
                            detect_progress["running"] = False
                            detect_progress["current_file"] = ""
                    t = threading.Thread(target=_run_detect, daemon=True)
                    t.start()
                    self._json({"ok": True, "message": "Face detection started with %d workers" % FACE_WORKERS})

                else:
                    self._json({"error": "Not found"}, 404)

            def _handle_worker_status(self):
                """GET /worker-status — detailed worker and queue info for VaultSearch."""
                _GPU_SERVERS = [
                    {"name": "Gemma0", "port": 8090, "model": "Gemma 3 12B", "task_type": "visual_analysis"},
                    {"name": "Gemma1", "port": 8091, "model": "Gemma 3 12B", "task_type": "visual_analysis"},
                ]

                # Check GPU online/processing status
                def _check(srv):
                    port = srv["port"]
                    base = "http://localhost:%d" % port
                    try:
                        req = urllib.request.Request("%s/health" % base)
                        resp = urllib.request.urlopen(req, timeout=5)
                        code = resp.getcode()
                        if code == 503:
                            return True, True  # online, processing
                        if code != 200:
                            return False, False
                        try:
                            sreq = urllib.request.Request("%s/slots" % base)
                            sresp = urllib.request.urlopen(sreq, timeout=3)
                            slots = json.loads(sresp.read())
                            processing = any(s.get("is_processing", False) for s in slots)
                            return True, processing
                        except Exception:
                            return True, False
                    except urllib.error.URLError as e:
                        if "timed out" in str(e).lower():
                            return True, True  # blocked = busy
                        return False, False
                    except Exception:
                        return False, False

                adb = sqlite3.connect(str(DB_PATH), timeout=10)
                adb.execute("PRAGMA journal_mode=WAL")

                # Get currently assigned tasks
                assigned = adb.execute("""
                    SELECT t.id, t.task_type, t.worker_id, t.source, f.filename
                    FROM tasks t JOIN files f ON t.file_id = f.id
                    WHERE t.status = 'assigned'
                """).fetchall()

                # Build worker_id -> task info map
                assigned_map = {}
                for tid, ttype, wid, src, fname in assigned:
                    if wid:
                        assigned_map[wid] = {"source": src or "crawler", "file": fname, "task_type": ttype}

                # Queue depths by type and source
                queue_rows = adb.execute("""
                    SELECT task_type, source, COUNT(*) FROM tasks
                    WHERE status = 'pending'
                    GROUP BY task_type, source
                """).fetchall()
                queue_map = {}  # {task_type: {api: N, crawler: M}}
                for tt, src, cnt in queue_rows:
                    if tt not in queue_map:
                        queue_map[tt] = {"api": 0, "crawler": 0}
                    queue_map[tt][src or "crawler"] = cnt

                # Read scanner state for crawler info
                scanner_state = {"state": "idle", "current_folder": ""}
                try:
                    if SCANNER_STATE_FILE.exists():
                        with open(SCANNER_STATE_FILE, "r") as f:
                            sdata = json.load(f)
                        ss = sdata.get("state", {})
                        scanner_state["state"] = ss.get("state", "idle")
                        scanner_state["current_folder"] = Path(ss.get("current_folder", "")).name if ss.get("current_folder") else ""
                except Exception:
                    pass

                adb.close()

                # Build GPU list
                gpus = []
                for srv in _GPU_SERVERS:
                    online, processing = _check(srv)
                    # Find current task for this GPU by matching worker_id pattern
                    current_task = None
                    port_str = str(srv["port"])
                    for wid, tinfo in assigned_map.items():
                        if port_str in wid:
                            current_task = tinfo
                            break
                    q = queue_map.get(srv["task_type"], {"api": 0, "crawler": 0})
                    gpus.append({
                        "name": srv["name"],
                        "port": srv["port"],
                        "model": srv["model"],
                        "online": online,
                        "processing": processing or (current_task is not None),
                        "current_task": current_task,
                        "queue": q,
                    })

                # Pro 580X — dynamic model (Gemma for API or Whisper for transcription)
                pro580x_current = None
                for wid, tinfo in assigned_map.items():
                    if "pro580x" in wid or ("whisper" in wid):
                        pro580x_current = tinfo
                        break
                try:
                    with open(str(PRO580X_STATE_FILE)) as f:
                        orch = json.loads(f.read())
                    orch_state = orch.get("state", "unknown")
                    orch_model = orch.get("current_model")
                    if orch_model == "gemma":
                        model_name = "Gemma 3 12B (API)"
                        port = PRO580X_GEMMA_PORT
                    elif orch_model == "whisper":
                        model_name = "Whisper large-v3-turbo"
                        port = PRO580X_WHISPER_PORT
                    else:
                        model_name = "loading..."
                        port = 0
                    va_q = queue_map.get("visual_analysis", {"api": 0, "crawler": 0})
                    tr_q = queue_map.get("transcribe", {"api": 0, "crawler": 0})
                    gpus.append({
                        "name": "Pro 580X",
                        "port": port,
                        "model": model_name,
                        "online": orch_state in ("gemma_ready", "whisper_busy"),
                        "processing": pro580x_current is not None or orch_state == "whisper_busy",
                        "current_task": pro580x_current,
                        "queue": {"api": va_q.get("api", 0), "crawler": tr_q.get("crawler", 0)},
                        "orchestrator_state": orch_state,
                    })
                except Exception:
                    gpus.append({
                        "name": "Pro 580X",
                        "port": 0,
                        "model": "unknown",
                        "online": False,
                        "processing": False,
                        "current_task": None,
                        "queue": {"api": 0, "crawler": 0},
                        "orchestrator_state": "unknown",
                    })

                # CPU workers
                fd_q = queue_map.get("face_detect", {"api": 0, "crawler": 0})
                face_current = None
                for wid, tinfo in assigned_map.items():
                    if "face" in wid:
                        face_current = tinfo
                        break

                sd_q = queue_map.get("scene_detect", {"api": 0, "crawler": 0})
                scene_active = sum(1 for wid in assigned_map if "scene" in wid)

                # ALA worker
                ala_q = queue_map.get("ala", {"api": 0, "crawler": 0})
                ala_current = None
                for wid, tinfo in assigned_map.items():
                    if "ala" in wid:
                        ala_current = tinfo
                        break

                self._json({
                    "gpus": gpus,
                    "cpu_workers": {
                        "face_detect": {
                            "processing": face_current is not None,
                            "current_task": face_current,
                            "queue": fd_q,
                        },
                        "scene_detect": {
                            "workers": 3,
                            "active": scene_active,
                            "queue": sd_q,
                        },
                        "ala": {
                            "processing": ala_current is not None,
                            "current_task": ala_current,
                            "queue": ala_q,
                        },
                    },
                    "crawler": scanner_state,
                })

            def _handle_api_job_submit(self, length):
                """Handle POST /api/jobs — multipart file upload for job submission."""
                VALID_TASK_TYPES = {'transcribe', 'visual_analysis', 'face_detect', 'scene_detect', 'ala', 'text_chat'}
                content_type = self.headers.get("Content-Type", "")
                ctype, pdict = cgi.parse_header(content_type)
                if ctype != "multipart/form-data":
                    self._json({"error": "Content-Type must be multipart/form-data"}, 400)
                    return

                # Read the raw body and parse multipart using email module
                # to correctly extract filenames from Content-Disposition
                raw_body = self.rfile.read(length)
                boundary = pdict.get('boundary', '')
                if isinstance(boundary, str):
                    boundary = boundary.encode()

                # Parse using email to get filenames
                from email.parser import BytesParser
                from email.policy import default as email_policy
                msg_bytes = (
                    b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw_body
                )
                msg = BytesParser(policy=email_policy).parsebytes(msg_bytes)

                fields = {}  # name -> value
                file_data = None
                original_filename = "upload"

                for part in msg.iter_parts():
                    cd = part.get("Content-Disposition", "")
                    _, cd_params = cgi.parse_header(cd)
                    name = cd_params.get("name", "")
                    fname = cd_params.get("filename")
                    payload = part.get_payload(decode=True)
                    if fname:
                        # This is the file part
                        file_data = payload
                        original_filename = Path(fname).name or "upload"
                    else:
                        val = payload.decode("utf-8", errors="replace") if payload else ""
                        fields[name] = val.strip()

                # Extract task_type
                task_type = fields.get("task_type", "").strip()
                if task_type not in VALID_TASK_TYPES:
                    self._json({"error": "task_type must be one of: %s" % ", ".join(sorted(VALID_TASK_TYPES))}, 400)
                    return

                # --- text_chat: prompt-only, no file required ---
                if task_type == 'text_chat':
                    prompt = fields.get("prompt", "").strip()
                    if not prompt:
                        self._json({"error": "prompt is required for text_chat"}, 400)
                        return
                    source_app = fields.get("source_app", "").strip() or None
                    max_tokens = None
                    temperature = None
                    try:
                        max_tokens = int(fields["max_tokens"]) if "max_tokens" in fields else None
                    except (ValueError, TypeError):
                        pass
                    try:
                        temperature = float(fields["temperature"]) if "temperature" in fields else None
                    except (ValueError, TypeError):
                        pass

                    job_id = str(uuid.uuid4())
                    now = datetime.now().isoformat()
                    # Synthetic file_id — no real file
                    fid = hashlib.sha256(("%s|%s" % (prompt[:200], now)).encode()).hexdigest()[:16]
                    task_id = "%s_text_chat" % fid

                    adb = sqlite3.connect(str(DB_PATH), timeout=30)
                    adb.execute("PRAGMA journal_mode=WAL")
                    adb.execute("PRAGMA busy_timeout=30000")
                    try:
                        adb.execute("""
                            INSERT INTO api_jobs (id, task_type, status, source_app, prompt, max_tokens, temperature, created_at)
                            VALUES (?, 'text_chat', 'queued', ?, ?, ?, ?, ?)
                        """, (job_id, source_app, prompt, max_tokens, temperature, now))
                        adb.execute("""
                            INSERT INTO tasks (id, file_id, task_type, status, source, api_job_id, created_at)
                            VALUES (?, ?, 'text_chat', 'pending', 'api', ?, ?)
                        """, (task_id, fid, job_id, now))
                        adb.commit()
                        pos = adb.execute("""
                            SELECT COUNT(*) FROM tasks
                            WHERE source = 'api' AND task_type = 'text_chat' AND status = 'pending'
                              AND created_at < ?
                        """, (now,)).fetchone()[0]
                        self._json({
                            "ok": True,
                            "job_id": job_id,
                            "task_id": task_id,
                            "task_type": "text_chat",
                            "status": "queued",
                            "queue_position": pos
                        })
                    except Exception as e:
                        adb.rollback()
                        self._json({"error": "Failed to create job: %s" % e}, 500)
                    finally:
                        adb.close()
                    return

                # --- File-based tasks: require file upload ---
                if not file_data:
                    self._json({"error": "file is required"}, 400)
                    return

                source_app = fields.get("source_app", "").strip() or None

                # Generate job ID and save file
                job_id = str(uuid.uuid4())
                safe_name = re.sub(r'[^\w.\-]', '_', original_filename)
                upload_path = str(UPLOAD_DIR / ("%s_%s" % (job_id, safe_name)))
                with open(upload_path, "wb") as f:
                    f.write(file_data)

                now = datetime.now().isoformat()
                fsize = len(file_data)

                # Create a virtual file record
                fid = hashlib.sha256(("%s|%s|%s" % (upload_path, fsize, now)).encode()).hexdigest()[:16]
                # Determine file type from extension
                ext = Path(original_filename).suffix.lower()
                if ext in IMAGE_EXTS:
                    file_type = "image"
                elif ext in VIDEO_EXTS:
                    file_type = "video"
                elif ext in AUDIO_EXTS:
                    file_type = "audio"
                else:
                    file_type = "unknown"

                adb = sqlite3.connect(str(DB_PATH), timeout=30)
                adb.execute("PRAGMA journal_mode=WAL")
                adb.execute("PRAGMA busy_timeout=30000")
                try:
                    # Insert file record (so workers can find it via JOIN)
                    adb.execute("""
                        INSERT OR IGNORE INTO files (id, path, filename, file_type, size_bytes, modified_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    """, (fid, upload_path, original_filename, file_type, fsize, now))

                    # Insert API job record
                    lyrics = fields.get("lyrics", "").strip() or None
                    adb.execute("""
                        INSERT INTO api_jobs (id, task_type, status, source_app, uploaded_filename, upload_path, lyrics, created_at)
                        VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
                    """, (job_id, task_type, source_app, original_filename, upload_path, lyrics, now))

                    # Create the task
                    task_id = "%s_%s" % (fid, task_type)
                    adb.execute("""
                        INSERT INTO tasks (id, file_id, task_type, status, source, api_job_id, created_at)
                        VALUES (?, ?, ?, 'pending', 'api', ?, ?)
                    """, (task_id, fid, task_type, job_id, now))
                    adb.commit()

                    # Calculate queue position
                    pos = adb.execute("""
                        SELECT COUNT(*) FROM tasks
                        WHERE source = 'api' AND task_type = ? AND status = 'pending'
                          AND created_at < ?
                    """, (task_type, now)).fetchone()[0]

                    self._json({
                        "ok": True,
                        "job_id": job_id,
                        "task_id": task_id,
                        "task_type": task_type,
                        "status": "queued",
                        "queue_position": pos
                    })
                except Exception as e:
                    adb.rollback()
                    # Clean up uploaded file on error
                    try:
                        os.unlink(upload_path)
                    except OSError:
                        pass
                    self._json({"error": "Failed to create job: %s" % e}, 500)
                finally:
                    adb.close()

            def do_DELETE(self):
                parsed = urlparse.urlparse(self.path)
                # DELETE /api/jobs/<job_id>
                if parsed.path.startswith("/api/jobs/"):
                    job_id = parsed.path[len("/api/jobs/"):]
                    if not job_id:
                        self._json({"error": "job_id required"}, 400)
                        return
                    adb = sqlite3.connect(str(DB_PATH), timeout=30)
                    adb.execute("PRAGMA journal_mode=WAL")
                    adb.execute("PRAGMA busy_timeout=30000")
                    try:
                        row = adb.execute(
                            "SELECT status, upload_path FROM api_jobs WHERE id=?", (job_id,)
                        ).fetchone()
                        if not row:
                            self._json({"error": "Job not found"}, 404)
                            return
                        if row[0] != 'queued':
                            self._json({"error": "Can only cancel queued jobs (current status: %s)" % row[0]}, 409)
                            return
                        # Delete task, job, and uploaded file
                        adb.execute("DELETE FROM tasks WHERE api_job_id=?", (job_id,))
                        adb.execute("DELETE FROM api_jobs WHERE id=?", (job_id,))
                        adb.commit()
                        if row[1]:
                            try:
                                os.unlink(row[1])
                            except OSError:
                                pass
                        self._json({"ok": True, "cancelled": job_id})
                    finally:
                        adb.close()
                else:
                    self._json({"error": "Not found"}, 404)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _json(self, data, code=200):
                body = json.dumps(data, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                log.info(f"HTTP {args[0]}")

        server = HTTPServer(("0.0.0.0", serve_port), SearchHandler)
        log.info(f"Search API running on http://0.0.0.0:{serve_port}")
        log.info(f"Endpoints: /search?q=, /status, /health, /gpu-status, /folders, /transcripts, /transcript, /faces/ui, /faces/clusters, /faces/persons")

        # Auto-start face detection if there are unscanned files
        if HAS_FACE_RECOGNITION:
            unscanned = indexer.db.execute("""
                SELECT COUNT(*) FROM files f
                WHERE f.status = 'indexed' AND f.file_type IN ('image', 'video')
                AND f.id NOT IN (SELECT file_id FROM face_scanned_files)
            """).fetchone()[0]
            if unscanned > 0:
                log.info("Auto-starting face detection for %d unscanned files (%d workers)..." % (unscanned, FACE_WORKERS))
                def _auto_detect():
                    import multiprocessing as mp
                    try:
                        detect_progress["running"] = True
                        detect_progress["processed"] = 0
                        detect_progress["faces_found"] = 0
                        detect_progress["current_file"] = ""

                        thread_db = sqlite3.connect(str(DB_PATH), timeout=30)
                        thread_db.execute("PRAGMA journal_mode=WAL")
                        thread_db.execute("PRAGMA busy_timeout=30000")

                        # Use face_scanned_files to find unscanned files
                        images = thread_db.execute("""
                            SELECT f.id, f.path FROM files f
                            WHERE f.status = 'indexed' AND f.file_type = 'image'
                            AND f.id NOT IN (SELECT file_id FROM face_scanned_files)
                        """).fetchall()

                        kframes = thread_db.execute("""
                            SELECT k.id, k.file_id, k.thumbnail_path
                            FROM keyframes k JOIN files f ON k.file_id = f.id
                            WHERE f.status = 'indexed'
                            AND f.id NOT IN (SELECT file_id FROM face_scanned_files)
                        """).fetchall()

                        # Build work items: (file_id, image_path, keyframe_id_or_None)
                        work = []
                        for fid, fpath in images:
                            if Path(fpath).exists():
                                work.append((fid, fpath, None))
                        for kf_id, fid, thumb_path in kframes:
                            if Path(thumb_path).exists():
                                work.append((fid, thumb_path, kf_id))

                        total = len(work)
                        detect_progress["total"] = total
                        processed = 0
                        found_total = 0

                        # Use spawn context to avoid forking ChromaDB's tokio threads
                        ctx = mp.get_context("spawn")
                        pool = ctx.Pool(processes=FACE_WORKERS)

                        # Feed paths to the pool
                        paths = [item[1] for item in work]
                        for idx, (img_path, face_data) in enumerate(
                            zip(paths, pool.imap(_detect_faces_worker, paths))
                        ):
                            fid, fpath, kf_id = work[idx]
                            _, raw_faces = face_data
                            detect_progress["current_file"] = Path(fpath).name

                            if raw_faces:
                                # Convert bytes back to numpy arrays for store_faces
                                faces_result = []
                                for enc_bytes, bbox in raw_faces:
                                    enc = np.frombuffer(enc_bytes, dtype=np.float64).copy()
                                    faces_result.append((enc, bbox))
                                cnt = store_faces(thread_db, fid, fpath, faces_result, keyframe_id=kf_id)
                                found_total += cnt
                                detect_progress["faces_found"] = found_total
                                log.info("  %s: %d face(s)" % (Path(fpath).name, cnt))

                            # Mark file as scanned (even if 0 faces)
                            indexer.mark_face_scanned(fid, db=thread_db)

                            processed += 1
                            detect_progress["processed"] = processed
                            if processed % 50 == 0:
                                log.info("  Progress: %d / %d (%d faces found)" % (processed, total, found_total))
                                thread_db.commit()

                        pool.close()
                        pool.join()
                        thread_db.commit()
                        log.info("Face detection complete: scanned %d items, found %d faces" % (processed, found_total))

                        # Auto-cluster new faces (incremental - preserves existing merges)
                        if found_total > 0:
                            log.info("Auto-clustering new faces...")
                            clusters = indexer.cluster_faces(db=thread_db)
                            log.info("Auto-clustering complete: %d new clusters" % len(clusters))

                            # Deduplicate: keep best face per person per file
                            deduped = indexer.deduplicate_faces(db=thread_db)
                            if deduped:
                                log.info("Dedup: removed %d duplicate faces" % deduped)

                        thread_db.close()
                    except Exception as e:
                        log.error("Face detection error: %s" % e)
                        import traceback
                        log.error(traceback.format_exc())
                    finally:
                        detect_progress["running"] = False
                        detect_progress["current_file"] = ""
                t = threading.Thread(target=_auto_detect, daemon=True)
                t.start()

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("Search API stopped.")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
