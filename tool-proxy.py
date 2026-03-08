#!/usr/bin/env python3
"""
Tool-Calling Proxy for Gemma 3 12B
Translates OpenAI function-calling API into plain-text prompts that Gemma
can understand, then parses Gemma's response back into OpenAI tool_calls format.

Sits between Continue (VS Code) and llama.cpp:
  Continue → localhost:8083 → 10.10.11.157:8080 (Gemma)

No external dependencies — uses only Python 3 standard library.

Usage:
  python3 tool-proxy.py                    # default ports
  python3 tool-proxy.py --port 8083        # custom local port
  python3 tool-proxy.py --backend http://10.10.11.157:8080  # custom backend
"""

import json
import re
import sys
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTEN_PORT = 8083
BACKEND_URL = "http://10.10.11.157:8080"
DEBUG = True  # Set to False to silence logging

def log(msg):
    if DEBUG:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Tool call injection and parsing
# ---------------------------------------------------------------------------

TOOL_SYSTEM_PROMPT = """You have access to external tools. When the user's question requires using a tool, respond ONLY with a JSON tool call in this exact format (no other text):

<tool_call>
{"name": "TOOL_NAME", "arguments": {"arg1": "value1"}}
</tool_call>

If you don't need a tool, respond normally with plain text.

Available tools:
"""

def format_tools_for_prompt(tools):
    """Convert OpenAI tool definitions into a text description."""
    lines = []
    for tool in tools:
        func = tool.get("function", tool)
        name = func.get("name", "")
        desc = func.get("description", "")
        params = func.get("parameters", {}).get("properties", {})
        required = func.get("parameters", {}).get("required", [])

        param_parts = []
        for pname, pinfo in params.items():
            req = " (required)" if pname in required else ""
            param_parts.append(f"    - {pname}: {pinfo.get('type', 'string')} — {pinfo.get('description', '')}{req}")

        lines.append(f"- {name}: {desc}")
        if param_parts:
            lines.append("  Parameters:")
            lines.extend(param_parts)

    return "\n".join(lines)


def inject_tools_into_messages(messages, tools):
    """Add tool descriptions to the system prompt."""
    tool_text = TOOL_SYSTEM_PROMPT + format_tools_for_prompt(tools)

    # Find or create system message
    has_system = False
    new_messages = []
    for msg in messages:
        if msg.get("role") == "system":
            msg = dict(msg)
            msg["content"] = tool_text + "\n\n" + msg.get("content", "")
            has_system = True
        new_messages.append(msg)

    if not has_system:
        new_messages.insert(0, {"role": "system", "content": tool_text})

    return new_messages


def format_tool_result_message(messages):
    """Convert tool result messages into user messages Gemma understands."""
    new_messages = []
    for msg in messages:
        if msg.get("role") == "tool":
            # Convert tool result to a user message
            tool_name = msg.get("name", "tool")
            content = msg.get("content", "")
            new_messages.append({
                "role": "user",
                "content": f"Tool result from {tool_name}:\n{content}\n\nNow answer the original question using this information."
            })
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Convert assistant tool_call message to plain text
            tc = msg["tool_calls"][0]["function"]
            new_messages.append({
                "role": "assistant",
                "content": f'<tool_call>\n{{"name": "{tc["name"]}", "arguments": {tc["arguments"]}}}\n</tool_call>'
            })
        else:
            new_messages.append(msg)
    return new_messages


TOOL_CALL_PATTERN = re.compile(
    r'<tool_call>\s*(\{.*?\})\s*</tool_call>',
    re.DOTALL
)

# Match ```tool_call ... ``` format (Gemma uses this)
BACKTICK_PATTERN = re.compile(
    r'```tool_call\s*(\{.*?\})\s*```',
    re.DOTALL
)

# Also match raw JSON tool calls without tags
RAW_JSON_PATTERN = re.compile(
    r'^\s*\{\s*"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}\s*$',
    re.DOTALL
)


def parse_tool_call(content):
    """Check if the model's response is a tool call. Returns (name, args) or None."""
    if not content:
        return None

    # Try tagged format first: <tool_call>{...}</tool_call>
    match = TOOL_CALL_PATTERN.search(content)
    if match:
        try:
            call = json.loads(match.group(1))
            return (call.get("name"), json.dumps(call.get("arguments", {})))
        except json.JSONDecodeError:
            pass

    # Try backtick format: ```tool_call {...} ```
    match = BACKTICK_PATTERN.search(content)
    if match:
        try:
            call = json.loads(match.group(1))
            return (call.get("name"), json.dumps(call.get("arguments", {})))
        except json.JSONDecodeError:
            pass

    # Try raw JSON format
    match = RAW_JSON_PATTERN.match(content.strip())
    if match:
        try:
            name = match.group(1)
            args = match.group(2)
            json.loads(args)  # validate
            return (name, args)
        except json.JSONDecodeError:
            pass

    return None

# ---------------------------------------------------------------------------
# HTTP Proxy
# ---------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Read request body
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        if self.path == "/v1/chat/completions":
            self._handle_chat(body)
        else:
            # Pass through other endpoints unchanged
            self._proxy_raw(self.path, body)

    def do_GET(self):
        # Pass through GET requests (health, models, etc.)
        self._proxy_get(self.path)

    def _handle_chat(self, body):
        """Handle chat completion with tool injection."""
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self._error(400, "Invalid JSON")
            return

        tools = req.pop("tools", None)
        req.pop("tool_choice", None)

        # Force non-streaming so we can parse tool calls from the full response
        was_streaming = req.get("stream", False)
        req["stream"] = False

        log(f"Chat request: stream={was_streaming}→False, tools={len(tools) if tools else 0}")

        messages = req.get("messages", [])

        # Log message roles for debugging
        roles = [m.get("role", "?") for m in messages]
        log(f"  Messages: {roles}")

        # Convert any tool result messages
        messages = format_tool_result_message(messages)

        # Inject tool descriptions if tools were provided
        if tools:
            messages = inject_tools_into_messages(messages, tools)
            tool_names = [t.get("function", t).get("name", "?") for t in tools]
            log(f"  Injected tools: {tool_names}")

        req["messages"] = messages

        # Forward to backend
        result = self._proxy_post("/v1/chat/completions", req)
        if result is None:
            return

        # Check if the response contains a tool call
        if tools:
            try:
                data = json.loads(result)
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                log(f"  Gemma response: {content[:200]}...")
                tool_call = parse_tool_call(content)

                if tool_call:
                    name, arguments = tool_call
                    log(f"  TOOL CALL DETECTED: {name}({arguments})")
                    # Rewrite response to OpenAI tool_calls format
                    data["choices"][0]["message"] = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": f"call_{name}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments
                            }
                        }]
                    }
                    data["choices"][0]["finish_reason"] = "tool_calls"
                    result = json.dumps(data).encode()
                else:
                    log(f"  No tool call detected in response")

            except (json.JSONDecodeError, KeyError, IndexError) as e:
                log(f"  Parse error: {e}")

        self._send(200, result)

    def _proxy_post(self, path, data):
        """Forward a POST request to the backend."""
        try:
            body = json.dumps(data).encode()
            req = urllib.request.Request(
                BACKEND_URL + path,
                data=body,
                headers={"Content-Type": "application/json"}
            )
            resp = urllib.request.urlopen(req, timeout=120)
            return resp.read()
        except urllib.error.URLError as e:
            self._error(502, f"Backend unreachable: {e}")
            return None
        except Exception as e:
            self._error(500, str(e))
            return None

    def _proxy_get(self, path):
        """Forward a GET request to the backend."""
        try:
            req = urllib.request.Request(BACKEND_URL + path)
            resp = urllib.request.urlopen(req, timeout=10)
            self._send(200, resp.read())
        except urllib.error.URLError as e:
            self._error(502, f"Backend unreachable: {e}")
        except Exception as e:
            self._error(500, str(e))

    def _proxy_raw(self, path, body):
        """Forward a POST request raw to the backend."""
        try:
            req = urllib.request.Request(
                BACKEND_URL + path,
                data=body,
                headers={"Content-Type": "application/json"}
            )
            resp = urllib.request.urlopen(req, timeout=120)
            self._send(200, resp.read())
        except urllib.error.URLError as e:
            self._error(502, f"Backend unreachable: {e}")
        except Exception as e:
            self._error(500, str(e))

    def _send(self, code, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, json.dumps({"error": message}))

    def log_message(self, fmt, *args):
        if DEBUG:
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global BACKEND_URL
    port = LISTEN_PORT
    backend = BACKEND_URL

    # Simple arg parsing
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--backend" and i + 1 < len(args):
            backend = args[i + 1]
            i += 2
        else:
            i += 1

    BACKEND_URL = backend

    server = HTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"Tool-calling proxy running on http://0.0.0.0:{port}")
    print(f"Backend: {BACKEND_URL}")
    print(f"Configure Continue to point to: http://localhost:{port}/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nProxy stopped.")


if __name__ == "__main__":
    main()
