# HTTP Inspector MCP Server

Streamable HTTP MCP server with web panel and screenshot capability.  
Single process, single port — all-in-one HTTP traffic inspection toolkit.

## Features

- **Send arbitrary HTTP requests** — any method, custom headers/body, proxy, timeout (like Burp Repeater)
- **Web Panel** — real-time traffic history table with SSE, raw HTTP detail view, manual sender tab
- **Screenshot** — AI can call `screenshot_panel` to capture the web panel in PNG (base64)
- **Streamable HTTP** — MCP clients connect via `http://localhost:9876/mcp`

## Quick Start

```bash
# Install
cd mcp-http-inspector
pip install -e .
playwright install chromium  # or use local Chrome

# Run
python server.py
```

Open `http://localhost:9876` for the web panel.

## MCP Client Configuration

```json
{
  "mcpServers": {
    "http-inspector": {
      "url": "http://localhost:9876/mcp"
    }
  }
}
```

## MCP Tools

| Tool | Description |
|---|---|
| `send_http_request` | Send arbitrary HTTP request |
| `list_history` | List request history with filters |
| `get_request_detail` | Get raw HTTP request/response by ID |
| `screenshot_panel` | Screenshot the web panel (history/detail/sender) → base64 PNG |
| `clear_all_history` | Clear all history |

## Architecture

```
MCP Client ──HTTP──▶ :9876/mcp (Streamable HTTP)
Browser    ──HTTP──▶ :9876/    (Web Panel SPA)
                     :9876/api/* (REST + SSE)
                            │
                     httpx ──▶ Target servers
                     SQLite ◀── History store
                     Playwright ──▶ Screenshots
```
