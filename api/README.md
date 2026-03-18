# Media Indexer API

Base URL: `http://10.10.11.157:8081`

## Authentication

None. The API is accessible only on the local network.

## Error Format

All endpoints return JSON. Errors use HTTP status codes:

- `200` — Success
- `400` — Bad request (missing parameters, invalid input)
- `404` — Resource not found
- `405` — Method not allowed
- `500` — Server error

Error response body:

```json
{
    "error": "description of what went wrong"
}
```

## Endpoints Overview

| Method | Path | Description | Docs |
|--------|------|-------------|------|
| **Job Submission API** | | | [jobs.md](jobs.md) |
| POST | `/api/jobs` | Submit a file for processing | [jobs.md](jobs.md#submit-a-job) |
| GET | `/api/jobs` | List submitted jobs | [jobs.md](jobs.md#list-jobs) |
| GET | `/api/jobs/{id}` | Check job status | [jobs.md](jobs.md#check-job-status) |
| DELETE | `/api/jobs/{id}` | Cancel a queued job | [jobs.md](jobs.md#cancel-a-job) |
| GET | `/api/queue` | Queue overview | [jobs.md](jobs.md#queue-overview) |
| **Worker Status** | | | [workers.md](workers.md) |
| GET | `/worker-status` | GPU/CPU worker status | [workers.md](workers.md#get-worker-status) |
| **Search & Browse** | | | [search.md](search.md) |
| GET | `/search` | Full-text search | [search.md](search.md#search) |
| GET | `/status` | Index counts | [search.md](search.md#status) |
| GET | `/health` | Health check | [search.md](search.md#health) |
| GET | `/gpu-status` | GPU server status | [search.md](search.md#gpu-status) |
| GET | `/folders` | Browse indexed folders | [search.md](search.md#folders) |
| GET | `/thumbnail` | Get file thumbnail | [search.md](search.md#thumbnail) |
| GET | `/keyframe` | Get video keyframe | [search.md](search.md#keyframe) |
| GET | `/transcripts` | List transcribed files | [search.md](search.md#transcripts) |
| GET | `/transcript` | Get transcript for a file | [search.md](search.md#transcript) |
| GET | `/notifications` | Get system notifications | [search.md](search.md#notifications) |
| POST | `/notifications/mark-read` | Mark notifications read | [search.md](search.md#mark-notifications-read) |
| **Face Management** | | | [search.md](search.md#face-endpoints) |
| GET | `/faces/status` | Face detection stats | [search.md](search.md#face-status) |
| GET | `/faces/clusters` | List face clusters | [search.md](search.md#face-clusters) |
| POST | `/faces/detect` | Start face detection scan | [search.md](search.md#face-detect) |
| GET | `/faces/detect/progress` | Detection progress | [search.md](search.md#face-detect-progress) |
| POST | `/faces/cluster` | Run clustering | [search.md](search.md#face-cluster) |
| POST | `/faces/assign` | Auto-assign faces | [search.md](search.md#face-assign) |
| POST | `/faces/name` | Name a cluster | [search.md](search.md#face-name) |
| POST | `/faces/rename` | Rename a person | [search.md](search.md#face-rename) |
| POST | `/faces/merge` | Merge two clusters | [search.md](search.md#face-merge) |
| POST | `/faces/ignore` | Hide a cluster | [search.md](search.md#face-ignore) |
| POST | `/faces/unignore` | Unhide a cluster | [search.md](search.md#face-unignore) |

## Priority System

Jobs submitted via `/api/jobs` take priority over crawler-discovered tasks. When a worker finishes its current task, it picks up any pending API job before returning to crawler work. Multiple API jobs are processed in submission order (FIFO).
