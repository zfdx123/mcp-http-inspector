---
name: http-inspector
description: 如何使用 http-inspector MCP 工具发送 HTTP 请求并对 Detail 截图写入 Markdown 报告。截图需下载到本地后引用，localhost URL 仅用于下载。
---

# HTTP Inspector — Send HTTP Requests + Screenshot for Reports

You have access to the `http-inspector` MCP server. It provides **6 tools** for sending arbitrary HTTP requests and capturing screenshots of the results.

## Key Principle: MUST Download, Never Hotlink

The tool returns a **screenshot URL** (e.g., `http://localhost:9876/screenshots/req_1.png`). **This URL only works on the MCP server's machine.** You are REQUIRED to:

1. **Download** the PNG from the URL using `curl -o` or equivalent
2. Save it to a local file
3. Reference the **local file** in your Markdown

```markdown
![Request Detail](report/req_1.png)
```

🚫 **NEVER** paste the localhost URL into a report — it will be broken for everyone else.

## Available Tools

| Tool | Purpose | Key Params |
|---|---|---|
| `send_http_request` | Send any HTTP request (like Burp Repeater) | `method`, `url`, `headers` (dict), `body`, `proxy`, `timeout`, `follow_redirects`, `verify_ssl`, `auto_screenshot`, `max_body_length` |
| `list_history` | List past requests (summary only, no raw body) | `method_filter`, `url_filter`, `status_filter`, `limit`, `offset` |
| `get_request_detail` | Get request detail (body truncated to 5K by default) | `request_id`, `max_body_length` |
| `screenshot_panel` | Screenshot any panel view → PNG URL | `view`, `request_id`, `width`, `height` |
| `screenshot_last_request` | **Quick shot**: screenshot the most recent request's Detail → PNG URL | `width`, `height` |
| `clear_all_history` | Wipe all requests and screenshots | none |

## Three Workflows

### ① One-step (best for reports)
```
1. send_http_request(..., auto_screenshot=True)
   → { "id": 1, "screenshot_url": "http://localhost:9876/screenshots/req_1.png", ... }

2. Download the screenshot locally:
   curl -o report/req_1.png http://localhost:9876/screenshots/req_1.png

3. Reference in Markdown:
   ![Evidence](report/req_1.png)
```

### ② Two-step (send, then screenshot latest)
```
1. send_http_request(...)
2. screenshot_last_request() → get URL, then download
```

### ③ Explicit (screenshot any request from history)
```
1. list_history() → pick a request_id
2. screenshot_panel(view="detail", request_id=X) → get URL, then download
```

## Markdown Report Pattern

```markdown
### Test: Login Endpoint
- **Request**: POST https://target.com/api/login
- **Status**: 200 OK | **Duration**: 234ms

![Login Response](report/req_3.png)

### Test: Admin Access
- **Request**: GET https://target.com/admin
- **Status**: 403 Forbidden

![Admin Forbidden](report/req_5.png)
```

## Notes

- `body` accepts **dict** or **string**: `{"user": "admin"}` or `"plain text"`
- `verify_ssl: false` for self-signed certificates
- `max_body_length` controls response body truncation (default 2000, 0 = full)
- Screenshot URL is local to the MCP server; download the file, don't link it
- Detail view uses full_page (1400x900) — captures entire raw HTTP
- The web panel runs on `http://localhost:9876` for live viewing
- `clear_all_history` wipes DB records and deletes all screenshot files
