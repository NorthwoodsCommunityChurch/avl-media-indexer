# Job Submission API

Submit files for processing by the server's GPU and CPU workers. API jobs take priority over crawler tasks.

## Submit a Job

```
POST /api/jobs
Content-Type: multipart/form-data
```

### Parameters

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | Yes | The file to process |
| `task_type` | string | Yes | One of: `transcribe`, `visual_analysis`, `face_detect`, `scene_detect` |
| `source_app` | string | No | Identifier for the calling app (e.g., `"midi-automation"`) |

### Task Types

| Type | Worker | GPU | Description |
|------|--------|-----|-------------|
| `transcribe` | Whisper | Pro 580X (port 9090) | Speech-to-text transcription |
| `visual_analysis` | Gemma | RX 580 x2 (ports 8090/8091) | AI image/video description — server distributes across both GPUs |
| `face_detect` | FaceWorker | CPU | Detect and identify faces |
| `scene_detect` | SceneWorker | CPU + VAAPI | Detect scene changes and extract keyframes |

### Example

```bash
curl -X POST \
  -F "file=@sermon.wav" \
  -F "task_type=transcribe" \
  -F "source_app=midi-automation" \
  http://10.10.11.157:8081/api/jobs
```

### Response

```json
{
    "ok": true,
    "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "status": "queued",
    "queue_position": 2
}
```

`queue_position` is the number of API jobs of the same task type ahead of this one (0 = next up).

---

## Check Job Status

```
GET /api/jobs/{job_id}
```

### Example

```bash
curl http://10.10.11.157:8081/api/jobs/a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

### Response

```json
{
    "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "task_type": "transcribe",
    "status": "complete",
    "source_app": "midi-automation",
    "uploaded_filename": "sermon.wav",
    "queue_position": 0,
    "created_at": "2026-03-13T10:30:00",
    "started_at": "2026-03-13T10:30:05",
    "completed_at": "2026-03-13T10:32:15",
    "result": {
        "transcript": "Welcome to this morning's service...",
        "segments": [
            {"start": 0.0, "end": 3.5, "text": "Welcome to this morning's service"},
            {"start": 3.5, "end": 7.2, "text": "Let's open in prayer"}
        ]
    },
    "error_message": null
}
```

### Status Values

| Status | Description |
|--------|-------------|
| `queued` | Waiting for a worker |
| `processing` | Worker is running the task |
| `complete` | Done — `result` field contains output |
| `failed` | Error — `error_message` has details |

### Result Payloads by Task Type

**transcribe:**
```json
{
    "transcript": "full text...",
    "segments": [{"start": 0.0, "end": 3.5, "text": "segment text"}]
}
```

**visual_analysis:**
```json
{
    "description": "A wide shot of a church sanctuary with..."
}
```

**face_detect:**
```json
{
    "faces_found": 3,
    "face_ids": ["abc123", "def456", "ghi789"]
}
```

**scene_detect:**
```json
{
    "scenes": [],
    "keyframe_count": 42
}
```

---

## List Jobs

```
GET /api/jobs
```

### Query Parameters

| Param | Type | Description |
|-------|------|-------------|
| `status` | string | Filter by status: `queued`, `processing`, `complete`, `failed` |
| `task_type` | string | Filter by task type |
| `source_app` | string | Filter by source app |
| `limit` | int | Max results (default 20) |

### Example

```bash
curl "http://10.10.11.157:8081/api/jobs?status=queued&task_type=transcribe"
```

### Response

```json
{
    "jobs": [
        {
            "job_id": "...",
            "task_type": "transcribe",
            "status": "queued",
            "source_app": "midi-automation",
            "uploaded_filename": "sermon.wav",
            "created_at": "2026-03-13T10:30:00"
        }
    ],
    "count": 1
}
```

---

## Queue Overview

```
GET /api/queue
```

Shows aggregate counts across all job types, split by API vs crawler source.

### Example

```bash
curl http://10.10.11.157:8081/api/queue
```

### Response

```json
{
    "api_jobs": {
        "queued": 5,
        "processing": 2,
        "complete": 143,
        "failed": 1
    },
    "by_type": {
        "transcribe": {
            "api_queued": 2,
            "api_processing": 0,
            "crawler_pending": 39
        },
        "visual_analysis": {
            "api_queued": 3,
            "api_processing": 2,
            "crawler_pending": 98744
        },
        "face_detect": {
            "api_queued": 0,
            "api_processing": 0,
            "crawler_pending": 0
        },
        "scene_detect": {
            "api_queued": 0,
            "api_processing": 0,
            "crawler_pending": 34
        }
    }
}
```

---

## Cancel a Job

```
DELETE /api/jobs/{job_id}
```

Only works for jobs with status `queued`. Processing jobs cannot be cancelled.

### Example

```bash
curl -X DELETE http://10.10.11.157:8081/api/jobs/a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

### Response

```json
{
    "ok": true,
    "message": "Job cancelled and cleaned up"
}
```

### Errors

- `400` if job is not in `queued` status
- `404` if job_id not found

---

## Cleanup

Uploaded files for completed jobs are automatically deleted after 24 hours. The `api_jobs` record is kept for history.
