"""Playwright-based browser client for reliable automation."""

import asyncio
import json
from typing import Any

from loguru import logger


class PlaywrightClient:
    """Playwright-based browser client that connects to browser via CDP.

    This client uses Playwright's accessibility APIs for more reliable
    click/type operations, especially for React/SPA applications like Xiaohongshu.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 18800):
        self.host = host
        self.port = port
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self._connected = False
        self._ref_map = {}  # Map from ref (e1, e2) to accessibility node

    async def connect(self):
        """Connect to browser via CDP using Playwright."""
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        cdp_url = f"http://{self.host}:{self.port}"

        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(cdp_url)
            # Get the first context (usually default)
            contexts = self.browser.contexts
            if contexts:
                self.context = contexts[0]
            else:
                self.context = await self.browser.new_context()

            # Get the most relevant page (prefer xiaohongshu pages)
            pages = self.context.pages
            if pages:
                # First, prefer xiaohongshu pages
                xiaohongshu_pages = [p for p in pages if 'xiaohongshu' in p.url]
                if xiaohongshu_pages:
                    self.page = xiaohongshu_pages[0]
                else:
                    # Then prefer non-new-tab pages
                    non_newtab = [p for p in pages if not p.url.startswith('chrome://new-tab')]
                    if non_newtab:
                        self.page = non_newtab[0]
                    else:
                        self.page = pages[0]
            else:
                self.page = await self.context.new_page()

            self._connected = True
        except Exception as e:
            await self.close()
            raise ConnectionError(f"Failed to connect to browser at {cdp_url}: {e}")

    async def close(self):
        """Close the connection."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self._connected = False
        self.browser = None
        self.playwright = None
        self.context = None
        self.page = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self.page is not None

    async def _ensure_page(self):
        """Ensure we have the correct page (not new-tab)."""
        if not self.context:
            return

        pages = self.context.pages
        if not pages:
            self.page = await self.context.new_page()
            return

        # Find non-newtab page
        non_newtab = [p for p in pages if not p.url.startswith('chrome://new-tab')]
        if non_newtab:
            self.page = non_newtab[0]
        elif pages:
            # If only newtab exists, don't use it - create new one
            pass

    async def navigate(self, url: str) -> dict:
        """Navigate to URL."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)  # Wait for dynamic content
            return {"success": True, "url": url}
        except Exception as e:
            return {"error": str(e)}

    async def get_content(self) -> str:
        """Get page content/text."""
        if not self.is_connected:
            return ""

        try:
            # Use JavaScript to extract text content
            text = await self.page.evaluate("""
                (function() {
                    // Get body text
                    var body = document.body;
                    if (!body) return '';

                    // Clone and clean
                    var clone = body.cloneNode(true);

                    // Remove script and style elements
                    var scripts = clone.querySelectorAll('script, style, noscript');
                    scripts.forEach(function(el) { el.remove(); });

                    return clone.innerText || clone.textContent || '';
                })()
            """)
            return text[:10000] if text else ""
        except Exception as e:
            return f"Error: {e}"

    async def get_snapshot(self, max_nodes: int = 50, interactive: bool = True, use_dom: bool = False, save_scroll: bool = True) -> dict:
        """Get snapshot with refs - auto-fallback to DOM if ARIA is insufficient.

        参数:
        - max_nodes: 最大返回元素数
        - interactive: 只返回可交互元素 (button, link, textbox 等)
        - use_dom: 强制使用 DOM 方式
        - save_scroll: 保留滚动位置 (仅用于 API 兼容性，Playwright 不需要)
        """
        if not self.is_connected:
            return {"error": "Not connected"}

        import re

        # 如果强制使用 DOM 方式
        if use_dom:
            return await self.get_snapshot_dom(max_nodes)

        try:
            # 1. 等待网络请求完成
            try:
                await self.page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            await asyncio.sleep(0.5)

            # 2. 尝试 ARIA snapshot
            aria_text = await self.page.locator(':root').aria_snapshot()

            # 3. 检查 ARIA 是否捕获了足够元素
            if aria_text:
                lines = aria_text.split('\n')
                aria_link_count = sum(1 for l in lines if '- link' in l or '- button' in l)

                # 如果 ARIA 捕获太少，回退到 DOM
                if aria_link_count < 10:
                    logger.warning(f"[Playwright] ARIA snapshot only captured {aria_link_count} links, using DOM instead")
                    return await self.get_snapshot_dom(max_nodes)
            else:
                return await self.get_snapshot_dom(max_nodes)

            # 4. 如果没有 ARIA 内容
            if not aria_text:
                return {"error": "Empty ARIA snapshot"}

            # 5. 解析 ARIA 文本
            lines = aria_text.split('\n')
            elements = []
            ref_map = {}
            counter = 0

            # OpenClaw 的 INTERACTIVE_ROLES
            INTERACTIVE_ROLES = {'button', 'link', 'textbox', 'checkbox', 'radio',
                                'combobox', 'listbox', 'menuitem', 'option',
                                'searchbox', 'slider', 'spinbutton', 'switch',
                                'tab', 'treeitem'}

            # 也包含有名称的内容元素 (OpenClaw 行为)
            CONTENT_ROLES = {'heading', 'cell', 'gridcell', 'row', 'columnheader', 'description'}

            # Track nth for duplicate (role, name) 组合
            name_tracker = {}

            for line in lines:
                line_stripped = line.strip()
                if not line_stripped.startswith('- '):
                    continue

                # OpenClaw regex: /^(\s*-\s*)(\w+)(?:\s+"([^"]*)")?(.*)$/
                match = re.match(r'^(\s*-\s*)(\w+)(?:\s+"([^"]*)")?(.*)$', line_stripped)
                if not match:
                    continue

                # Python: use groups() instead of destructuring
                groups = match.groups()
                indent = groups[0]
                role_raw = groups[1]
                name = groups[2] if len(groups) > 2 and groups[2] else None
                suffix = groups[3] if len(groups) > 3 else ''

                # 跳过路径
                if role_raw.startswith('/'):
                    continue

                role = role_raw.lower()

                # 根据 interactive 参数决定是否包含
                if interactive:
                    # 只包含 interactive 角色
                    if role not in INTERACTIVE_ROLES:
                        continue
                else:
                    # 包含 interactive + 有名称的内容元素
                    if role not in INTERACTIVE_ROLES and role not in CONTENT_ROLES:
                        continue
                    # 没有 name 的内容元素跳过
                    if not name and role in CONTENT_ROLES:
                        continue

                # Track nth
                key = (role, name or '')
                count = name_tracker.get(key, 0)
                name_tracker[key] = count + 1

                counter += 1
                ref = f"e{counter}"

                elements.append({
                    "ref": ref,
                    "role": role,
                    "name": (name or '')[:60],
                    "tag": role,
                })

                ref_map[ref] = {
                    "role": role,
                    "name": name or '',
                    "nth": count
                }

                if counter >= max_nodes:
                    break

            self._ref_map = ref_map

            return {
                "elements": elements,
                "ref_map": ref_map,
                "total_lines": len(lines)
            }

        except Exception as e:
            return {"error": f"Snapshot failed: {e}"}

    async def click_by_ref(self, ref: str) -> dict:
        """Click element by ref - 支持 ARIA 和 DOM 两种方式."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            # Parse ref (e1, e2, etc.)
            if not ref.startswith('e'):
                return {"error": f"Invalid ref format: {ref}. Use e1, e2..."}

            # Get current snapshot if not already cached
            if not self._ref_map:
                await self.get_snapshot()

            # Get the target node
            if ref not in self._ref_map:
                return {"error": f"Element {ref} not found in snapshot. Run snapshot first."}

            info = self._ref_map[ref]
            role = info.get('role', '')
            name = info.get('name', '')
            nth = info.get('nth', 0)
            href = info.get('href', '')
            tag = info.get('tag', '')

            # 方式1: 如果有 href，尝试通过 href 查找并点击
            if href:
                try:
                    # Try to find by href
                    locator = self.page.locator(f'a[href*="{href[:30]}"]').first
                    if await locator.count() > 0:
                        await locator.click(force=True, timeout=5000)
                        await asyncio.sleep(1)
                        return {"success": True, "ref": ref, "method": "href"}
                except Exception as e:
                    pass

            # 方式2: getByRole + exact name
            if name and role:
                try:
                    locator = self.page.get_by_role(role, name=name, exact=True)
                    if nth > 0:
                        locator = locator.nth(nth)
                    await locator.click(force=True, timeout=5000)
                    await asyncio.sleep(1)
                    return {"success": True, "ref": ref, "method": "getByRole"}
                except Exception as e:
                    pass

            # 方式3: getByRole without exact
            if name and role:
                try:
                    locator = self.page.get_by_role(role, name=name)
                    if nth > 0:
                        locator = locator.nth(nth)
                    await locator.click(force=True, timeout=5000)
                    await asyncio.sleep(1)
                    return {"success": True, "ref": ref, "method": "getByRole-inexact"}
                except Exception as e:
                    pass

            # 方式4: 使用文本查找
            # 但对于 section 标签，先尝试 section.note-item（因为文本匹配可能匹配到侧边栏）
            if tag == 'section':
                try:
                    locator = self.page.locator('section.note-item')
                    count = await locator.count()
                    if nth < count:
                        await locator.nth(nth).click(force=True, timeout=5000)
                        await asyncio.sleep(1)
                        return {"success": True, "ref": ref, "method": "section-note-item"}
                except Exception as e:
                    pass

            if name:
                try:
                    locator = self.page.get_by_text(name, exact=False).first
                    await locator.click(force=True, timeout=5000)
                    await asyncio.sleep(1)
                    return {"success": True, "ref": ref, "method": "getByText"}
                except Exception as e:
                    pass

            # 方式5: 只用 role
            if role:
                try:
                    locator = self.page.get_by_role(role)
                    if nth > 0:
                        locator = locator.nth(nth)
                    await locator.click(force=True, timeout=5000)
                    await asyncio.sleep(1)
                    return {"success": True, "ref": ref, "method": "role-only"}
                except Exception as e:
                    pass

            # 方式6: 对于小红书帖子，尝试点击 section.note-item
            if tag == 'section':
                try:
                    # Find all note-item sections and click the nth one
                    locator = self.page.locator('section.note-item')
                    count = await locator.count()
                    if nth < count:
                        # Use force=True to bypass overlays
                        await locator.nth(nth).click(force=True, timeout=5000)
                        await asyncio.sleep(1)
                        return {"success": True, "ref": ref, "method": "section-note-item"}
                except Exception as e:
                    pass

            # 方式7: 通过文本找到最近的 section 父元素并点击
            if name:
                try:
                    # Find element with text, then find parent section
                    js_code = f"""
                    (function() {{
                        var text = '{name}';
                        var els = document.querySelectorAll('*');
                        for (var i = 0; i < els.length; i++) {{
                            var el = els[i];
                            if (el.innerText && el.innerText.includes(text)) {{
                                var section = el.closest('section.note-item') || el.closest('[class*="note"]');
                                if (section) return section.className;
                            }}
                        }}
                        return null;
                    }})()
                    """
                    class_result = await self.page.evaluate(js_code)
                    if class_result:
                        locator = self.page.locator(f'.{class_result.split(" ").join(".")}').first
                        await locator.click(force=True, timeout=5000)
                        await asyncio.sleep(1)
                        return {"success": True, "ref": ref, "method": "closest-section"}
                except Exception as e:
                    pass

            return {"error": f"Could not click element {ref}", "reason": "all_strategies_failed"}

        except Exception as e:
            return {"error": f"Click failed: {e}", "reason": "exception"}

    async def click_with_retry(self, ref: str, max_retries: int = 2, highlight: bool = True) -> dict:
        """Click with retry - 失败后滚动页面再重试，提升稳定性.

        Args:
            ref: Element reference (e.g., 'e1')
            max_retries: Maximum number of retry attempts
            highlight: Whether to highlight the element before clicking
        """
        # 高亮元素
        if highlight:
            await self.highlight_element(ref, duration=2.0)

        for attempt in range(max_retries):
            result = await self.click_by_ref(ref)

            if result.get("success"):
                return result

            # 如果失败，滚动后再试
            if attempt < max_retries - 1:
                await self.scroll(0, 300)
                await asyncio.sleep(1)

        # 最后一次尝试也失败，返回详细错误
        return result

    async def type_by_ref(self, ref: str, text: str, highlight: bool = True) -> dict:
        """Type text into an element by ref - 使用 getByRole.

        Args:
            ref: Element reference (e.g., 'e1')
            text: Text to type
            highlight: Whether to highlight the element before typing
        """
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            # Parse ref
            if not ref.startswith('e'):
                return {"error": f"Invalid ref format: {ref}"}

            # 高亮元素
            if highlight:
                await self.highlight_element(ref, duration=2.0)

            # Get snapshot if needed
            if not self._ref_map:
                await self.get_snapshot()

            if ref not in self._ref_map:
                return {"error": f"Element {ref} not found. Run snapshot first."}

            info = self._ref_map[ref]
            role = info.get('role', '')
            name = info.get('name', '')
            nth = info.get('nth', 0)

            # Try get_by_role first
            if role in ['textbox', 'searchbox', 'combobox', 'textbox'] and name:
                try:
                    locator = self.page.get_by_role(role, name=name, exact=True)
                    if nth > 0:
                        locator = locator.nth(nth)
                    await locator.fill(text)
                    return {"success": True, "ref": ref}
                except Exception:
                    pass

            # Fallback: try get_by_label or get_by_placeholder
            if name:
                try:
                    await self.page.get_by_label(name).fill(text)
                    return {"success": True, "ref": ref}
                except Exception:
                    pass

                try:
                    await self.page.get_by_placeholder(name).fill(text)
                    return {"success": True, "ref": ref}
                except Exception:
                    pass

            return {"error": f"Could not type into element {ref}"}

        except Exception as e:
            return {"error": f"Type failed: {e}"}

    async def hover_by_ref(self, ref: str) -> dict:
        """Hover over an element by ref."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            if not ref.startswith('e'):
                return {"error": f"Invalid ref format: {ref}"}

            idx = int(ref[1:]) - 1
            target_ref = f"e{idx + 1}"

            if not self._ref_map or target_ref not in self._ref_map:
                await self.get_snapshot()

            if target_ref not in self._ref_map:
                return {"error": f"Element {ref} not found"}

            node = self._ref_map[target_ref]
            role = node.get('role', '')
            name = node.get('name', '').strip()

            if role and name:
                try:
                    await self.page.get_by_role(role, name=name).hover()
                    return {"success": True, "ref": ref}
                except Exception:
                    pass

            return {"error": f"Could not hover element {ref}"}

        except Exception as e:
            return {"error": f"Hover failed: {e}"}

    async def find_element(self, strategy: str, value: str, action: str = None, **kwargs) -> dict:
        """语义定位器支持 - 类似 agent-browser 的 find 命令.

        支持的策略:
        - role: find role button click --name "Submit"
        - text: find text "Sign In" click
        - label: find label "Email" fill "user@test.com"
        - first: find first ".item" click
        - nth: find nth 2 "a" text
        """
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            locator = None

            if strategy == "role":
                role = value
                name = kwargs.get("name", "")
                if name:
                    locator = self.page.get_by_role(role, name=name)
                else:
                    locator = self.page.get_by_role(role)

            elif strategy == "text":
                locator = self.page.get_by_text(value, exact=False)

            elif strategy == "label":
                locator = self.page.get_by_label(value, exact=False)

            elif strategy == "first":
                locator = self.page.locator(value).first

            elif strategy == "nth":
                index = int(kwargs.get("index", 0))
                locator = self.page.locator(value).nth(index)

            else:
                return {"error": f"Unknown strategy: {strategy}"}

            if not locator:
                return {"error": f"Could not find element with strategy {strategy}"}

            # 执行操作
            if action == "click":
                await locator.click(force=True, timeout=5000)
                await asyncio.sleep(1)
                return {"success": True, "strategy": strategy, "action": action}
            elif action == "fill":
                text = kwargs.get("text", "")
                await locator.fill(text)
                return {"success": True, "strategy": strategy, "action": action}
            elif action == "text":
                text = await locator.text_content()
                return {"success": True, "text": text}
            elif action == "hover":
                await locator.hover()
                return {"success": True, "strategy": strategy, "action": action}
            else:
                # 没有指定操作，返回 locator
                return {"success": True, "strategy": strategy, "action": "found"}

        except Exception as e:
            return {"error": f"Find failed: {e}"}

    async def evaluate(self, expression: str) -> dict:
        """Execute JavaScript in page context."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            result = await self.page.evaluate(expression)
            return {"success": True, "result": str(result)[:500]}
        except Exception as e:
            return {"error": str(e)}

    async def get_snapshot_dom(self, max_nodes: int = 50) -> dict:
        """Get DOM-based snapshot with refs - more reliable for SPA like Xiaohongshu."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            # Wait for page to stabilize
            try:
                await self.page.wait_for_load_state("networkidle", timeout=10000)
            except:
                pass

            # DOM-based approach - similar to CDP client
            # Include section.note-item for Xiaohongshu posts
            js_code = """
            (function() {
                var elements = [];
                var seen = new Set();

                // Get all clickable/interactive elements + section.note-item
                var selectors = [
                    'section.note-item',
                    'a', 'button', '[role="button"]', '[role="link"]',
                    'input[type="button"]', 'input[type="submit"]',
                    '[onclick]', '[data-clickable="true"]'
                ];

                for (var s = 0; s < selectors.length; s++) {
                    var els = document.querySelectorAll(selectors[s]);
                    for (var i = 0; i < els.length; i++) {
                        var el = els[i];

                        // Skip invisible elements
                        if (!el.offsetParent) continue;

                        var tag = el.tagName.toLowerCase();
                        var text = (el.innerText || el.textContent || '').trim().substring(0, 100);
                        var href = el.href || '';
                        var role = el.getAttribute('role') || tag;
                        var placeholder = el.getAttribute('placeholder') || '';

                        // Skip empty or very short text
                        if (!text && !placeholder && tag !== 'a' && tag !== 'section') continue;
                        if (text.length < 2 && tag !== 'section') continue;

                        // Skip duplicate-looking elements
                        var key = tag + '|' + text.substring(0, 30);
                        if (seen.has(key)) continue;
                        seen.add(key);

                        elements.push({
                            tag: tag,
                            role: role,
                            text: text,
                            href: href.substring(0, 100),
                            placeholder: placeholder
                        });

                        if (elements.length >= arguments[0]) break;
                    }
                    if (elements.length >= arguments[0]) break;
                }

                return JSON.stringify(elements);
            })()
            """.replace('arguments[0]', str(max_nodes))

            result = await self.page.evaluate(js_code)

            import json
            try:
                dom_elements = json.loads(result) if result else []
            except:
                return {"error": "Failed to parse DOM elements"}

            # Convert to our format
            elements = []
            ref_map = {}

            for i, el in enumerate(dom_elements):
                ref = f"e{i + 1}"
                elements.append({
                    "ref": ref,
                    "role": el.get('role', el.get('tag', '')),
                    "name": el.get('text', '')[:60],
                    "tag": el.get('tag', ''),
                    "href": el.get('href', ''),
                    "placeholder": el.get('placeholder', '')
                })

                ref_map[ref] = {
                    "role": el.get('role', el.get('tag', '')),
                    "name": el.get('text', ''),
                    "tag": el.get('tag', ''),
                    "href": el.get('href', ''),
                    "nth": i  # Use index as nth
                }

            self._ref_map = ref_map

            return {
                "elements": elements,
                "ref_map": ref_map,
                "total": len(dom_elements),
                "method": "dom"
            }

        except Exception as e:
            return {"error": f"DOM snapshot failed: {e}"}

    async def highlight_element(self, ref: str, duration: float = 2.0, color: str = "#ff0000") -> dict:
        """Highlight an element by ref with a colored border.

        Args:
            ref: Element reference (e.g., 'e1')
            duration: How long to keep the highlight (seconds)
            color: Border color (CSS color or hex)

        Returns:
            {"success": True, "ref": ref}
        """
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            # Get element info from ref_map
            if not self._ref_map or ref not in self._ref_map:
                logger.warning(f"[Playwright] highlight: ref {ref} not in _ref_map, trying to get snapshot")
                await self.get_snapshot()

            if ref not in self._ref_map:
                return {"error": f"Element {ref} not found in ref_map"}

            info = self._ref_map[ref]
            role = info.get('role', '')
            name = info.get('name', '')
            nth = info.get('nth', 0)

            # Build JavaScript to highlight element
            js_code = f"""
            async () => {{
                // Find element by role and name
                const role = "{role}";
                const name = "{name}";
                const nth = {nth};

                let element = null;
                if (role && name) {{
                    const candidates = Array.from(document.querySelectorAll('[role="' + role + '"]')).filter(el => {{
                        const label = el.getAttribute('aria-label') || el.textContent || '';
                        return label.includes("{name}") || el.getAttribute('name') === "{name}";
                    }});
                    if (candidates.length > nth) {{
                        element = candidates[nth];
                    }}
                }}

                // Fallback: try to find by text content
                if (!element && name) {{
                    const texts = Array.from(document.querySelectorAll('button, a, [role="button"], [role="link"]'));
                    const matches = texts.filter(el => el.textContent.includes("{name}"));
                    if (matches.length > nth) {{
                        element = matches[nth];
                    }}
                }}

                if (!element) {{
                    return {{ error: 'Element not found for highlighting' }};
                }}

                // Apply highlight style
                const originalOutline = element.style.outline;
                const originalBoxShadow = element.style.boxShadow;
                element.style.outline = '3px solid {color}';
                element.style.boxShadow = '0 0 10px {color}';
                element.style.zIndex = '999999';
                element.style.position = 'relative';

                // Remove highlight after duration
                setTimeout(() => {{
                    element.style.outline = originalOutline;
                    element.style.boxShadow = originalBoxShadow;
                    element.style.zIndex = '';
                    element.style.position = '';
                }}, {duration * 1000});

                return {{ success: true, tag: element.tagName }};
            }}
            """

            result = await self.page.evaluate(js_code)
            logger.info(f"[Playwright] highlighted element {ref}: {result}")
            return {"success": True, "ref": ref, "result": result}

        except Exception as e:
            logger.error(f"[Playwright] highlight failed: {e}")
            return {"error": str(e)}

    async def scroll(self, x: int = 0, y: int = 0) -> dict:
        """Scroll the page."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            await self.page.evaluate(f"window.scrollTo({x}, {y})")
            await asyncio.sleep(0.5)
            return {"success": True}
        except Exception as e:
            return {"error": str(e)}

    async def scroll_to_selector(self, selector: str) -> dict:
        """Scroll to a specific selector."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            await self.page.locator(selector).scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
            return {"success": True}
        except Exception as e:
            return {"error": str(e)}

    async def resize_viewport(self, width: int, height: int) -> dict:
        """Resize the viewport."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            await self.page.set_viewport_size({"width": width, "height": height})
            return {"success": True}
        except Exception as e:
            return {"error": str(e)}

    async def take_screenshot(self, path: str) -> dict:
        """Take a screenshot."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            await self.page.screenshot(path=path)
            return {"success": True, "path": path}
        except Exception as e:
            return {"error": str(e)}

    async def get_url(self) -> str:
        """Get current URL."""
        if not self.is_connected:
            return ""

        return self.page.url

    async def list_tabs(self) -> dict:
        """List all tabs/pages."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            pages = self.context.pages
            tabs = []
            for i, p in enumerate(pages):
                tabs.append({
                    "id": f"t{i+1}",
                    "title": p.title[:40] if p.title else "Untitled",
                    "url": p.url
                })
            current = self.page.url
            return {"tabs": tabs, "current": current}
        except Exception as e:
            return {"error": str(e)}

    async def create_tab(self, url: str = "about:blank") -> dict:
        """Create a new tab."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            new_page = await self.context.new_page()
            if url and url != "about:blank":
                await new_page.goto(url)
            return {"success": True}
        except Exception as e:
            return {"error": str(e)}

    async def switch_tab(self, tab_id: str) -> dict:
        """Switch to a tab."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            # Parse tab_id (t1, t2, etc.)
            if tab_id.startswith('t'):
                idx = int(tab_id[1:]) - 1
            else:
                idx = int(tab_id) - 1

            pages = self.context.pages
            if 0 <= idx < len(pages):
                self.page = pages[idx]
                return {"success": True}
            return {"error": f"Tab {tab_id} not found"}
        except Exception as e:
            return {"error": str(e)}

    async def close_tab(self, tab_id: str) -> dict:
        """Close a tab."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            if tab_id.startswith('t'):
                idx = int(tab_id[1:]) - 1
            else:
                idx = int(tab_id) - 1

            pages = self.context.pages
            if 0 <= idx < len(pages):
                await pages[idx].close()
                # Update current page
                if self.page in pages:
                    self.page = pages[0] if pages else None
                return {"success": True}
            return {"error": f"Tab {tab_id} not found"}
        except Exception as e:
            return {"error": str(e)}

    async def press_key(self, key: str) -> dict:
        """Press a keyboard key."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            await self.page.keyboard.press(key)
            return {"success": True}
        except Exception as e:
            return {"error": str(e)}

    async def get_cookies(self) -> dict:
        """Get cookies."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            cookies = await self.context.cookies()
            return {"success": True, "cookies": cookies}
        except Exception as e:
            return {"error": str(e)}

    async def get_local_storage(self) -> dict:
        """Get local storage."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            result = await self.page.evaluate("""JSON.stringify(localStorage)""")
            storage = json.loads(result) if result else {}
            return {"success": True, "storage": storage}
        except Exception as e:
            return {"error": str(e)}

    async def get_session_storage(self) -> dict:
        """Get session storage."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            result = await self.page.evaluate("""JSON.stringify(sessionStorage)""")
            storage = json.loads(result) if result else {}
            return {"success": True, "storage": storage}
        except Exception as e:
            return {"error": str(e)}

    async def wait_for_url(self, url_pattern: str, timeout: int = 30000) -> dict:
        """Wait for URL to match pattern."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            await self.page.wait_for_url(url_pattern, timeout=timeout)
            return {"success": True, "url": self.page.url}
        except Exception as e:
            return {"error": str(e)}

    async def wait_for_selector(self, selector: str, timeout: int = 30000) -> dict:
        """Wait for selector to appear."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            await self.page.wait_for_selector(selector, timeout=timeout)
            return {"success": True}
        except Exception as e:
            return {"error": str(e)}

    async def wait_for_load(self, state: str = "load", timeout: int = 30000) -> dict:
        """Wait for page load state."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            await self.page.wait_for_load_state(state, timeout=timeout)
            return {"success": True}
        except Exception as e:
            return {"error": str(e)}

    # Additional methods to match CDP client interface

    async def query_selector(self, selector: str) -> dict:
        """Query for a selector."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            element = await self.page.query_selector(selector)
            if element:
                return {"node_id": 1, "selector": selector}
            return {"error": "Element not found"}
        except Exception as e:
            return {"error": str(e)}

    async def type_text(self, selector: str, text: str) -> dict:
        """Type text into an element by selector."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            element = await self.page.query_selector(selector)
            if element:
                await element.fill(text)
                return {"success": True}
            return {"error": f"Element not found: {selector}"}
        except Exception as e:
            return {"error": str(e)}

    async def click_element(self, selector: str) -> dict:
        """Click an element by selector."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            element = await self.page.query_selector(selector)
            if element:
                await element.click()
                await asyncio.sleep(1)
                return {"success": True}
            return {"error": f"Element not found: {selector}"}
        except Exception as e:
            return {"error": str(e)}

    async def wait(self, url: str = "", selector: str = "", load: bool = False, timeout: int = 30000) -> dict:
        """Wait for URL, selector, or load state."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            if url:
                await self.page.wait_for_url(url, timeout=timeout)
                return {"success": True, "url": self.page.url}
            elif selector:
                await self.page.wait_for_selector(selector, timeout=timeout)
                return {"success": True}
            elif load:
                await self.page.wait_for_load_state("load", timeout=timeout)
                return {"success": True}
            return {"error": "No wait condition provided"}
        except Exception as e:
            return {"error": str(e)}

    async def get_console_messages(self) -> dict:
        """Get console messages from the page."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            # 使用 JavaScript 获取控制台消息
            messages = await self.page.evaluate("""
                (function() {
                    if (window.__console_messages) {
                        return window.__console_messages;
                    }
                    return [];
                })()
            """)
            return {"success": True, "messages": messages if messages else []}
        except Exception as e:
            return {"success": True, "messages": [], "note": str(e)}

    async def get_errors(self) -> dict:
        """Get page errors from console."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            # 获取 JavaScript 错误
            errors = await self.page.evaluate("""
                (function() {
                    return window.__js_errors || [];
                })()
            """)
            return {"success": True, "errors": errors if errors else []}
        except Exception as e:
            return {"success": True, "errors": [], "note": str(e)}

    async def enable_console_logging(self) -> dict:
        """启用控制台日志捕获 - 用于调试."""
        if not self.is_connected:
            return {"error": "Not connected"}

        try:
            # 注入 JavaScript 来捕获控制台消息
            await self.page.evaluate("""
                (function() {
                    window.__console_messages = [];
                    window.__js_errors = [];

                    // 捕获 console.log, console.warn, console.error
                    ['log', 'warn', 'error', 'info'].forEach(function(method) {
                        var original = console[method];
                        console[method] = function() {
                            window.__console_messages.push({
                                type: method,
                                args: Array.from(arguments).map(function(a) {
                                    try {
                                        return typeof a === 'object' ? JSON.stringify(a) : String(a);
                                    } catch(e) { return String(a); }
                                }),
                                timestamp: Date.now()
                            });
                            original.apply(console, arguments);
                        };
                    });

                    // 捕获 JS 错误
                    window.onerror = function(msg, url, line, col, error) {
                        window.__js_errors.push({
                            message: msg,
                            url: url,
                            line: line,
                            col: col,
                            timestamp: Date.now()
                        });
                        return false;
                    };
                })()
            """)
            return {"success": True, "message": "Console logging enabled"}
        except Exception as e:
            return {"error": str(e)}
