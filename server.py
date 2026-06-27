"""MCP HTTP Inspector — Streamable HTTP MCP server with web panel and screenshot.

Single process, single port (default 9876):
  /mcp       — Streamable HTTP MCP endpoint
  /          — Web panel SPA
  /api/*     — REST API + SSE for web panel
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route
from starlette.requests import Request
from sse_starlette.sse import EventSourceResponse
from mcp.server.fastmcp import FastMCP

from db import init_db, insert_request, list_requests, get_request, clear_history

# ── FastMCP ───────────────────────────────────────────────
mcp = FastMCP("http-inspector")

# SSE queues for pushing updates to web panel
_sse_queues: list[asyncio.Queue] = []

PANEL_BASE_URL = "http://localhost:9876"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"

# Playwright references
_browser = None
_browser_lock = asyncio.Lock()

# Detect OS/arch to pick the correct Chrome binary
def _detect_chrome_path() -> str:
    import platform
    base = Path(__file__).parent / "browsers"
    system = platform.system()
    machine = platform.machine()

    if system == "Windows" or system.startswith("CYGWIN") or system.startswith("MSYS"):
        return str(base / "chrome-win64" / "chrome.exe")
    elif system == "Darwin":
        return str(base / "chrome-mac-arm64" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing")
    else:  # Linux
        return str(base / "chrome-linux64" / "chrome")

_CHROME_PATH = _detect_chrome_path()


# ── MCP Tools ─────────────────────────────────────────────
@mcp.tool()
async def send_http_request(
    method: str,
    url: str,
    headers: Optional[dict] = None,
    body: Optional[str | dict] = None,
    proxy: Optional[str] = None,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    auto_screenshot: bool = False,
) -> str:
    """发送任意 HTTP 请求（类似 Burp Repeater）。

    Args:
        method: HTTP 方法 (GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS)
        url: 目标 URL
        headers: 请求头字典，如 {"Content-Type": "application/json"}
        body: 请求体，字符串或 JSON 对象（dict）
        proxy: 代理地址，如 http://127.0.0.1:8080
        timeout: 超时秒数，默认 30
        follow_redirects: 是否跟随重定向，默认 true
        auto_screenshot: 设为 true 自动截图并返回 URL。必须下载到本地文件后引用，不可直接贴 URL 到 Markdown
    """
    parsed_headers = headers or {}
    # Normalize body: dict → JSON string
    body_str = None
    if body is not None:
        if isinstance(body, dict):
            body_str = json.dumps(body, ensure_ascii=False)
        else:
            body_str = str(body)

    raw_request = f"{method.upper()} {url} HTTP/1.1\r\n"
    for k, v in parsed_headers.items():
        raw_request += f"{k}: {v}\r\n"
    if body_str:
        raw_request += f"\r\n{body_str}"
    else:
        raw_request += "\r\n"

    client_kwargs = {"timeout": timeout, "follow_redirects": follow_redirects}
    if proxy:
        client_kwargs["proxy"] = proxy

    start = 0.0
    try:
        start = time.perf_counter()
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.request(
                method=method.upper(), url=url, headers=parsed_headers, content=body_str
            )
        elapsed = (time.perf_counter() - start) * 1000

        raw_response = f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or ''}\r\n"
        for k, v in resp.headers.items():
            raw_response += f"{k}: {v}\r\n"
        resp_body = resp.text
        raw_response += f"\r\n{resp_body}"

        req_id = await insert_request(
            method=method.upper(), url=url,
            status_code=resp.status_code, duration_ms=round(elapsed, 2),
            response_size=len(resp.content),
            raw_request=raw_request, raw_response=raw_response,
        )
        _broadcast_sse({
            "type": "new_request", "id": req_id, "method": method.upper(),
            "url": url, "status_code": resp.status_code,
            "duration_ms": round(elapsed, 2), "response_size": len(resp.content),
        })
        result = {
            "id": req_id, "status_code": resp.status_code,
            "duration_ms": round(elapsed, 2), "response_size": len(resp.content),
            "headers": dict(resp.headers), "body_preview": resp_body[:2000],
        }
        if auto_screenshot:
            sc = await _do_screenshot(view="detail", request_id=req_id)
            result["screenshot_url"] = sc["url"]
            result["screenshot_size"] = sc["size_bytes"]
        return json.dumps(result, ensure_ascii=False, indent=2)

    except httpx.RequestError as e:
        elapsed = (time.perf_counter() - start) * 1000 if start else 0
        err_msg = f"{type(e).__name__}: {e}"
        req_id = await insert_request(
            method=method.upper(), url=url,
            status_code=None, duration_ms=round(elapsed, 2), response_size=0,
            raw_request=raw_request, raw_response=None, error=err_msg,
        )
        _broadcast_sse({
            "type": "new_request", "id": req_id, "method": method.upper(),
            "url": url, "status_code": None, "duration_ms": round(elapsed, 2),
            "error": err_msg,
        })
        return json.dumps({"id": req_id, "error": err_msg}, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_history(
    method_filter: Optional[str] = None,
    url_filter: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """列出请求历史。支持按方法、URL、状态码过滤和分页。
    注意：不返回 raw body，用 get_request_detail 按需获取。
    """
    rows = await list_requests(
        method_filter=method_filter, url_filter=url_filter,
        status_filter=status_filter, limit=limit, offset=offset,
    )
    # Strip large fields to protect LLM context
    for r in rows:
        r.pop("raw_request", None)
        r.pop("raw_response", None)
    return json.dumps(rows, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_request_detail(
    request_id: int,
    max_body_length: int = 5000,
) -> str:
    """获取单条请求的详情。raw body 默认截断至 5000 字符，用 max_body_length=0 获取完整。

    Args:
        request_id: 历史记录 ID
        max_body_length: raw body 最大字符数，默认 5000。设为 0 不截断
    """
    row = await get_request(request_id)
    if not row:
        return json.dumps({"error": f"Request {request_id} not found"})
    if max_body_length > 0:
        for field in ("raw_request", "raw_response"):
            val = row.get(field)
            if val and len(val) > max_body_length:
                total = len(val)
                row[field] = val[:max_body_length] + f"\n... [truncated at {max_body_length} chars, {total - max_body_length} more bytes omitted]"
    return json.dumps(row, ensure_ascii=False, indent=2)


@mcp.tool()
async def screenshot_panel(
    view: str = "history",
    request_id: Optional[int] = None,
    width: int = 1280,
    height: int = 800,
) -> str:
    """对 Web 面板页面截图，保存为 PNG 文件，返回 URL。
    必须下载该 URL 到本地文件，不可直接贴到 Markdown！

    Detail 视图自动使用全页截图以捕获完整 Raw HTTP 请求+响应。
    写报告时推荐 view='detail' + request_id。
    如果只想截最新请求，用 screenshot_last_request 工具，无需传 ID。

    Args:
        view: 截取视图: history / detail / sender，默认 history
        request_id: 截取特定请求详情时传入 request_id（仅 view=detail 有效）
        width: 截图宽度，默认 1280（detail 默认 1400）
        height: 截图高度，默认 800（detail 全页截图时此为初始视口高度）
    """
    result = await _do_screenshot(view=view, request_id=request_id, width=width, height=height)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def screenshot_last_request(
    width: int = 1400,
    height: int = 900,
) -> str:
    """截图最新一条请求的 Detail 视图，保存为 PNG 文件，返回 URL。
    必须下载该 URL 到本地文件，不可直接贴到 Markdown！
    无需传 request_id，自动使用最近一次 send_http_request 的结果。

    Args:
        width: 截图宽度，默认 1400
        height: 截图高度，默认 900
    """
    rows = await list_requests(limit=1, offset=0)
    if not rows:
        return json.dumps({"error": "没有请求历史，请先调用 send_http_request"})
    latest_id = rows[0]["id"]
    result = await _do_screenshot(view="detail", request_id=latest_id, width=width, height=height)
    return json.dumps(result, ensure_ascii=False)


# ── Internal screenshot helper ─────────────────────────────
async def _do_screenshot(
    view: str = "history",
    request_id: Optional[int] = None,
    width: int = 1280,
    height: int = 800,
) -> dict:
    global _browser
    async with _browser_lock:
        if _browser is None:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(
                headless=True,
                executable_path=_CHROME_PATH,
                args=["--no-sandbox", "--disable-gpu"],
            )

        is_detail = (view == "detail" and request_id is not None)
        viewport_width = width if not is_detail else max(width, 1400)
        viewport_height = height if not is_detail else 900
        full_page = bool(is_detail)

        page = await _browser.new_page(
            viewport={"width": viewport_width, "height": viewport_height}
        )
        try:
            if is_detail:
                url = f"{PANEL_BASE_URL}/?tab=detail&id={request_id}&screenshot=1"
            elif view == "sender":
                url = f"{PANEL_BASE_URL}/?tab=sender&screenshot=1"
            else:
                url = f"{PANEL_BASE_URL}/?tab=history"
            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(1.2)
            screenshot_bytes = await page.screenshot(type="png", full_page=full_page)

            SCREENSHOTS_DIR.mkdir(exist_ok=True)
            tag = f"req{request_id}" if request_id else view
            filename = f"{tag}.png"
            filepath = SCREENSHOTS_DIR / filename
            filepath.write_bytes(screenshot_bytes)
            url = f"{PANEL_BASE_URL}/screenshots/{filename}"
            return {
                "url": url,
                "width": viewport_width,
                "height": viewport_height,
                "view": view,
                "request_id": request_id,
                "full_page": full_page,
                "size_bytes": len(screenshot_bytes),
            }
        finally:
            await page.close()


@mcp.tool()
async def clear_all_history() -> str:
    """清空所有请求历史和截图文件。"""
    count = await clear_history()
    if SCREENSHOTS_DIR.exists():
        deleted = 0
        for f in SCREENSHOTS_DIR.iterdir():
            f.unlink()
            deleted += 1
        return json.dumps({"cleared_requests": count, "deleted_screenshots": deleted})
    return json.dumps({"cleared": count})


# ── SSE helpers ────────────────────────────────────────────
def _broadcast_sse(data: dict):
    msg = json.dumps(data)
    for q in _sse_queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


# ── Web Panel / API routes (added to MCP's Starlette app) ──
WEB_DIR = Path(__file__).parent / "web"
INDEX_HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")


async def index(request: Request) -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


async def serve_logo(request: Request):
    """Serve logo.svg."""
    filepath = WEB_DIR / "logo.svg"
    from starlette.responses import FileResponse
    return FileResponse(filepath, media_type="image/svg+xml")


async def sse_events(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_queues.append(queue)

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {"data": data}
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
        finally:
            _sse_queues.remove(queue)

    return EventSourceResponse(generate())


async def api_list_history(request: Request):
    rows = await list_requests(
        method_filter=request.query_params.get("method_filter"),
        url_filter=request.query_params.get("url_filter"),
        status_filter=request.query_params.get("status_filter"),
        limit=int(request.query_params.get("limit", 100)),
        offset=int(request.query_params.get("offset", 0)),
    )
    return JSONResponse(rows)


async def api_get_request(request: Request):
    request_id = int(request.path_params["request_id"])
    row = await get_request(request_id)
    if not row:
        return JSONResponse({"error": f"Request {request_id} not found"}, status_code=404)
    return JSONResponse(row)


async def api_send(request: Request):
    """Send HTTP request from the web panel (called by Sender tab)."""
    data = await request.json()
    method = data.get("method", "GET")
    url = data.get("url", "")
    headers = data.get("headers", "")
    body = data.get("body", "")

    parsed_headers = data.get("headers", {}) or {}
    if isinstance(parsed_headers, str):
        try:
            parsed_headers = json.loads(parsed_headers)
        except json.JSONDecodeError:
            return JSONResponse({"error": "headers 不是合法的 JSON"}, status_code=400)

    # Normalize body: dict -> JSON string
    if body is not None and isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False)

    raw_request = f"{method.upper()} {url} HTTP/1.1\r\n"
    for k, v in parsed_headers.items():
        raw_request += f"{k}: {v}\r\n"
    if body:
        raw_request += f"\r\n{body}"
    else:
        raw_request += "\r\n"

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.request(
                method=method.upper(), url=url, headers=parsed_headers, content=body,
            )
        elapsed = (time.perf_counter() - start) * 1000

        raw_response = f"HTTP/1.1 {resp.status_code} {resp.reason_phrase or ''}\r\n"
        for k, v in resp.headers.items():
            raw_response += f"{k}: {v}\r\n"
        resp_body = resp.text
        raw_response += f"\r\n{resp_body}"

        req_id = await insert_request(
            method=method.upper(), url=url,
            status_code=resp.status_code, duration_ms=round(elapsed, 2),
            response_size=len(resp.content),
            raw_request=raw_request, raw_response=raw_response,
        )
        _broadcast_sse({
            "type": "new_request", "id": req_id, "method": method.upper(),
            "url": url, "status_code": resp.status_code,
            "duration_ms": round(elapsed, 2), "response_size": len(resp.content),
        })
        return JSONResponse({
            "id": req_id, "status_code": resp.status_code,
            "duration_ms": round(elapsed, 2), "response_size": len(resp.content),
            "headers": dict(resp.headers), "body": resp_body[:5000],
        })
    except httpx.RequestError as e:
        elapsed = (time.perf_counter() - start) * 1000
        err_msg = f"{type(e).__name__}: {e}"
        req_id = await insert_request(
            method=method.upper(), url=url,
            status_code=None, duration_ms=round(elapsed, 2), response_size=0,
            raw_request=raw_request, raw_response=None, error=err_msg,
        )
        _broadcast_sse({
            "type": "new_request", "id": req_id, "method": method.upper(),
            "url": url, "status_code": None, "duration_ms": round(elapsed, 2),
            "error": err_msg,
        })
        return JSONResponse({"id": req_id, "error": err_msg}, status_code=502)


async def api_clear(request: Request):
    count = await clear_history()
    # Delete all screenshot files
    if SCREENSHOTS_DIR.exists():
        for f in SCREENSHOTS_DIR.iterdir():
            f.unlink()
    _broadcast_sse({"type": "clear"})
    return JSONResponse({"cleared": count})


# ── Static file server for screenshots ────────────────────
async def serve_screenshot(request: Request):
    """Serve screenshot PNG files via HTTP."""
    filename = request.path_params["filename"]
    filepath = SCREENSHOTS_DIR / filename
    if not filepath.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    from starlette.responses import FileResponse
    return FileResponse(filepath, media_type="image/png")


# ── Assemble the app ──────────────────────────────────────
# Get MCP's Starlette app as the base
app: Starlette = mcp.streamable_http_app()

# Init DB at module load (idempotent)
asyncio.run(init_db())

# Add web panel & API routes ON TOP of MCP's Starlette app
# (insert before MCP's catch-all so / and /api/* take priority)
web_routes = [
    Route("/", index, methods=["GET"]),
    Route("/logo.svg", serve_logo, methods=["GET"]),
    Route("/screenshots/{filename:path}", serve_screenshot, methods=["GET"]),
    Route("/api/events", sse_events, methods=["GET"]),
    Route("/api/history", api_list_history, methods=["GET"]),
    Route("/api/history/{request_id:int}", api_get_request, methods=["GET"]),
    Route("/api/send", api_send, methods=["POST"]),
    Route("/api/clear", api_clear, methods=["POST"]),
]

# Insert routes at the beginning so they match before MCP's routes
app.router.routes = web_routes + app.router.routes


# ── Main ───────────────────────────────────────────────────
def main():
    """Entry point: uvicorn server:app"""
    uvicorn.run(app, host="0.0.0.0", port=9876, log_level="info")


if __name__ == "__main__":
    main()
