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
    description = """Browser automation using Chrome DevTools Protocol.

**Browser Management:**
- **start**: Start a browser instance (e.g., {"action": "start", "browser": "chrome"})
- **stop**: Stop the browser (e.g., {"action": "stop"})
- **status**: Check browser status (e.g., {"action": "status"})

**When user says "new tab", "open in new tab", "在新tab打开": MUST use new_tab action!**

Actions:
- **new_tab**: Create NEW tab and open URL (e.g., {"action": "new_tab", "url": "youtube.com"})
- navigate/open: Open URL in CURRENT tab (e.g., amazon.com)
- search: Search - uses direct URL (e.g., amazon.com/s?k=query)
- snapshot: Get page with element refs (e1, e2, e3...) - USE THIS FIRST
- click: Click by ref (e.g., {"action": "click", "ref": "e15"})
- type: Type by ref (e.g., {"action": "type", "ref": "e15", "text": "hello"})
- screenshot: Take screenshot
- get_text: Get page text
- press: Press key (Enter, Escape, etc)
- tabs: List all tabs
- switch_tab: Switch to tab (e.g., {"action": "switch_tab", "tab": "t2"})
- close_tab: Close tab

Parameters:
- browser: "chrome", "brave", "edge", or "chromium" (default: chrome)
- port: CDP port (default: 18800)
- profile: profile name (default: nanobot)
- headless: run in headless mode (default: false)

Examples:
- start: {"action": "start", "browser": "chrome", "port": 18800}
- stop: {"action": "stop"}
- status: {"action": "status"}
- snapshot: {"action": "snapshot"}
- click: {"action": "click", "ref": "e15"}
- search: {"action": "search", "query": "丹麦", "url": "小红书"}
- tabs: {"action": "tabs"}
- new_tab: {"action": "new_tab", "url": "youtube.com"}
- switch_tab: {"action": "switch_tab", "tab": "t2"}

TRUST [VERIFIED] results!"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.cdp = None
        self._lock = asyncio.Lock()
        self._manager = None
        self._port = DEFAULT_CDP_PORT
        self._browser = "chrome"

    def _get_manager(self):
        """Get browser manager instance."""
        if self._manager is None:
            from nanobot.agent.tools.browser_manager import BrowserManager
            self._manager = BrowserManager(workspace=self.workspace)
        return self._manager

    def _get_port(self, **kwargs) -> int:
        """Get port from kwargs or use default."""
        return kwargs.get("port", kwargs.get("cdp_port", DEFAULT_CDP_PORT))

    async def _get_client(self, port: int | None = None):
        """Get or create CDP client."""
        target_port = port or self._port
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

            # For all other actions, we need CDP connection
            cdp = await self._get_client(self._get_port(**kwargs))

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
                result = await cdp.get_snapshot()
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
                    text = el.get("text", "")
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

            elif action == "click":
                # Check if using ref (e1, e2) or selector
                selector = kwargs.get("selector", "")
                ref = kwargs.get("ref", "") or kwargs.get("key", "")  # Support both "ref" and "key"

                if ref:
                    # Use ref-based click
                    result = await cdp.click_by_ref(ref)
                    if result.get("success"):
                        current_url = result.get("current_url", "")
                        navigated_to = result.get("navigated_to", "")
                        if navigated_to:
                            return f"[VERIFIED] Clicked {ref}, navigated to: {navigated_to[:80]}"
                        if current_url:
                            return f"[VERIFIED] Clicked {ref}, now at: {current_url[:60]}"
                        return f"[VERIFIED] Clicked {ref} (no URL returned)"
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
                    "enum": ["start", "stop", "status", "new_tab", "navigate", "open", "search", "click", "type", "press", "screenshot", "get_text", "snapshot", "tabs", "switch_tab", "close_tab"]
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
                "url": {"type": "string"},
                "targetUrl": {"type": "string"},
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "query": {"type": "string"},
                "keyword": {"type": "string"},
                "key": {"type": "string"},
                "tab": {"type": "string"},
                "tab_id": {"type": "string"},
            },
            "required": ["action"]
        }
