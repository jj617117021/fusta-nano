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
    description = """Browser automation. Actions:

- navigate: Open URL
- search: Search - auto-finds search box, enters query, presses Enter
- click_text: Click element by text - RETURNS NEW URL if navigated!
- screenshot: Take screenshot (ONLY if user asks)
- get_text: Get page text
- scroll: Scroll
- find: Find elements - returns selectors
- click: Click by selector
- type: Type text
- press: Press key
- snapshot: Get accessibility tree

TRUST the result! [VERIFIED] means it worked. NO more tools after success!"""

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

            elif action == "search":
                # Quick search - use Playwright fill for reliability
                query = kwargs.get("query") or kwargs.get("text") or kwargs.get("keyword") or ""
                url_param = kwargs.get("url", "")  # Get URL parameter!

                if not query:
                    return "Error: query required"

                # If URL parameter is provided, navigate there first
                if url_param:
                    try:
                        await page.goto(url_param, wait_until="domcontentloaded", timeout=10000)
                        await asyncio.sleep(1)
                    except Exception as e:
                        pass  # Continue anyway

                # Try to find and fill search input using Playwright
                search_selectors = [
                    'input[type="search"]',
                    'input[type="text"]',
                    'input[placeholder*="搜索"]',
                    'input[placeholder*="Search"]',
                    '#search-input',
                    '.search-input',
                    'input[name="q"]',
                    'input[placeholder="搜索"]'
                ]

                selector_found = None
                for sel in search_selectors:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.fill(query)
                            selector_found = sel
                            break
                    except:
                        continue

                if selector_found:
                    await page.press(selector_found, 'Enter')
                    await asyncio.sleep(1)
                    return f"[VERIFIED] Searched: {query}"
                else:
                    # Try common sites if no search box
                    current_url = page.url.lower()
                    if 'amazon' not in current_url and 'xiaohongshu' not in current_url:
                        # Auto-navigate to common site based on query or default
                        if 'amazon' in query.lower():
                            await page.goto("https://www.amazon.com", wait_until="domcontentloaded", timeout=10000)
                        elif '小红书' in query or 'xiao' in query.lower():
                            await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=10000)
                            await asyncio.sleep(2)
                        elif 'youtube' in query.lower():
                            await page.goto("https://www.youtube.com", wait_until="domcontentloaded", timeout=10000)
                        else:
                            return "[FAILED] No search box found. Navigate to website first."

                    # Try again
                    for sel in search_selectors:
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                await el.fill(query)
                                await page.press(sel, 'Enter')
                                await asyncio.sleep(1)
                                return f"[VERIFIED] Searched: {query}"
                        except:
                            continue

                    return "[FAILED] No search box found"

            elif action == "click_text":
                # Click element by text content - more robust
                text = kwargs.get("text", "") or kwargs.get("query", "") or kwargs.get("keyword", "")
                if not text:
                    return "Error: text required for click_text"

                url_before = page.url

                # Click and wait for navigation or content change
                clicked = await page.evaluate("""(searchText) => {
                    const selectors = ['a', 'button', '.s-item-link', '.a-link-normal', '[class*="item"]', 'span', 'div'];
                    const lower = searchText.toLowerCase();

                    // Try to find and click
                    for (const sel of selectors) {
                        const elements = document.querySelectorAll(sel);
                        for (const el of elements) {
                            const elText = (el.innerText || el.textContent || '').trim();
                            if (elText.toLowerCase().includes(lower) && elText.length < 100) {
                                const rect = el.getBoundingClientRect();
                                if (rect.width > 5 && rect.height > 5) {
                                    el.click();
                                    return el.tagName;
                                }
                            }
                        }
                    }
                    return null;
                }""", text)

                if clicked:
                    # Wait a bit for potential navigation
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=3000)
                    except:
                        pass
                    await asyncio.sleep(1)

                    url_after = page.url
                    title = await page.title() if page.url != url_before else ""

                    if url_after != url_before:
                        return f"[VERIFIED] Clicked '{text}'. Now at: {url_after[:80]}"
                    else:
                        return f"[VERIFIED] Clicked '{text}'. Stayed on same page."
                return f"[FAILED] No clickable element found with: {text}"

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
                    "enum": ["navigate", "search", "click_text", "screenshot", "click", "type", "press", "snapshot", "evaluate", "wait", "get_url", "get_title", "get_text", "scroll", "find", "status", "stop"]
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
