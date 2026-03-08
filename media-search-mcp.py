#!/usr/bin/env python3
"""
MCP Server for Vault Media Search
Exposes the media index database to Continue (VS Code) and other MCP clients.

Connects to the search API running on the Mac Pro (media-indexer.py serve).
No external dependencies — uses only Python 3 standard library.

Usage:
  python3 media-search-mcp.py

Configure in Continue's config.yaml:
  mcpServers:
    - name: vault-media
      command: python3
      args: ["/path/to/media-search-mcp.py"]
"""

import json
import sys
import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEARCH_API = "http://10.10.11.157:8081"
SERVER_NAME = "vault-media-search"
SERVER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def api_get(path):
    """Call the search API on the Mac Pro."""
    try:
        url = SEARCH_API + path
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except urllib.error.URLError as e:
        return {"error": f"Cannot reach search API at {SEARCH_API}: {e}"}
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------------------
# MCP Protocol (JSON-RPC 2.0 over stdio)
# ---------------------------------------------------------------------------

def read_message():
    """Read a JSON-RPC message from stdin (Content-Length framing)."""
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None  # EOF
        line = line.decode("utf-8").rstrip("\r\n")
        if line == "":
            break  # End of headers
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip()] = value.strip()

    cl = headers.get("Content-Length", "")
    if not cl:
        return None
    length = int(cl)
    if length == 0:
        return None

    body = sys.stdin.buffer.read(length)
    if not body:
        return None
    return json.loads(body)


def write_message(msg):
    """Write a JSON-RPC message to stdout (Content-Length framing)."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n"
    sys.stdout.buffer.write(header.encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def response(id, result):
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": id, "result": result}


def error_response(id, code, message):
    """Build a JSON-RPC error response."""
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "search_media",
        "description": (
            "Search the Northwoods vault media database. Finds videos, images, and audio "
            "by AI-generated descriptions, filenames, or folder tags. The vault contains "
            "church production media: worship services, events, stock footage, graphics, "
            "and audio recordings. Use natural language queries like 'sunset timelapse', "
            "'baptism video', 'Easter 2024 worship', or 'drone aerial shot'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms (natural language or keywords). Uses full-text search across AI descriptions, filenames, and folder tags."
                },
                "limit": {
                    "type": "number",
                    "description": "Max results to return (default 10, max 50)"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "media_status",
        "description": (
            "Get the current status of the media indexer — how many files are indexed, "
            "pending, errored, and which folders are being tracked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "list_indexed_folders",
        "description": "List all folders currently being tracked by the media indexer.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "search_person_media",
        "description": (
            "Search for media containing a specific person by name. Uses face recognition "
            "data to find images and videos where the named person appears. Can combine "
            "person name with scene descriptions, e.g., 'Jon Smith red shirt outdoor'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Person name, optionally with scene keywords (e.g., 'Jon Smith red shirt')"
                },
                "limit": {
                    "type": "number",
                    "description": "Max results to return (default 10, max 50)"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "list_known_persons",
        "description": "List all persons identified in the media vault via face recognition.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "face_recognition_status",
        "description": "Get face recognition statistics: faces detected, clustered, named, and persons identified.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name, arguments):
    """Execute a tool and return the result as text content."""
    if name == "search_media":
        query = arguments.get("query", "")
        limit = min(int(arguments.get("limit", 10)), 50)

        data = api_get(f"/search?q={urllib.request.quote(query)}&limit={limit}")

        if "error" in data:
            return f"Error: {data['error']}"

        results = data.get("results", [])
        if not results:
            return f"No media found matching: {query}"

        lines = [f"Found {data.get('count', len(results))} results for \"{query}\":\n"]
        for r in results:
            lines.append(f"  [{r['type']}] {r['filename']}")
            if r.get('description'):
                desc = r['description'][:300]
                lines.append(f"    {desc}")
            if r.get('duration'):
                mins = int(r['duration'] // 60)
                secs = int(r['duration'] % 60)
                lines.append(f"    Duration: {mins}:{secs:02d}")
            if r.get('width') and r.get('height'):
                lines.append(f"    Resolution: {r['width']}x{r['height']}")
            lines.append(f"    Path: {r['path']}")
            lines.append("")

        return "\n".join(lines)

    elif name == "media_status":
        data = api_get("/status")

        if "error" in data:
            return f"Error: {data['error']}"

        counts = data.get("counts", {})
        folders = data.get("folders", [])

        lines = ["Media Indexer Status:"]
        lines.append(f"  Indexed:  {counts.get('indexed', 0)}")
        lines.append(f"  Pending:  {counts.get('pending', 0)}")
        lines.append(f"  Indexing: {counts.get('indexing', 0)}")
        lines.append(f"  Errors:   {counts.get('error', 0)}")
        lines.append(f"  Offline:  {counts.get('offline', 0)}")
        lines.append(f"\nTracked Folders:")
        for path, count, last_scan in folders:
            lines.append(f"  {path} ({count} files, last scan: {last_scan or 'never'})")

        return "\n".join(lines)

    elif name == "list_indexed_folders":
        data = api_get("/folders")

        if "error" in data:
            return f"Error: {data['error']}"

        folders = data.get("folders", [])
        if not folders:
            return "No folders are being tracked yet."

        lines = ["Indexed Folders:"]
        for f in folders:
            lines.append(f"  {f['name']} — {f['path']}")
            lines.append(f"    Files: {f['count']}, Last scan: {f.get('last_scan', 'never')}")
        return "\n".join(lines)

    elif name == "search_person_media":
        # Person names are stored in face_names column which is in the FTS index,
        # so regular search naturally finds them
        query = arguments.get("query", "")
        limit = min(int(arguments.get("limit", 10)), 50)

        data = api_get(f"/search?q={urllib.request.quote(query)}&limit={limit}")

        if "error" in data:
            return f"Error: {data['error']}"

        results = data.get("results", [])
        if not results:
            return f"No media found matching: {query}"

        lines = [f"Found {data.get('count', len(results))} results for \"{query}\":\n"]
        for r in results:
            lines.append(f"  [{r['type']}] {r['filename']}")
            if r.get('description'):
                desc = r['description'][:300]
                lines.append(f"    {desc}")
            if r.get('duration'):
                mins = int(r['duration'] // 60)
                secs = int(r['duration'] % 60)
                lines.append(f"    Duration: {mins}:{secs:02d}")
            lines.append(f"    Path: {r['path']}")
            lines.append("")

        return "\n".join(lines)

    elif name == "list_known_persons":
        data = api_get("/faces/persons")

        if "error" in data:
            return f"Error: {data['error']}"

        persons = data.get("persons", [])
        if not persons:
            return "No named persons yet. Use the face management UI to name face clusters."

        lines = ["Known Persons:"]
        for p in persons:
            lines.append(f"  {p['name']} ({p['face_count']} faces)")
        return "\n".join(lines)

    elif name == "face_recognition_status":
        data = api_get("/faces/status")

        if "error" in data:
            return f"Error: {data['error']}"

        lines = [
            "Face Recognition Status:",
            f"  Total faces detected:   {data.get('total_faces', 0)}",
            f"  Clustered:              {data.get('clustered_faces', 0)}",
            f"  Named:                  {data.get('named_faces', 0)}",
            f"  Named persons:          {data.get('named_persons', 0)}",
            f"  Unnamed clusters:       {data.get('unnamed_clusters', 0)}",
            f"  Files with faces:       {data.get('files_with_faces', 0)}",
            f"  Files not yet scanned:  {data.get('files_without_face_scan', 0)}",
            f"  Face recognition lib:   {'available' if data.get('face_recognition_available') else 'not installed'}"
        ]
        return "\n".join(lines)

    else:
        return f"Unknown tool: {name}"

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    try:
        while True:
            msg = read_message()
            if msg is None:
                break

            method = msg.get("method", "")
            msg_id = msg.get("id")
            params = msg.get("params", {})

            if method == "initialize":
                write_message(response(msg_id, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION
                    }
                }))

            elif method == "notifications/initialized":
                pass  # No response needed for notifications

            elif method == "tools/list":
                write_message(response(msg_id, {"tools": TOOLS}))

            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                result_text = execute_tool(tool_name, arguments)
                write_message(response(msg_id, {
                    "content": [{"type": "text", "text": result_text}]
                }))

            elif method == "ping":
                write_message(response(msg_id, {}))

            elif msg_id is not None:
                write_message(error_response(msg_id, -32601, f"Method not found: {method}"))
    except (EOFError, BrokenPipeError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
