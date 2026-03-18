# Worker Status API

Real-time status of all GPU and CPU workers, including what they're currently processing and queue depths split by API vs crawler source.

## GET /worker-status

```bash
curl http://10.10.11.157:8081/worker-status
```

### Response

```json
{
    "gpus": [
        {
            "name": "Gemma0",
            "port": 8090,
            "model": "Gemma 3 12B Q3_K_S",
            "online": true,
            "processing": true,
            "current_task": {
                "source": "crawler",
                "file": "interview.mp4",
                "task_type": "visual_analysis"
            },
            "queue": {
                "api": 3,
                "crawler": 98744
            }
        },
        {
            "name": "Gemma1",
            "port": 8091,
            "model": "Gemma 3 12B Q3_K_S",
            "online": true,
            "processing": true,
            "current_task": {
                "source": "api",
                "file": "uploaded_photo.jpg",
                "task_type": "visual_analysis"
            },
            "queue": {
                "api": 3,
                "crawler": 98744
            }
        },
        {
            "name": "Whisper",
            "port": 9090,
            "model": "Whisper large-v3-turbo",
            "online": true,
            "processing": true,
            "current_task": {
                "source": "api",
                "file": "sermon.wav",
                "task_type": "transcribe"
            },
            "queue": {
                "api": 2,
                "crawler": 39
            }
        }
    ],
    "cpu_workers": {
        "face_detect": {
            "processing": true,
            "current_task": {
                "source": "crawler",
                "file": "photo.jpg",
                "task_type": "face_detect"
            },
            "queue": {
                "api": 0,
                "crawler": 0
            }
        },
        "scene_detect": {
            "workers": 3,
            "active": 2,
            "queue": {
                "api": 0,
                "crawler": 34
            }
        }
    },
    "crawler": {
        "state": "scanning",
        "current_folder": "Videos Vault"
    }
}
```

### Fields

#### GPUs (`gpus[]`)

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Worker name (Gemma0, Gemma1, Whisper) |
| `port` | int | LLM server port (8090, 8091, 9090) |
| `model` | string | Model loaded on this GPU |
| `online` | bool | Whether the LLM server is responding |
| `processing` | bool | Whether it's currently working on a task |
| `current_task` | object/null | What it's working on right now |
| `current_task.source` | string | `"api"` or `"crawler"` |
| `current_task.file` | string | Filename being processed |
| `current_task.task_type` | string | Task type being run |
| `queue` | object | Pending tasks for this worker's task type |
| `queue.api` | int | API jobs waiting |
| `queue.crawler` | int | Crawler tasks waiting |

Both Gemma workers share the same queue (visual_analysis), so their `queue` counts are identical.

#### CPU Workers (`cpu_workers`)

**face_detect:**

| Field | Type | Description |
|-------|------|-------------|
| `processing` | bool | Whether face detection is active |
| `current_task` | object/null | Current task details |
| `queue` | object | Pending face_detect tasks (api/crawler) |

**scene_detect:**

| Field | Type | Description |
|-------|------|-------------|
| `workers` | int | Total scene detection worker count |
| `active` | int | Currently active workers |
| `queue` | object | Pending scene_detect tasks (api/crawler) |

#### Crawler

| Field | Type | Description |
|-------|------|-------------|
| `state` | string | `"scanning"`, `"processing"`, `"sleeping"`, or `"idle"` |
| `current_folder` | string | Folder currently being scanned |

### Notes

- GPU servers bind to `127.0.0.1` on the server — they're not directly reachable from the network. This endpoint proxies their status.
- Queue counts come from the SQLite task queue, not the LLM servers themselves.
- `current_task` is `null` when a worker is idle or between tasks.
