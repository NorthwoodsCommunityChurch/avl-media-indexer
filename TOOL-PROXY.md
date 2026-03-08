# Tool-Calling Proxy

Gemma 3 12B does not support the OpenAI `tools` API natively. The tool-calling proxy (`tool-proxy.py`) bridges this gap so Continue's MCP tools work with Gemma.

## How It Works
```
Continue (VS Code) → localhost:8083 → 10.10.11.157:8090 (Gemma GPU1)
```

1. **Intercepts** the `tools` parameter from Continue's chat request
2. **Injects** tool descriptions into the system prompt as plain text
3. **Forwards** the modified request to Gemma (without the `tools` field)
4. **Parses** Gemma's text response for tool call patterns
5. **Rewrites** the response to OpenAI `tool_calls` format if a tool call was detected

## Supported Response Formats
The proxy recognizes three patterns Gemma might use:
- XML tags: `<tool_call>{"name": "...", "arguments": {...}}</tool_call>`
- Backtick blocks: `` ```tool_call {"name": "...", "arguments": {...}} ``` `` (Gemma prefers this)
- Raw JSON: `{"name": "...", "arguments": {...}}`

## Running the Proxy
```bash
# Default (port 8083, backend at Mac Pro)
python3 tool-proxy.py

# Custom ports
python3 tool-proxy.py --port 8083 --backend http://10.10.11.157:8090
```

## Continue Config
Continue must point to the proxy, not directly to Gemma:
```yaml
models:
  - name: Gemma 3 12B (Vision)
    provider: openai
    model: gemma-3-12b
    apiBase: http://localhost:8083/v1  # proxy, NOT direct
    apiKey: none
```

## Key Files
- `tool-proxy.py` — proxy server (in LLM Server project, runs on dev Mac)
- No external dependencies — Python 3 stdlib only

## Limitations
- Non-streaming only (waits for full response to parse tool calls)
- Single tool call per response (no parallel tool calling)
- Gemma may occasionally respond with plain text instead of a tool call
