# Search & Browse API

Existing endpoints for searching indexed media, browsing content, and managing faces.

## Search

```
GET /search?q={query}&limit={limit}
```

Full-text search across filenames, AI descriptions, tags, face names, and transcripts.

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | required | Search query |
| `limit` | int | 50 | Max results |

```bash
curl "http://10.10.11.157:8081/search?q=baptism&limit=10"
```

```json
{
    "query": "baptism",
    "count": 3,
    "results": [
        {
            "id": "abc123",
            "path": "/mnt/vault/Videos Vault/2024/baptism-sunday.mp4",
            "filename": "baptism-sunday.mp4",
            "type": "video",
            "description": "A baptism ceremony in a church sanctuary...",
            "tags": "baptism, ceremony, water",
            "duration": 345.6,
            "width": 1920,
            "height": 1080
        }
    ]
}
```

---

## Status

```
GET /status
```

Returns index counts by status and file type.

```json
{
    "counts": {
        "pending": 1234,
        "indexing": 2,
        "indexed": 28396,
        "error": 15,
        "offline": 0,
        "by_type": {
            "video": {"pending": 100, "indexing": 1, "indexed": 5000, "error": 5, "offline": 0},
            "image": {"pending": 1000, "indexing": 1, "indexed": 20000, "error": 10, "offline": 0},
            "audio": {"pending": 134, "indexing": 0, "indexed": 3396, "error": 0, "offline": 0}
        }
    },
    "scanner": {
        "state": "scanning",
        "current_folder": "Videos Vault",
        "files_scanned": 45230,
        "files_new": 123,
        "next_scan_in": null,
        "transcribing": false
    }
}
```

---

## Health

```
GET /health
```

Quick health check. Returns `200` if the server is running.

```json
{"status": "ok"}
```

---

## GPU Status

```
GET /gpu-status
```

Status of all GPU servers (online/processing state).

```json
{
    "servers": [
        {"id": 0, "port": 8090, "online": true, "processing": true},
        {"id": 1, "port": 8091, "online": true, "processing": false},
        {"id": 2, "port": 9090, "online": true, "processing": true}
    ]
}
```

---

## Folders

```
GET /folders
```

List top-level indexed folders and their file counts.

---

## Thumbnail

```
GET /thumbnail?id={file_id}
```

Returns a JPEG thumbnail for the given file. For images, returns a resized version. For videos, returns a keyframe.

---

## Keyframe

```
GET /keyframe?id={file_id}&index={keyframe_index}
```

Returns a specific keyframe image from a video file.

| Param | Type | Description |
|-------|------|-------------|
| `id` | string | File ID |
| `index` | int | Keyframe index (0-based) |

---

## Transcripts

```
GET /transcripts?limit={limit}&offset={offset}
```

List files that have transcripts.

```
GET /transcript?id={file_id}
```

Get the full transcript for a specific file, including segments with timestamps.

---

## Notifications

```
GET /notifications
```

Returns system notifications (alerts, warnings).

```json
{
    "notifications": [
        {
            "id": 1,
            "severity": "warning",
            "title": "Disk usage above 80%",
            "message": "...",
            "created_at": "2026-03-13T10:00:00",
            "read": false
        }
    ],
    "unread_count": 1
}
```

```
POST /notifications/mark-read
```

Marks all notifications as read.

---

## Face Endpoints

### Face Status

```
GET /faces/status
```

```json
{
    "total_faces": 15234,
    "clustered_faces": 14800,
    "named_faces": 12000,
    "unnamed_clusters": 45,
    "named_persons": 12,
    "files_with_faces": 8500,
    "files_without_face_scan": 200,
    "face_recognition_available": true
}
```

### Face Clusters

```
GET /faces/clusters
GET /faces/clusters?show=ignored
```

Returns face clusters with sample thumbnails.

```json
{
    "clusters": [
        {
            "cluster_id": 1,
            "person_id": "person_abc",
            "person_name": "John Smith",
            "face_count": 234,
            "sample_faces": [
                {"face_id": "f1", "thumbnail": "/faces/thumbnail?id=f1"}
            ]
        }
    ]
}
```

### Face Detect

```
POST /faces/detect
```

Starts a face detection scan on unscanned files. Returns immediately; poll progress with the endpoint below.

### Face Detect Progress

```
GET /faces/detect/progress
```

```json
{
    "running": true,
    "processed": 150,
    "total": 200,
    "faces_found": 45,
    "current_file": "photo_2024.jpg"
}
```

### Face Cluster

```
POST /faces/cluster
Content-Type: application/json

{"tolerance": 0.5}
```

Runs clustering on detected faces.

### Face Assign

```
POST /faces/assign
```

Auto-assigns unclustered faces to known persons.

### Face Name

```
POST /faces/name
Content-Type: application/json

{"cluster_id": 5, "name": "John Smith"}
```

Names an unnamed cluster, creating a new person.

### Face Rename

```
POST /faces/rename
Content-Type: application/json

{"person_id": "person_abc", "name": "Jonathan Smith"}
```

Renames an existing person.

### Face Merge

```
POST /faces/merge
Content-Type: application/json

{"source_cluster_id": 3, "target_cluster_id": 7}
```

Merges two clusters together.

### Face Ignore

```
POST /faces/ignore
Content-Type: application/json

{"cluster_id": 5}
```

Hides a cluster from the default view.

### Face Unignore

```
POST /faces/unignore
Content-Type: application/json

{"cluster_id": 5}
```

Restores a hidden cluster.
