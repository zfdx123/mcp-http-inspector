"""MCP HTTP Inspector — Streamable HTTP MCP server with web panel and screenshot.

Copyright (C) 2026  HTTP Inspector Contributors
License: GPL-3.0-or-later <https://www.gnu.org/licenses/gpl-3.0>

Single process, single port (default 9876):
  /mcp       — Streamable HTTP MCP endpoint
  /          — Web panel SPA
  /api/*     — REST API + SSE for web panel

Two transports:
  http  (default) — Streamable HTTP on <host>:<port>/mcp
  stdio           — MCP stdio on stdin/stdout; the web panel is started
                    automatically in a background thread on the same port,
                    so screenshots keep working in stdio mode.
"""

import argparse
import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse
from starlette.requests import Request
from sse_starlette.sse import EventSourceResponse
from mcp.server.mcpserver import MCPServer

from db import init_db, insert_request, list_requests, get_request, clear_history

# Init DB at module load (idempotent)
asyncio.run(init_db())

# ── Web panel / screenshot config ────────────────────────
# Default; updated by main() from --port so screenshot URLs always
# point at the port actually in use.
PANEL_BASE_URL = "http://localhost:9876"
SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"

# SSE queues for pushing updates to web panel
_sse_queues: list[asyncio.Queue] = []

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

# Web panel SPA index
WEB_DIR = Path(__file__).parent / "web"
INDEX_HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")


# ── MCP app factory ──────────────────────────────────────
def create_mcp() -> MCPServer:
    """Build an MCPServer with all MCP tools and web-panel routes registered.

    One instance is created per transport (the HTTP app at startup, and a
    fresh one for each stdio run) so the two never share MCPServer session
    state inside the same process.
    """
    server = MCPServer("http-inspector")

    @server.tool()
    async def send_http_request(
        method: str,
        url: str,
        headers: Optional[dict] = None,
        body: Optional[str | dict] = None,
        proxy: Optional[str] = None,
        timeout: float = 30.0,
        follow_redirects: bool = True,
        verify_ssl: bool = True,
        auto_screenshot: bool = False,
        max_body_length: int = 2000,
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
            verify_ssl: 是否验证 SSL 证书，默认 true。自签名证书时设为 false
            auto_screenshot: 设为 true 自动截图并返回 URL。必须下载到本地文件后引用，不可直接贴 URL 到 Markdown
            max_body_length: 返回 body 的最大字符数，默认 2000。设为 0 不截断
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

        client_kwargs = {"timeout": timeout, "follow_redirects": follow_redirects, "verify": verify_ssl}
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
                "headers": dict(resp.headers),
                "body_preview": resp_body[:max_body_length] if max_body_length > 0 else resp_body,
            }
            if max_body_length > 0 and len(resp_body) > max_body_length:
                result["body_truncated"] = True
                result["body_total_length"] = len(resp_body)
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

    @server.tool()
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

    @server.tool()
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

    @server.tool()
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

    @server.tool()
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

    @server.tool()
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

    # ── Web Panel / API routes (registered on the MCP app) ──

    @server.custom_route("/", methods=["GET"])
    async def index(request: Request) -> HTMLResponse:
        return HTMLResponse(INDEX_HTML)

    @server.custom_route("/logo.svg", methods=["GET"])
    async def serve_logo(request: Request):
        """Serve logo.svg."""
        filepath = WEB_DIR / "logo.svg"
        from starlette.responses import FileResponse
        return FileResponse(filepath, media_type="image/svg+xml")

    @server.custom_route("/screenshots/{filename:path}", methods=["GET"])
    async def serve_screenshot(request: Request):
        """Serve screenshot PNG files via HTTP."""
        filename = request.path_params["filename"]
        filepath = SCREENSHOTS_DIR / filename
        if not filepath.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        from starlette.responses import FileResponse
        return FileResponse(filepath, media_type="image/png")

    @server.custom_route("/api/events", methods=["GET"])
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

    @server.custom_route("/api/history", methods=["GET"])
    async def api_list_history(request: Request):
        rows = await list_requests(
            method_filter=request.query_params.get("method_filter"),
            url_filter=request.query_params.get("url_filter"),
            status_filter=request.query_params.get("status_filter"),
            limit=int(request.query_params.get("limit", 100)),
            offset=int(request.query_params.get("offset", 0)),
        )
        return JSONResponse(rows)

    @server.custom_route("/api/history/{request_id:int}", methods=["GET"])
    async def api_get_request(request: Request):
        request_id = int(request.path_params["request_id"])
        row = await get_request(request_id)
        if not row:
            return JSONResponse({"error": f"Request {request_id} not found"}, status_code=404)
        return JSONResponse(row)

    @server.custom_route("/api/send", methods=["POST"])
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

    @server.custom_route("/api/clear", methods=["POST"])
    async def api_clear(request: Request):
        count = await clear_history()
        # Delete all screenshot files
        if SCREENSHOTS_DIR.exists():
            for f in SCREENSHOTS_DIR.iterdir():
                f.unlink()
        _broadcast_sse({"type": "clear"})
        return JSONResponse({"cleared": count})

    return server


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

            # Retry briefly: in stdio mode the web panel thread may still
            # be warming up when the first screenshot is requested.
            last_exc = None
            for _ in range(3):
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    break
                except Exception as e:  # noqa: BLE001 - retry any nav failure
                    last_exc = e
                    await asyncio.sleep(0.5)
            else:
                raise last_exc

            await asyncio.sleep(1.2)
            if full_page:
                # Clip to the exact content height: documentElement.scrollHeight
                # is never smaller than the viewport, so a plain full_page shot
                # leaves a large blank band under short content.
                dims = await page.evaluate(
                    "() => ({w: document.documentElement.scrollWidth, h: document.body.scrollHeight})"
                )
                screenshot_bytes = await page.screenshot(
                    type="png", full_page=True,
                    clip={"x": 0, "y": 0, "width": max(1, dims["w"]), "height": max(1, dims["h"])},
                )
            else:
                screenshot_bytes = await page.screenshot(type="png", full_page=False)

            # Report the real PNG dimensions (viewport params can differ from
            # the clipped full-page output), not the viewport we asked for.
            import struct
            actual_width, actual_height = struct.unpack(">II", screenshot_bytes[16:24])

            SCREENSHOTS_DIR.mkdir(exist_ok=True)
            tag = f"req{request_id}" if request_id else view
            filename = f"{tag}.png"
            filepath = SCREENSHOTS_DIR / filename
            filepath.write_bytes(screenshot_bytes)
            url = f"{PANEL_BASE_URL}/screenshots/{filename}"
            return {
                "url": url,
                "width": actual_width,
                "height": actual_height,
                "view": view,
                "request_id": request_id,
                "full_page": full_page,
                "size_bytes": len(screenshot_bytes),
            }
        finally:
            await page.close()


# ── SSE helpers ────────────────────────────────────────────
def _broadcast_sse(data: dict):
    msg = json.dumps(data)
    for q in _sse_queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


# ── Launchers ─────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-http-inspector",
        description="MCP HTTP Inspector — HTTP request inspector MCP server with web panel.",
    )
    parser.add_argument(
        "--transport", choices=["http", "stdio"], default="http",
        help="MCP transport mode: 'http' (Streamable HTTP, default) or 'stdio'.",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Web panel bind host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=9876,
        help="Web panel port (default: 9876)",
    )
    return parser.parse_args()


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    """Block until the web panel starts listening (or raise SystemExit)."""
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    if target == "::":
        target = "::1"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target, port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.1)
    raise SystemExit(f"web panel failed to start on {host}:{port}")


def _run_http(args: argparse.Namespace) -> None:
    global PANEL_BASE_URL
    PANEL_BASE_URL = f"http://localhost:{args.port}"
    app: Starlette = create_mcp().streamable_http_app(host=args.host)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _run_stdio(args: argparse.Namespace) -> None:
    global PANEL_BASE_URL
    PANEL_BASE_URL = f"http://localhost:{args.port}"

    # Start the web panel in a background thread so the web UI and
    # screenshots keep working while MCP speaks stdio on stdin/stdout.
    # uvicorn logs go to stderr, keeping stdout a clean protocol channel.
    app: Starlette = create_mcp().streamable_http_app(host=args.host)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True, name="web-panel").start()
    _wait_for_port(args.host, args.port)

    # Fresh MCPServer instance — never shares session state with the HTTP app.
    create_mcp().run(transport="stdio")


# ── Main ───────────────────────────────────────────────────
def main():
    """Entry point: uvicorn server:app (http) or MCP stdio server."""
    args = _parse_args()
    if args.transport == "http":
        _run_http(args)
    else:
        _run_stdio(args)


if __name__ == "__main__":
    main()