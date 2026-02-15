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
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Optional: ChromaDB for semantic/vector search
try:
    import chromadb
    HAS_CHROMADB = True
except ImportError:
    HAS_CHROMADB = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LLM_SERVER = "http://localhost:8080"
DATA_DIR = Path.home() / "media-index"
DB_PATH = DATA_DIR / "index.db"
THUMB_DIR = DATA_DIR / "thumbnails"
CHROMA_DIR = DATA_DIR / "chroma"
LOG_PATH = DATA_DIR / "indexer.log"
FFMPEG = "/usr/local/bin/ffmpeg"
FFPROBE = "/usr/local/bin/ffprobe"

# File extensions to index
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".m4v", ".avi", ".mkv", ".r3d", ".braw"}
AUDIO_EXTS = {".wav", ".mp3", ".aac", ".m4a", ".flac", ".aif", ".aiff"}
ALL_MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

# Skip these directories
SKIP_DIRS = {".Spotlight-V100", ".Trashes", ".fseventsd", ".DS_Store",
             ".TemporaryItems", "@eaDir", "#recycle", "$RECYCLE.BIN"}

# How many keyframes to extract per video
KEYFRAMES_PER_VIDEO = 3  # beginning, middle, end

# Max concurrent LLM requests (matches --parallel 3)
MAX_CONCURRENT_LLM = 2  # Leave 1 slot free for user queries

# Max image size to send to LLM (resize if larger)
MAX_IMAGE_DIMENSION = 1280

# Rescan interval for watching (seconds)
RESCAN_INTERVAL = 300  # 5 minutes

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

    db = sqlite3.connect(str(DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")

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
    """)

    # Full-text search index
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
            filename, ai_description, tags,
            content='files',
            content_rowid='rowid'
        )
    """)

    # Triggers to keep FTS in sync
    db.executescript("""
        CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
            INSERT INTO files_fts(rowid, filename, ai_description, tags)
            VALUES (new.rowid, new.filename, new.ai_description, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, filename, ai_description, tags)
            VALUES ('delete', old.rowid, old.filename, old.ai_description, old.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF ai_description, tags ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, filename, ai_description, tags)
            VALUES ('delete', old.rowid, old.filename, old.ai_description, old.tags);
            INSERT INTO files_fts(rowid, filename, ai_description, tags)
            VALUES (new.rowid, new.filename, new.ai_description, new.tags);
        END;
    """)

    db.commit()
    return db


def file_id(path, size, mtime):
    """Generate a stable ID from path + size + mtime."""
    raw = f"{path}|{size}|{mtime}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def folder_id(path):
    """Generate a stable folder ID."""
    return hashlib.sha256(path.encode()).hexdigest()[:16]

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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
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

def extract_thumbnail(video_path, timestamp, output_path):
    """Extract a single frame from a video at the given timestamp."""
    try:
        cmd = [
            FFMPEG, "-y", "-ss", str(timestamp),
            "-i", str(video_path),
            "-vframes", "1",
            "-vf", f"scale='min({MAX_IMAGE_DIMENSION},iw)':-2",
            "-q:v", "3",
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    except Exception as e:
        log.warning(f"Thumbnail extraction failed: {e}")
        return False


def extract_keyframes(video_path, duration, file_hash):
    """Extract keyframes from a video. Returns list of (timestamp, thumbnail_path)."""
    thumb_subdir = THUMB_DIR / file_hash
    thumb_subdir.mkdir(parents=True, exist_ok=True)

    frames = []
    if duration <= 0:
        return frames

    # Pick timestamps: 10% in, middle, 90% in
    timestamps = []
    if duration < 5:
        timestamps = [duration / 2]
    elif duration < 30:
        timestamps = [1, duration / 2]
    else:
        timestamps = [
            duration * 0.1,
            duration * 0.5,
            duration * 0.9
        ]

    for i, ts in enumerate(timestamps[:KEYFRAMES_PER_VIDEO]):
        thumb_path = thumb_subdir / f"frame_{i:02d}.jpg"
        if extract_thumbnail(video_path, ts, thumb_path):
            frames.append((ts, str(thumb_path)))

    return frames

# ---------------------------------------------------------------------------
# LLM Vision API
# ---------------------------------------------------------------------------

def describe_image(image_path, context=""):
    """Send an image to Gemma 3 12B and get a description."""
    try:
        # Read and base64 encode the image
        with open(image_path, "rb") as f:
            img_bytes = f.read()

        # Determine mime type
        ext = Path(image_path).suffix.lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "tif": "image/tiff", "tiff": "image/tiff", "bmp": "image/bmp",
                "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")

        img_b64 = base64.b64encode(img_bytes).decode()

        prompt = (
            "Describe this image concisely for a searchable media database. "
            "Include: main subject, setting/location type, lighting, colors, "
            "people (count and activity), equipment or objects visible, "
            "camera angle, and mood. Use keywords that someone might search for. "
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
            f"{LLM_SERVER}/v1/chat/completions",
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read())

        content = result["choices"][0]["message"]["content"]
        return content.strip()

    except urllib.error.URLError as e:
        log.error(f"LLM server not reachable: {e}")
        return None
    except Exception as e:
        log.error(f"Vision description failed for {image_path}: {e}")
        return None


def describe_audio_filename(filepath, context=""):
    """For audio files without vision, describe based on filename and metadata."""
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
            f"{LLM_SERVER}/v1/chat/completions",
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=60)
        result = json.loads(resp.read())
        return result["choices"][0]["message"]["content"].strip()

    except Exception as e:
        log.error(f"Audio description failed for {filepath}: {e}")
        return None

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
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith("._")]

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
# Indexing pipeline
# ---------------------------------------------------------------------------

def index_file(db, filepath, fid, folder_context=""):
    """Process a single media file through the indexing pipeline."""
    filepath = Path(filepath)
    ext = filepath.suffix.lower()
    ftype = get_file_type(ext)

    log.info(f"Indexing [{ftype}]: {filepath.name}")

    try:
        # Stage 1: Metadata
        stat = filepath.stat()
        meta = probe_media(filepath) if ftype in ("video", "audio") else {}
        if meta is None:
            meta = {}

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

        # Stage 2: Get AI description
        description = None
        tags = None

        if ftype == "image":
            description = describe_image(str(filepath), context=folder_context)

        elif ftype == "video":
            # Extract keyframes first
            duration = meta.get("duration", 0)
            frames = extract_keyframes(str(filepath), duration, fid)

            if frames:
                # Describe the first keyframe (representative)
                first_frame_path = frames[0][1]
                description = describe_image(first_frame_path, context=folder_context)

                # Store keyframes
                for ts, thumb_path in frames:
                    kf_id = f"{fid}_{ts:.1f}"
                    db.execute("""
                        INSERT OR REPLACE INTO keyframes (id, file_id, timestamp_seconds, thumbnail_path)
                        VALUES (?, ?, ?, ?)
                    """, (kf_id, fid, ts, thumb_path))

                # Describe additional keyframes concurrently if we have multiple
                if len(frames) > 1:
                    for ts, thumb_path in frames[1:]:
                        kf_desc = describe_image(thumb_path, context=folder_context)
                        if kf_desc:
                            kf_id = f"{fid}_{ts:.1f}"
                            db.execute("""
                                UPDATE keyframes SET ai_description = ? WHERE id = ?
                            """, (kf_desc, kf_id))
            else:
                description = f"Video file: {filepath.name}"

        elif ftype == "audio":
            description = describe_audio_filename(str(filepath), context=folder_context)

        # Generate tags from the description
        if description:
            # Extract simple tags from the path (folder names are informative)
            path_parts = [p for p in filepath.parts if p not in ("/", "Volumes", "Vault")]
            tags = ", ".join(path_parts[:-1])  # folder names as tags

        # Stage 3: Store results
        db.execute("""
            UPDATE files SET
                ai_description = ?, tags = ?,
                indexed_at = ?, status = 'indexed'
            WHERE id = ?
        """, (description, tags, datetime.now().isoformat(), fid))
        db.commit()

        log.info(f"  Done: {filepath.name}")
        return True

    except Exception as e:
        log.error(f"  Error indexing {filepath.name}: {e}")
        db.execute("""
            UPDATE files SET status = 'error', error_message = ? WHERE id = ?
        """, (str(e), fid))
        db.commit()
        return False

# ---------------------------------------------------------------------------
# Main indexer
# ---------------------------------------------------------------------------

class MediaIndexer:
    def __init__(self):
        self.db = init_db()
        self.running = True
        self.stats = {"scanned": 0, "indexed": 0, "errors": 0, "skipped": 0}

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

        log.info(f"Scanning: {fpath}")

        for media_path in crawl_folder(fpath):
            if not self.running:
                break

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
                self.stats["scanned"] += 1

            except (OSError, PermissionError) as e:
                log.warning(f"Cannot access: {media_path}: {e}")
                continue

        self.db.execute("""
            UPDATE folders SET last_scan = ?, file_count = ?
            WHERE id = ?
        """, (datetime.now().isoformat(), count, fid))
        self.db.commit()

        log.info(f"Scan complete: {count} new files found")
        return count

    def _process_one(self, task):
        """Process a single file with its own DB connection (thread-safe)."""
        i, total, fid, fpath, folder_path = task

        context = ""
        if folder_path:
            rel = os.path.relpath(os.path.dirname(fpath), folder_path)
            context = f"{Path(folder_path).name}/{rel}"

        log.info(f"[{i}/{total}] Processing: {Path(fpath).name}")

        if not Path(fpath).exists():
            db = sqlite3.connect(str(DB_PATH))
            db.execute("UPDATE files SET status = 'offline' WHERE id = ?", (fid,))
            db.commit()
            db.close()
            return "offline"

        # Each thread gets its own DB connection
        db = sqlite3.connect(str(DB_PATH))
        db.execute("PRAGMA journal_mode=WAL")
        success = index_file(db, fpath, fid, folder_context=context)

        # Add to ChromaDB for semantic search
        if success and self.chroma_collection is not None:
            row = db.execute(
                "SELECT ai_description, filename, file_type, tags FROM files WHERE id = ?",
                (fid,)
            ).fetchone()
            if row and row[0]:
                try:
                    self.chroma_collection.upsert(
                        ids=[fid],
                        documents=[row[0]],
                        metadatas=[{
                            "filename": row[1] or "",
                            "file_type": row[2] or "",
                            "tags": row[3] or "",
                            "path": fpath
                        }]
                    )
                except Exception as e:
                    log.warning("ChromaDB upsert failed for %s: %s" % (fid, str(e)))

        db.close()
        return "indexed" if success else "error"

    def process_pending(self):
        """Process all pending files with concurrent LLM requests."""
        pending = self.db.execute("""
            SELECT f.id, f.path, fo.path as folder_path
            FROM files f
            LEFT JOIN folders fo ON f.folder_id = fo.id
            WHERE f.status = 'pending'
            ORDER BY f.path
        """).fetchall()

        total = len(pending)
        log.info(f"Processing {total} pending files (workers: {MAX_CONCURRENT_LLM})")

        # Build task list
        tasks = []
        for i, (fid, fpath, folder_path) in enumerate(pending):
            tasks.append((i + 1, total, fid, fpath, folder_path))

        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_LLM) as pool:
            futures = {}
            for task in tasks:
                if not self.running:
                    break
                future = pool.submit(self._process_one, task)
                futures[future] = task

            for future in as_completed(futures):
                if not self.running:
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                result = future.result()
                if result == "indexed":
                    self.stats["indexed"] += 1
                elif result == "error":
                    self.stats["errors"] += 1

        return self.stats

    def get_status(self):
        """Get current indexing status."""
        counts = {}
        for status in ("pending", "indexing", "indexed", "error", "offline"):
            row = self.db.execute(
                "SELECT COUNT(*) FROM files WHERE status = ?", (status,)
            ).fetchone()
            counts[status] = row[0]

        folders = self.db.execute(
            "SELECT path, file_count, last_scan FROM folders WHERE enabled = 1"
        ).fetchall()

        return {"counts": counts, "folders": folders, "stats": self.stats}

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
            safe_query = '"' + query.replace('"', '""') + '"'
            fts_rows = self.db.execute("""
                SELECT f.id FROM files_fts fts
                JOIN files f ON f.rowid = fts.rowid
                WHERE files_fts MATCH ?
                LIMIT ?
            """, (safe_query, limit * 3)).fetchall()

            fts_ids = set(r[0] for r in fts_rows)
            for fid in scored:
                if fid in fts_ids:
                    scored[fid] += 0.3  # boost for exact keyword match

            # Add FTS-only results that ChromaDB missed
            for fid in fts_ids:
                if fid not in scored:
                    scored[fid] = 0.3
        except Exception:
            pass  # FTS might fail on unusual queries

        if not scored:
            return []

        # 3. Sort by combined score and fetch full metadata
        sorted_ids = sorted(scored.keys(), key=lambda x: scored[x], reverse=True)[:limit]

        results = []
        for fid in sorted_ids:
            row = self.db.execute("""
                SELECT path, filename, file_type, ai_description, tags,
                       duration_seconds, width, height
                FROM files WHERE id = ?
            """, (fid,)).fetchone()

            if row:
                results.append({
                    "id": fid,
                    "path": row[0], "filename": row[1], "type": row[2],
                    "description": row[3], "tags": row[4],
                    "duration": row[5], "width": row[6], "height": row[7]
                })

        return results

    def _fts_search(self, query, limit=20):
        """Keyword-only search via SQLite FTS5 (fallback)."""
        try:
            safe_query = '"' + query.replace('"', '""') + '"'
            results = self.db.execute("""
                SELECT f.id, f.path, f.filename, f.file_type, f.ai_description, f.tags,
                       f.duration_seconds, f.width, f.height
                FROM files_fts fts
                JOIN files f ON f.rowid = fts.rowid
                WHERE files_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (safe_query, limit)).fetchall()
        except Exception:
            results = []

        return [{
            "id": r[0],
            "path": r[1], "filename": r[2], "type": r[3],
            "description": r[4], "tags": r[5],
            "duration": r[6], "width": r[7], "height": r[8]
        } for r in results]

    def get_thumbnail(self, file_id):
        """Get or generate a thumbnail for a file. Returns (jpeg_bytes, mime) or (None, None)."""
        # Look up file info
        row = self.db.execute(
            "SELECT path, file_type FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        if not row:
            return None, None

        filepath, file_type = row

        # For videos: use existing extracted keyframe
        if file_type == "video":
            thumb_dir = THUMB_DIR / file_id
            thumb_path = thumb_dir / "frame_00.jpg"
            if thumb_path.exists():
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
        print(f"  {sys.argv[0]} reembed                       — Embed all descriptions into ChromaDB")
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
            indexer.scan_folder(folder)

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

        log.info("Starting continuous indexing (Ctrl+C to stop)")
        log.info(f"Watching: {', '.join(folders)}")
        log.info(f"Rescan interval: {RESCAN_INTERVAL}s")

        while indexer.running:
            for folder in folders:
                if not indexer.running:
                    break
                indexer.scan_folder(folder)

            indexer.process_pending()
            print_status(indexer)

            if indexer.running:
                log.info(f"Sleeping {RESCAN_INTERVAL}s before next scan...")
                for _ in range(RESCAN_INTERVAL):
                    if not indexer.running:
                        break
                    time.sleep(1)

        log.info("Indexer stopped.")

    elif command == "reembed":
        if not HAS_CHROMADB:
            print("ChromaDB not installed. Run: pip3 install chromadb")
            sys.exit(1)
        count = indexer.reembed_all()
        print("Embedded %d descriptions into ChromaDB." % count)

    elif command == "serve":
        from http.server import HTTPServer, BaseHTTPRequestHandler
        import urllib.parse as urlparse

        try:
            serve_port = int(sys.argv[2]) if len(sys.argv) > 2 else 8081
        except ValueError:
            print("Invalid port number: %s" % sys.argv[2])
            sys.exit(1)

        class SearchHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse.urlparse(self.path)
                params = urlparse.parse_qs(parsed.query)

                if parsed.path == "/search":
                    q = params.get("q", [""])[0]
                    try:
                        limit = int(params.get("limit", ["20"])[0])
                    except ValueError:
                        limit = 20
                    limit = max(1, min(limit, 200))
                    if not q:
                        self._json({"error": "Missing ?q= parameter"}, 400)
                        return
                    results = indexer.search(q, limit)
                    self._json({"query": q, "count": len(results), "results": results})

                elif parsed.path == "/status":
                    self._json(indexer.get_status())

                elif parsed.path == "/health":
                    self._json({"status": "ok"})

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

                elif parsed.path == "/folders":
                    rows = indexer.db.execute(
                        "SELECT path, name, file_count, last_scan FROM folders WHERE enabled = 1"
                    ).fetchall()
                    folders_list = [{"path": r[0], "name": r[1], "count": r[2], "last_scan": r[3]} for r in rows]
                    self._json({"folders": folders_list})

                else:
                    self._json({"error": "Not found. Endpoints: /search?q=, /thumbnail?id=, /status, /health, /folders"}, 404)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
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
        log.info(f"Endpoints: /search?q=..., /status, /health, /folders")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log.info("Search API stopped.")

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
