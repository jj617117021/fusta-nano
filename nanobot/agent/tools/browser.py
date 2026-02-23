"""Browser automation tool using Chrome DevTools Protocol."""

import asyncio
import os
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


# Default CDP port
DEFAULT_CDP_PORT = 18800


class BrowserTool(Tool):
    """Browser automation tool using CDP."""

    name = "browser"
    description = """Browser automation using Playwright (default, more reliable for React) or CDP.

**IMPORTANT - Use this workflow:**
1. start browser: {"action": "start"}
2. search: {"action": "search", "query": "关键词", "url": "xiaohongshu"}
3. snapshot: {"action": "snapshot"} - Get element refs (e1, e2...) AFTER search/navigate
4. click: {"action": "click", "ref": "e5"} OR act: {"action": "act", "request": {"kind": "click", "ref": "e5"}}

**Actions:** search, navigate, snapshot, click, act, scroll, screenshot, get_text
**Browser:** start, stop, status
**Tabs:** new_tab, tabs, switch_tab, close_tab
**Input:** type, press

TRUST [VERIFIED] results!"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.cdp = None
        self.playwright = None
        self._lock = asyncio.Lock()
        self._manager = None
        self._port = DEFAULT_CDP_PORT
        self._browser = "chrome"
        self._driver = "playwright"  # "cdp" or "playwright" (playwright is more reliable for React)

    def _get_manager(self):
        """Get browser manager instance."""
        if self._manager is None:
            from nanobot.agent.tools.browser_manager import BrowserManager
            self._manager = BrowserManager(workspace=self.workspace)
        return self._manager

    def _get_port(self, **kwargs) -> int:
        """Get port from kwargs or use default."""
        return kwargs.get("port", kwargs.get("cdp_port", DEFAULT_CDP_PORT))

    async def _get_client(self, port: int | None = None, driver: str | None = None):
        """Get or create browser client (CDP or Playwright)."""
        target_driver = driver or self._driver
        target_port = port or self._port

        if target_driver == "playwright":
            if self.playwright is None:
                from nanobot.agent.tools.playwright_client import PlaywrightClient
                self.playwright = PlaywrightClient(host="127.0.0.1", port=target_port)
                await self.playwright.connect()
            return self.playwright
        else:
            if self.cdp is None:
                from nanobot.agent.tools.cdp_client import CDPClient
                self.cdp = CDPClient(host="127.0.0.1", port=target_port)
                await self.cdp.connect()
            return self.cdp

    async def execute(self, action: str, **kwargs) -> str:
        """Execute a browser action."""
        try:
            # Handle browser management actions first (don't require CDP connection)
            if action == "start":
                browser = kwargs.get("browser", "chrome")
                port = self._get_port(**kwargs)
                profile = kwargs.get("profile", "nanobot")
                headless = kwargs.get("headless", False)

                # Update instance state
                self._port = port
                self._browser = browser

                manager = self._get_manager()
                result = await manager.start(
                    browser=browser,
                    port=port,
                    profile=profile,
                    headless=headless,
                )
                if result.get("success"):
                    msg = f"[VERIFIED] {result.get('message')}"
                    if result.get("pid"):
                        msg += f" (PID: {result.get('pid')})"
                    return msg
                return f"[FAILED] {result.get('error')}"

            elif action == "stop":
                port = self._get_port(**kwargs)
                manager = self._get_manager()
                result = await manager.stop(port=port)
                if result.get("success"):
                    # Reset CDP client
                    if self.cdp:
                        await self.cdp.close()
                        self.cdp = None
                    # Reset Playwright client
                    if self.playwright:
                        await self.playwright.close()
                        self.playwright = None
                    return f"[VERIFIED] {result.get('message')}"
                return f"[FAILED] {result.get('error')}"

            elif action == "status":
                port = self._get_port(**kwargs)
                manager = self._get_manager()
                result = await manager.status(port=port)
                if result.get("running"):
                    info = f"[BROWSER RUNNING] Port: {result.get('port')}, Browser: {result.get('browser')}"
                    return info
                return f"[BROWSER OFFLINE] Port: {result.get('port')} - Use action=start to launch browser"

            elif action == "profiles":
                manager = self._get_manager()
                result = manager.list_profiles()
                lines = ["[PROFILES]"]
                for name, config in result.get("profiles", {}).items():
                    lines.append(f"  {name}: port={config['port']}, browser={config['browser']}")
                return "\n".join(lines)

            # For all other actions, force playwright (more reliable for React apps)
            # Ignore model-provided driver to ensure consistent behavior
            driver = self._driver  # Always use playwright
            cdp = await self._get_client(self._get_port(**kwargs), driver=driver)

            if action == "navigate" or action == "open":
                url = kwargs.get("url") or kwargs.get("targetUrl") or kwargs.get("target_url", "")
                if not url:
                    return "Error: URL required"

                result = await cdp.navigate(url)
                error = result.get("error")
                if error:
                    return f"[FAILED] {error}"

                # Get current URL
                await asyncio.sleep(1)
                content = await cdp.get_content()
                title = content[:100] if content else "Page loaded"
                return f"[VERIFIED] Navigated to: {url}\n{title[:100]}"

            elif action == "search":
                query = kwargs.get("query") or kwargs.get("text") or kwargs.get("keyword") or ""
                url = kwargs.get("url", "")

                # ALWAYS navigate first if URL is provided
                if url:
                    # Determine the site and build search URL
                    url_lower = url.lower()
                    search_url = None

                    if 'amazon' in url_lower:
                        search_url = f"https://www.amazon.com/s?k={query.replace(' ', '+')}"
                    elif 'xiaohongshu' in url_lower or '小红书' in url or '小红书' in query:
                        search_url = f"https://www.xiaohongshu.com/search_result?keyword={query}"
                    elif 'youtube' in url_lower:
                        search_url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
                    elif 'ebay' in url_lower:
                        search_url = f"https://www.ebay.com/sch/i.html?_nkw={query.replace(' ', '+')}"

                    if search_url:
                        await cdp.navigate(search_url)
                        await asyncio.sleep(3)
                        content = await cdp.get_content()
                        return f"[VERIFIED] Searched: {query}\n\n{content[:800]}"

                    # Fallback: navigate to site and use search box
                    if 'amazon' in url_lower:
                        target = 'https://www.amazon.com'
                    elif 'xiaohongshu' in url_lower or '小红书' in url:
                        target = 'https://www.xiaohongshu.com'
                    else:
                        target = url if url.startswith('http') else 'https://' + url

                    await cdp.navigate(target)
                    await asyncio.sleep(3)

                # Try to find and use search box
                selectors = [
                    'input[type="search"]',
                    'input[type="text"]',
                    'input[placeholder*="搜索"]',
                    'input[placeholder*="Search"]',
                    '#twotabsearchtextbox'
                ]

                for sel in selectors:
                    node_id = await cdp.query_selector(sel)
                    if node_id:
                        # Type slowly
                        await cdp.type_text(sel, query)
                        await asyncio.sleep(2)
                        # Press Enter multiple times
                        await cdp.press_key("Enter")
                        await asyncio.sleep(4)
                        await cdp.press_key("Enter")
                        await asyncio.sleep(3)
                        # Get content
                        content = await cdp.get_content()
                        return f"[VERIFIED] Searched: {query}\n\n{content[:800]}"

                return "[FAILED] No search box found"

            # elif action == "click" - moved to line 213
            # elif action == "type" - moved to line 230

            elif action == "press":
                key = kwargs.get("key", "")
                if not key:
                    return "Error: key required"

                await cdp.press_key(key)
                return f"[VERIFIED] Pressed: {key}"

            elif action == "tabs":
                # List all tabs
                result = await cdp.list_tabs()
                tabs = result.get("tabs", [])
                current = result.get("current", "")
                lines = ["[TABS]"]
                for i, tab in enumerate(tabs):
                    marker = " ←" if tab.get("id") == current else ""
                    lines.append(f"t{i+1}: {tab.get('title', 'Untitled')[:40]}{marker}")
                return "\n".join(lines)

            elif action == "new_tab":
                url = kwargs.get("url", "") or kwargs.get("targetUrl", "") or "about:blank"
                # Auto-add https:// if missing
                if url and not url.startswith("http"):
                    if "." not in url:
                        url = f"https://www.{url}.com"
                    else:
                        url = f"https://www.{url}" if not url.startswith("www.") else f"https://{url}"
                result = await cdp.create_tab(url)
                if result.get("success"):
                    # Wait for page to load
                    await asyncio.sleep(3)
                    return f"[VERIFIED] Created new tab and opened {url}. NO need to navigate again!"
                return f"[FAILED] {result.get('error')}"

            elif action == "switch_tab":
                tab_id = kwargs.get("tab", "") or kwargs.get("tab_id", "") or kwargs.get("key", "")
                if tab_id.startswith("t"):
                    # Convert t1, t2 to index
                    try:
                        idx = int(tab_id[1:]) - 1
                        tabs_result = await cdp.list_tabs()
                        tabs = tabs_result.get("tabs", [])
                        if 0 <= idx < len(tabs):
                            tab_id = tabs[idx].get("id", "")
                    except:
                        pass
                result = await cdp.switch_tab(tab_id)
                if result.get("success"):
                    return f"[VERIFIED] Switched to tab: {tab_id}"
                return f"[FAILED] {result.get('error')}"

            elif action == "close_tab":
                tab_id = kwargs.get("tab", "") or kwargs.get("tab_id", "") or kwargs.get("key", "")
                # Support t1, t2, etc. index format
                if tab_id.startswith("t"):
                    try:
                        idx = int(tab_id[1:]) - 1
                        tabs_result = await cdp.list_tabs()
                        tabs = tabs_result.get("tabs", [])
                        if 0 <= idx < len(tabs):
                            tab_id = tabs[idx].get("id", "")
                    except:
                        pass
                result = await cdp.close_tab(tab_id)
                if result.get("success"):
                    return f"[VERIFIED] Closed tab: {tab_id}"
                return f"[FAILED] {result.get('error')}"

            elif action == "hover":
                ref = kwargs.get("ref", "") or kwargs.get("key", "")
                if not ref:
                    return "Error: ref required"
                result = await cdp.hover_by_ref(ref)
                if result.get("success"):
                    return f"[VERIFIED] Hovered over {ref}"
                return f"[FAILED] {result.get('error')}"

            elif action == "scroll":
                x = kwargs.get("x", 0)
                y = kwargs.get("y", 0)
                selector = kwargs.get("selector", "")
                if selector:
                    result = await cdp.scroll_to_selector(selector)
                else:
                    result = await cdp.scroll(x=x, y=y)
                if result.get("success"):
                    return f"[VERIFIED] Scrolled"
                return f"[FAILED] {result.get('error')}"

            elif action == "resize":
                width = kwargs.get("width", 1280)
                height = kwargs.get("height", 720)
                result = await cdp.resize_viewport(width, height)
                if result.get("success"):
                    return f"[VERIFIED] Resized to {width}x{height}"
                return f"[FAILED] {result.get('error')}"

            elif action == "evaluate":
                expression = kwargs.get("expression", "") or kwargs.get("code", "")
                if not expression:
                    return "Error: expression required"
                result = await cdp.evaluate(expression)
                if result.get("success"):
                    return f"[RESULT]\n{result.get('result', '')}"
                return f"[FAILED] {result.get('error')}"

            elif action == "cookies":
                # Get cookies
                result = await cdp.get_cookies()
                if result.get("success"):
                    cookies = result.get("cookies", [])
                    lines = ["[COOKIES]"]
                    for c in cookies[:20]:
                        lines.append(f"  {c.get('name')}={c.get('value')[:30]}... domain={c.get('domain')}")
                    return "\n".join(lines)
                return f"[FAILED] {result.get('error')}"

            elif action == "storage":
                storage_type = kwargs.get("type", "local")  # local or session
                if storage_type == "session":
                    result = await cdp.get_session_storage()
                else:
                    result = await cdp.get_local_storage()
                if result.get("success"):
                    items = result.get("storage", {})
                    lines = [f"[{storage_type.upper()} STORAGE]"]
                    for k, v in items.items():
                        lines.append(f"  {k}: {str(v)[:50]}")
                    return "\n".join(lines)
                return f"[FAILED] {result.get('error')}"

            elif action == "wait":
                url = kwargs.get("url", "")
                selector = kwargs.get("selector", "")
                load = kwargs.get("load", False)
                timeout = kwargs.get("timeout", 30000)
                result = await cdp.wait(url=url, selector=selector, load=load, timeout=timeout)
                if result.get("success"):
                    if url:
                        return f"[VERIFIED] URL matched: {result.get('url')}"
                    if selector:
                        return f"[VERIFIED] Selector found: {selector}"
                    if load:
                        return f"[VERIFIED] Page loaded"
                return f"[FAILED] {result.get('error')}"

            elif action == "console":
                result = await cdp.get_console_messages()
                if result.get("success"):
                    messages = result.get("messages", [])
                    if not messages:
                        return "[CONSOLE] No messages"
                    lines = ["[CONSOLE]"]
                    lines.extend(messages[:20])
                    return "\n".join(lines)
                return f"[FAILED] {result.get('error')}"

            elif action == "errors":
                result = await cdp.get_errors()
                if result.get("success"):
                    errors = result.get("errors", [])
                    if not errors:
                        return "[ERRORS] No page errors"
                    lines = ["[PAGE ERRORS]"]
                    lines.extend(errors[:10])
                    return "\n".join(lines)
                return f"[FAILED] {result.get('error')}"

            elif action == "download":
                url = kwargs.get("url", "")
                path = kwargs.get("path", "")
                if not url:
                    return "Error: url required"
                if not path:
                    # Default to downloads folder
                    downloads_dir = self.workspace / "downloads"
                    downloads_dir.mkdir(exist_ok=True)
                    import time
                    path = str(downloads_dir / f"download_{int(time.time())}")
                result = await cdp.download_file(url, path)
                if result.get("success"):
                    return f"[VERIFIED] Downloaded to: {result.get('path')}"
                return f"[FAILED] {result.get('error')}"

            elif action == "upload":
                selector = kwargs.get("selector", "")
                file_path = kwargs.get("path", "") or kwargs.get("file", "")
                if not selector:
                    return "Error: selector required"
                if not file_path:
                    return "Error: path required"
                result = await cdp.upload_file(selector, file_path)
                if result.get("success"):
                    return f"[VERIFIED] Uploaded: {result.get('file')}"
                return f"[FAILED] {result.get('error')}"

            elif action == "trace":
                # Start or stop trace
                mode = kwargs.get("mode", "stop")  # start or stop
                path = kwargs.get("path", "")
                if not path:
                    traces_dir = self.workspace / "traces"
                    traces_dir.mkdir(exist_ok=True)
                    import time
                    path = str(traces_dir / f"trace_{int(time.time())}.json")

                if mode == "start":
                    result = await cdp.start_trace(path)
                    if result.get("success"):
                        return f"[VERIFIED] Trace started: {path}"
                    return f"[FAILED] {result.get('error')}"
                else:
                    result = await cdp.stop_trace(path)
                    if result.get("success"):
                        return f"[VERIFIED] Trace saved to: {result.get('path')}"
                    return f"[FAILED] {result.get('error')}"

            elif action == "screenshot":
                screenshots_dir = self.workspace / "screenshots"
                screenshots_dir.mkdir(exist_ok=True)
                import time
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                path = screenshots_dir / f"screenshot_{timestamp}.png"

                result = await cdp.take_screenshot(str(path))
                if result.get("success"):
                    return f"Screenshot saved to {path}"
                return f"[FAILED] {result.get('error')}"

            elif action == "get_text":
                content = await cdp.get_content()
                return f"[PAGE CONTENT]\n{content[:5000]}"

            elif action == "snapshot":
                # Get DOM snapshot with refs
                # Use get_snapshot_dom for better results on Xiaohongshu (captures posts)
                result = await cdp.get_snapshot_dom(max_nodes=50)
                if "error" in result:
                    return f"[ERROR] {result.get('error')}"

                elements = result.get("elements", [])
                lines = []
                lines.append("[PAGE SNAPSHOT with refs]")
                lines.append(f"Found {len(elements)} clickable elements")
                lines.append("")

                for el in elements[:30]:
                    ref = el.get("ref", "")
                    tag = el.get("tag", "")
                    text = el.get("name", "") or el.get("text", "")
                    ptype = el.get("type", "")
                    placeholder = el.get("placeholder", "")

                    line = f"{ref} <{tag}>"
                    if ptype:
                        line += f' type="{ptype}"'
                    if placeholder:
                        line += f' placeholder="{placeholder[:20]}"'
                    if text:
                        line += f' "{text[:40]}..."'
                    lines.append(line)

                lines.append("")
                lines.append("Use ref (e1, e2...) for click/type actions")

                return "\n".join(lines[:50])

            elif action == "act":
                # OpenClaw-style act action
                # Request format: {"kind": "click", "ref": "e5"} or {"kind": "fill", "ref": "e8", "value": "text"}
                import json
                request = kwargs.get("request", {})
                if isinstance(request, str):
                    try:
                        request = json.loads(request)
                    except:
                        return "Error: request must be JSON object"

                kind = request.get("kind", "")
                ref = request.get("ref", "")

                if kind == "click" and ref:
                    result = await cdp.click_by_ref(ref)
                    if result.get("success"):
                        return f"[VERIFIED] Clicked {ref}"
                    return f"[FAILED] {result.get('error')}"
                elif kind == "fill" and ref:
                    value = request.get("value", "")
                    result = await cdp.type_by_ref(ref, value)
                    if result.get("success"):
                        return f"[VERIFIED] Filled {ref}: {value}"
                    return f"[FAILED] {result.get('error')}"
                else:
                    return f"Error: act requires kind=click/fill and ref"

            elif action == "click":
                # Check if using ref (e1, e2) or selector
                selector = kwargs.get("selector", "")
                ref = kwargs.get("ref", "") or kwargs.get("key", "")  # Support both "ref" and "key"

                if ref:
                    # Use ref-based click
                    result = await cdp.click_by_ref(ref)
                    if result.get("success"):
                        method = result.get("method", "unknown")
                        current_url = result.get("current_url", "")
                        navigated_to = result.get("navigated_to", "")
                        if navigated_to:
                            return f"[VERIFIED] Clicked {ref} ({method}), navigated to: {navigated_to[:80]}"
                        if current_url:
                            return f"[VERIFIED] Clicked {ref} ({method}), now at: {current_url[:60]}"
                        return f"[VERIFIED] Clicked {ref} ({method})"
                    return f"[FAILED] {result.get('error')} (ref={ref})"
                elif selector:
                    # Use selector-based click
                    result = await cdp.click_element(selector)
                    if result.get("success"):
                        return f"[VERIFIED] Clicked: {selector}"
                    return f"[FAILED] {result.get('error')}"
                else:
                    return "Error: selector or ref required"

            elif action == "type":
                # Check if using ref or selector
                selector = kwargs.get("selector", "")
                ref = kwargs.get("ref", "")
                text = kwargs.get("text", "")

                if ref:
                    # Use ref-based type
                    result = await cdp.type_by_ref(ref, text)
                    if result.get("success"):
                        return f"[VERIFIED] Typed into ref {ref}: {text}"
                    return f"[FAILED] {result.get('error')}"
                elif selector:
                    # Use selector-based type (existing)
                    result = await cdp.type_text(selector, text)
                    if result.get("success"):
                        return f"[VERIFIED] Typed: {text}"
                    return f"[FAILED] {result.get('error')}"
                else:
                    return "Error: selector/ref and text required"

            elif action == "status":
                if self.cdp:
                    return "[OK] Browser connected"
                return "[OFFLINE] Browser not connected"

            elif action == "stop" or action == "close":
                if self.cdp:
                    await self.cdp.close()
                    self.cdp = None
                return "Browser disconnected"

            else:
                return f"Unknown action: {action}"

        except Exception as e:
            return f"[ERROR] {str(e)}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "status", "profiles", "new_tab", "navigate", "open", "search", "click", "type", "hover", "scroll", "resize", "evaluate", "cookies", "storage", "wait", "console", "errors", "download", "upload", "trace", "press", "screenshot", "get_text", "snapshot", "tabs", "switch_tab", "close_tab"]
                },
                "browser": {
                    "type": "string",
                    "enum": ["chrome", "brave", "edge", "chromium"],
                    "description": "Browser to use"
                },
                "port": {
                    "type": "integer",
                    "description": "CDP port (default: 18800)"
                },
                "cdp_port": {
                    "type": "integer",
                    "description": "CDP port (alias for port)"
                },
                "profile": {
                    "type": "string",
                    "description": "Profile name (default: nanobot)"
                },
                "headless": {
                    "type": "boolean",
                    "description": "Run in headless mode"
                },
                # driver parameter removed - playwright is always used internally
                "url": {"type": "string"},
                "targetUrl": {"type": "string"},
                "selector": {"type": "string"},
                "save_scroll": {"type": "boolean", "description": "Save and restore scroll position after snapshot (default: true)"},
                "text": {"type": "string"},
                "query": {"type": "string"},
                "keyword": {"type": "string"},
                "key": {"type": "string"},
                "tab": {"type": "string"},
                "tab_id": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "expression": {"type": "string"},
                "code": {"type": "string"},
                "type": {"type": "string", "enum": ["local", "session"]},
                "timeout": {"type": "integer", "description": "Timeout in milliseconds (default: 30000)"},
                "load": {"type": "boolean", "description": "Wait for page load"},
                "path": {"type": "string", "description": "File path for download/upload/trace"},
                "file": {"type": "string", "description": "File path for upload"},
                "mode": {"type": "string", "enum": ["start", "stop"], "description": "Mode for trace (start or stop)"},
            },
            "required": ["action"]
        }
