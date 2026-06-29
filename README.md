# HTTP Inspector MCP Server

基于 Streamable HTTP 的 MCP 服务器，集成 Web 面板与截图功能。  
单进程、单端口 — 一站式 HTTP 流量检测工具箱。  
解决AI自动化渗透测试后续需要人工复现截图的问题。

## 功能

- **发送任意 HTTP 请求** — 支持全部方法、自定义 Header/Body、代理、超时（类似 Burp Repeater）
- **Web 面板** — 实时流量历史表格（SSE 推送）、Raw HTTP 详情视图、手动发送面板
- **截图** — AI 可调用 `screenshot_panel` 截图 Web 面板，返回 PNG 文件 URL
- **Streamable HTTP** — MCP 客户端通过 `http://localhost:9876/mcp` 连接

## 快速开始

```bash
# 安装依赖
cd mcp-http-inspector
pip install -e .

# 安装 Chromium 浏览器（用于截图）
# 项目 browsers/ 目录已内置 linux/mac-arm/win64 三平台 Chrome
# 无需额外安装，启动时自动检测

# 运行
python server.py
```

浏览器打开 `http://localhost:9876` 查看 Web 面板。

## MCP 客户端配置

```json
{
  "mcpServers": {
    "http-inspector": {
      "url": "http://localhost:9876/mcp"
    }
  }
}
```

## MCP 工具

| 工具 | 说明 |
|---|---|
| `send_http_request` | 发送任意 HTTP 请求，支持 `auto_screenshot` 一步获取截图 |
| `list_history` | 列出请求历史，支持过滤和分页（不返回 raw body） |
| `get_request_detail` | 按 ID 获取原始 HTTP 请求/响应（默认截断 5000 字符） |
| `screenshot_panel` | 截图 Web 面板（history/detail/sender），返回 PNG 下载 URL |
| `screenshot_last_request` | 快捷截图最新请求的 Detail，无需传 ID |
| `clear_all_history` | 清空所有历史记录和截图文件 |

> 更多使用指南见 `skills/SKILL.md`

## 架构

```
MCP 客户端 ──HTTP──▶ :9876/mcp (Streamable HTTP)
浏览器     ──HTTP──▶ :9876/    (Web 面板 SPA)
                     :9876/api/* (REST + SSE)
                            │
                     httpx ──▶ 目标服务器
                     SQLite ◀── 历史存储
                     Playwright ──▶ 截图
```

## 支持平台

自动检测 OS 选择内置 Chrome 浏览器：

> ⚠️ **截图功能依赖 Chromium 浏览器**，需自行下载并解压到 `browsers/` 目录。
> 
> 下载地址：[Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/)
> 

| 系统 | 下载后解压到的路径 |
|------|-------------------|
| 🐧 Linux x64 | 下载 `chrome-linux64.zip` → 解压到 `browsers/chrome-linux64/chrome` |
| 🍎 macOS ARM | 下载 `chrome-mac-arm64.zip` → 解压到 `browsers/chrome-mac-arm64/` |
| 🪟 Windows x64 | 下载 `chrome-win64.zip` → 解压到 `browsers/chrome-win64/chrome.exe` |

> 
> 启动时自动检测操作系统，选择对应路径的 Chrome。**不使用截图功能则无需下载。**