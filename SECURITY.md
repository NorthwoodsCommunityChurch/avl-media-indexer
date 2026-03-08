# Security Findings - LLM Server

**Review Date**: 2026-03-01
**Reviewer**: Claude Security Review (Clara)
**Severity Summary**: 0 Critical, 1 High, 2 Medium, 1 Low

## Findings

| ID | Severity | Finding | File | Line | Status |
|----|----------|---------|------|------|--------|
| LLM-01 | HIGH | All services bind to 0.0.0.0 without authentication | Multiple | - | Open |
| LLM-02 | MEDIUM | CORS wildcard allows any origin to access APIs | media-indexer | - | Open |
| LLM-03 | MEDIUM | File paths in search results could expose directory structure | media-indexer | - | Open |
| LLM-04 | LOW | Hardcoded binary paths assume specific installation | media-indexer | - | Open |

## Detailed Findings

### LLM-01 [HIGH] All services bind to 0.0.0.0 without authentication

**Location**: Ports 8080, 8081, 8083
**Description**: Three services (llama.cpp LLM API, media-indexer search/thumbnail API, tool-proxy) all bind to `0.0.0.0` on fixed ports with no authentication. Any device on the network can query the LLM, search media metadata, and access thumbnails.
**Impact**: On a trusted production network this is acceptable by design, but if the Mac is connected to an untrusted network (e.g., guest WiFi), all services are fully exposed. The LLM API could be used for arbitrary inference, consuming GPU resources.
**Remediation**: This is documented as intentional for the trusted local network. Consider binding to a specific interface or adding optional token-based auth for defense-in-depth. Firewall rules should restrict access to known machines.

### LLM-02 [MEDIUM] CORS wildcard allows any origin to access APIs

**Location**: media-indexer HTTP headers
**Description**: The media-indexer API returns `Access-Control-Allow-Origin: *`, allowing any website to make cross-origin requests to the API if the user visits a malicious page.
**Impact**: A malicious website visited by someone on the same network could query the search API and retrieve media metadata and thumbnails.
**Remediation**: Restrict CORS to specific origins (e.g., `localhost:*` or specific production machine hostnames).

### LLM-03 [MEDIUM] File paths in search results could expose directory structure

**Location**: media-indexer search API
**Description**: Search results return full NAS file paths, exposing the directory structure of the media library.
**Impact**: Reveals internal file organization. Low sensitivity for a church media library, but provides information useful for further exploitation if combined with other vulnerabilities.
**Remediation**: Consider returning relative paths or path IDs instead of full filesystem paths.

### LLM-04 [LOW] Hardcoded binary paths assume specific installation

**Location**: media-indexer subprocess calls
**Description**: Binary paths for ffmpeg/ffprobe are hardcoded to `/usr/local/bin/`. If these binaries were replaced (e.g., via a compromised install), the indexer would execute the replacements.
**Impact**: Low - requires local access to replace system binaries, which implies the machine is already compromised.
**Remediation**: No action needed. The subprocess calls use list arguments (not shell strings), preventing injection.

## Security Posture Assessment

**Overall Risk: MEDIUM**

The LLM Server has a deliberately open architecture designed for a trusted local network. Credentials are properly stored in macOS Keychain (not in code), subprocess calls use safe list-based arguments, and SQL injection is prevented via proper FTS5 escaping. The main risks are the unauthenticated services on all interfaces and the CORS wildcard. These are acceptable trade-offs for the intended deployment environment but should be documented and firewalled.

## Remediation Priority

1. LLM-01 - Add firewall rules to restrict port access
2. LLM-02 - Restrict CORS to specific origins
3. LLM-03 - Consider using relative paths in API responses
4. LLM-04 - No action needed

---

## Intended Use

This project is designed for **local network use only** within the Northwoods church production infrastructure. It is NOT intended to be exposed to the internet.

## Network Exposure

| Port | Service | Binding | Description |
|------|---------|---------|-------------|
| 8080 | llama.cpp | 0.0.0.0 | LLM inference API (OpenAI-compatible) |
| 8081 | media-indexer | 0.0.0.0 | Search API, thumbnail serving, status |
| 8083 | tool-proxy | 0.0.0.0 | Tool-calling proxy (runs on dev Mac) |

All services bind to `0.0.0.0` (all interfaces) to allow access from other machines on the local network.

## What Is Exposed

- Search results (file paths, AI descriptions, folder tags)
- Thumbnail images (resized keyframes and image previews)
- Indexing status (file counts, folder paths, processing stats)
- System health check

## What Is NOT Exposed

- NAS credentials (stored in macOS Keychain, not in code)
- Full file contents (only metadata and thumbnails)
- Write access (all endpoints are read-only)
- No ability to modify, delete, or upload files

## Authentication

**None.** This is intentional for a trusted local network environment. Adding authentication would add complexity without meaningful security benefit on an isolated production network.

## Data Storage

- **SQLite database** (`~/media-index/index.db`): Contains file paths, AI-generated descriptions, metadata, and indexing status. Uses WAL mode for concurrent access.
- **Thumbnails** (`~/media-index/thumbnails/`): Cached JPEG thumbnails generated from source media via ffmpeg.
- **ChromaDB** (`~/media-index/chroma/`): Optional vector embeddings for semantic search. Contains text embeddings of AI descriptions.
- **Logs** (`~/media-index/indexer.log`): Processing logs with file paths and status messages.

No credentials, tokens, or secrets are stored in any of these locations.

## Subprocess Execution

The indexer calls `ffmpeg` and `ffprobe` for media processing:
- Arguments are constructed as Python lists (not shell strings), preventing shell injection
- File paths come from filesystem crawling (`os.walk`), not from HTTP request parameters
- All subprocess calls have timeouts (30-60 seconds) to prevent hangs
- Binary paths are hardcoded to `/usr/local/bin/` (static ffmpeg builds)

## Security Review Summary

| Category | Status | Notes |
|----------|--------|-------|
| Hardcoded secrets | Pass | No credentials in source code |
| SQL injection | Pass | FTS5 queries properly escaped with quote wrapping |
| Path traversal | Pass | Thumbnail IDs are SHA-256 hash prefixes, not user paths |
| Command injection | Pass | Subprocess uses list args, no user input in commands |
| Input validation | Fixed | Query limit parameter capped at 200 |
| CORS | Documented | Wildcard (`*`) - acceptable for local network |
| HTTPS | Not implemented | HTTP only - acceptable for trusted LAN |
| Rate limiting | Not implemented | Acceptable for local single-user environment |
| Authentication | Not implemented | Intentional for trusted local network |

## Recommendations

1. **Network isolation**: Keep the Mac Pro on a separate VLAN or behind a firewall if possible
2. **Do not expose to internet**: These services have no authentication and should never be port-forwarded or made publicly accessible
3. **Firewall rules**: Consider restricting ports 8080-8083 to known production machines only

## Reporting a Vulnerability

If you discover a security issue, please report it via [GitHub private vulnerability reporting](https://github.com/NorthwoodsCommunityChurch/avl-media-indexer/security/advisories/new).
