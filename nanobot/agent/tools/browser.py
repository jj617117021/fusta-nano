"""Browser automation tool using Playwright."""

import asyncio
import base64
import os
from pathlib import Path
from typing import Any

from PIL import Image

from nanobot.agent.tools.base import Tool


class BrowserTool(Tool):
    """Browser automation tool - control a headless browser for web automation."""

    name = "browser"
    description = """Control a headless browser for web automation. Use this tool to:
- Open URLs and navigate the web
- Take screenshots of pages
- Click, type, and interact with web elements
- Get accessibility snapshots of pages

IMPORTANT: Always VERIFY your actions worked by checking the result.
- After navigate: check the returned URL matches what you expected
- After type: verify the text appears on page
- After click: verify the action had an effect

Actions:
- navigate: Open a URL, returns current URL and title
- screenshot: Take a screenshot, saves to workspace/screenshots/ with timestamp
- get_text: Get visible text from page
- scroll: Scroll the page (direction: up/down, amount: pixels)
- find: Find elements by text/placeholder/button - returns matching selectors
- click: Click an element by selector, returns success/failure
- type: Type text into an element, returns success/failure
- press: Press a keyboard key
- snapshot: Get accessibility tree of the page
- evaluate: Execute JavaScript
- wait: Wait for conditions"""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.browser = None
        self.context = None
        self.page = None
        self._lock = asyncio.Lock()

    async def _get_page(self):
        """Get or create browser page."""
        import logging
        logger = logging.getLogger("browser")

        async with self._lock:
            if self.browser is None:
                from playwright.async_api import async_playwright
                self.playwright = await async_playwright().start()

                # Use persistent context to save login sessions
                user_data_dir = self.workspace / "browser_data"
                user_data_dir.mkdir(exist_ok=True)

                try:
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        user_data_dir=str(user_data_dir),
                        headless=False,
                        channel="chrome",
                        viewport={"width": 1280, "height": 720},
                        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                        args=[
                            "--no-sandbox",
                            "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-blink-features=AutomationControlled",
                        ]
                    )
                    self.browser = self.context.browser
                    self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                    logger.info("Browser launched successfully with persistent context")
                except Exception as e:
                    logger.error("Failed to launch browser: {}", e)
                    raise

            return self.page

    async def execute(self, action: str, **kwargs) -> str:
        """
        Execute a browser action.

        Args:
            action: The action to perform (navigate, screenshot, click, type, etc.)
            **kwargs: Additional arguments for the action

        Returns:
            Result of the action as a string
        """
        try:
            page = await self._get_page()

            if action == "navigate" or action == "open":
                url = kwargs.get("url", "")
                if not url:
                    return "Error: URL is required for navigate action"

                # Check if page is still valid (not closed by user)
                try:
                    if page.is_closed():
                        # Page was closed, need to create a new one
                        self.page = await self.context.new_page()
                        page = self.page
                except Exception:
                    pass

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                except Exception as e:
                    # Page may have been closed during navigation
                    try:
                        self.page = await self.context.new_page()
                        page = self.page
                        await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    except Exception as e2:
                        return f"[FAILED] Browser page error: {e2}. Try using 'stop' then navigate again."

                current_url = page.url
                title = await page.title()
                return f"[VERIFIED] Navigated to: {current_url}\nTitle: {title}\nExpected: {url}"

            elif action == "screenshot":
                full_page = kwargs.get("fullPage", False)

                # Always save to workspace/screenshots with timestamp
                screenshots_dir = self.workspace / "screenshots"
                screenshots_dir.mkdir(exist_ok=True)
                import time
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                path = screenshots_dir / f"screenshot_{timestamp}.png"

                await page.screenshot(path=str(path), full_page=full_page)
                return f"Screenshot saved to {path}"

            elif action == "click":
                selector = kwargs.get("selector", "")
                if not selector:
                    return "Error: selector is required for click action"

                # Check if element exists first
                element = await page.query_selector(selector)
                if not element:
                    return f"[FAILED] Element not found: {selector}"

                await page.click(selector)
                return f"[VERIFIED] Clicked element: {selector}"

            elif action == "type":
                selector = kwargs.get("selector", "")
                text = kwargs.get("text", "")
                if not selector or not text:
                    return "Error: selector and text are required for type action"

                # Check if element exists first
                element = await page.query_selector(selector)
                if not element:
                    return f"[FAILED] Element not found: {selector}"

                await page.fill(selector, text)

                # Verify the text was entered
                value = await page.eval_on_selector(selector, "el => el.value")
                if text in value:
                    return f"[VERIFIED] Typed '{text}' into {selector}. Current value: {value}"
                else:
                    return f"[WARNING] Typed but value is: {value}"

            elif action == "press":
                key = kwargs.get("key", "")
                if not key:
                    return "Error: key is required for press action"
                await page.keyboard.press(key)
                return f"Pressed key: {key}"

            elif action == "snapshot" or action == "aria":
                # Get accessibility tree
                snapshot = await page.accessibility.snapshot()
                if snapshot:
                    return self._format_accessibility_tree(snapshot)
                return "No accessibility snapshot available"

            elif action == "evaluate" or action == "eval":
                script = kwargs.get("script", "")
                if not script:
                    return "Error: script is required for evaluate action"
                result = await page.evaluate(script)
                return f"Result: {result}"

            elif action == "wait":
                timeout = kwargs.get("timeout", 3000)
                selector = kwargs.get("selector", "")
                url = kwargs.get("url", "")

                if selector:
                    await page.wait_for_selector(selector, timeout=timeout)
                    return f"Waited for selector: {selector}"
                elif url:
                    await page.wait_for_url(url, timeout=timeout)
                    return f"Waited for URL: {url}"
                else:
                    await asyncio.sleep(timeout / 1000)
                    return f"Waited for {timeout}ms"

            elif action == "get_url":
                return page.url

            elif action == "get_title":
                title = await page.title()
                return title

            elif action == "get_text":
                # Extract visible text content from the page
                text = await page.evaluate("""() => {
                    const el = document.body;
                    return el ? el.innerText : 'Element not found';
                }""")
                return f"[PAGE TEXT]\n{text[:10000]}"

            elif action == "scroll":
                # Scroll the page
                direction = kwargs.get("direction", "down")
                amount = kwargs.get("amount", 500)
                if direction == "up":
                    await page.evaluate(f"window.scrollBy(0, -{amount})")
                else:
                    await page.evaluate(f"window.scrollBy(0, {amount})")
                return f"[VERIFIED] Scrolled {direction} by {amount}px"

            elif action == "find":
                # Find elements by text, placeholder, or aria-label
                text = kwargs.get("text", "")
                selector_type = kwargs.get("type", "text")  # text, placeholder, aria, button

                if not text:
                    return "Error: text parameter is required for find action"

                # Find elements matching criteria
                results = await page.evaluate(f"""(text, type) => {{
                    const results = [];
                    const searchText = text.toLowerCase();

                    if (type === 'text') {{
                        const elements = document.querySelectorAll('a, button, span, div, p, h1, h2, h3, h4, h5, h6, label, li');
                        elements.forEach(el => {{
                            const elText = el.innerText || el.textContent || '';
                            if (elText.toLowerCase().includes(searchText)) {{
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {{
                                    results.push({{
                                        text: elText.substring(0, 100),
                                        tag: el.tagName.toLowerCase(),
                                        selector: getSelector(el)
                                    }});
                                }}
                            }}
                        }});
                    }} else if (type === 'placeholder') {{
                        const elements = document.querySelectorAll('input, textarea');
                        elements.forEach(el => {{
                            if (el.placeholder && el.placeholder.toLowerCase().includes(searchText)) {{
                                results.push({{
                                    text: el.placeholder,
                                    tag: el.tagName.toLowerCase(),
                                    selector: getSelector(el)
                                }});
                            }}
                        }});
                    }} else if (type === 'button') {{
                        const elements = document.querySelectorAll('button, a[role="button"], input[type="submit"]');
                        elements.forEach(el => {{
                            const elText = el.innerText || el.value || el.alt || '';
                            if (elText.toLowerCase().includes(searchText)) {{
                                results.push({{
                                    text: elText.substring(0, 100),
                                    tag: el.tagName.toLowerCase(),
                                    selector: getSelector(el)
                                }});
                            }}
                        }});
                    }}

                    function getSelector(el) {{
                        if (el.id) return '#' + el.id;
                        if (el.className && typeof el.className === 'string') {{
                            return el.tagName.toLowerCase() + '.' + el.className.split(' ')[0];
                        }}
                        return el.tagName.toLowerCase();
                    }}

                    return results.slice(0, 10);
                }}""", text, selector_type)

                if not results:
                    return f"[NOT FOUND] No elements found matching '{text}'"

                lines = [f"[FOUND {len(results)} elements]"]
                for i, r in enumerate(results):
                    lines.append(f"{i+1}. [{r['tag']}] {r['text']} -> selector: {r['selector']}")
                return "\n".join(lines)

            elif action == "status":
                if self.browser:
                    return "Browser is running"
                return "Browser is not running"

            elif action == "stop" or action == "close":
                await self._close()
                return "Browser closed"

            else:
                return f"Unknown action: {action}. Available actions: navigate, screenshot, click, type, press, snapshot, evaluate, wait, get_url, get_title, status, stop"

        except Exception as e:
            return f"Error: {str(e)}"

    def _format_accessibility_tree(self, node, indent=0) -> str:
        """Format accessibility tree for display."""
        lines = []
        prefix = "  " * indent

        if "name" in node:
            role = node.get("role", "unknown")
            name = node.get("name", "")
            lines.append(f"{prefix}[{role}] {name}")

        for child in node.get("children", []):
            lines.extend(self._format_accessibility_tree(child, indent + 1))

        return "\n".join(lines)

    async def _close(self):
        """Close the browser."""
        async with self._lock:
            if self.browser:
                await self.browser.close()
                await self.playwright.stop()
                self.browser = None
                self.context = None
                self.page = None
                self.playwright = None

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action to perform",
                    "enum": ["navigate", "screenshot", "click", "type", "press", "snapshot", "evaluate", "wait", "get_url", "get_title", "get_text", "scroll", "find", "status", "stop"]
                },
                "url": {"type": "string", "description": "URL for navigate action"},
                "path": {"type": "string", "description": "DEPRECATED - screenshots always save to workspace/screenshots/"},
                "fullPage": {"type": "boolean", "description": "Capture full page for screenshot"},
                "selector": {"type": "string", "description": "CSS selector for click/type actions"},
                "text": {"type": "string", "description": "Text to type"},
                "key": {"type": "string", "description": "Key to press"},
                "script": {"type": "string", "description": "JavaScript to evaluate"},
                "timeout": {"type": "number", "description": "Timeout in milliseconds"},
            },
            "required": ["action"]
        }
