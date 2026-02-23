"""Browser automation tool using Chrome DevTools Protocol (CDP)."""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional
import websockets
import websockets.client as ws_client


class CDPClient:
    """Chrome DevTools Protocol client."""

    def __init__(self, host: str = "127.0.0.1", port: int = 18800):
        self.host = host
        self.port = port
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.target_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self.ref_map: dict = {}  # Maps refs (e1, e2) to nodeIds
        self._id = 0

    async def connect(self):
        """Connect to Chrome."""
        # Get the WebSocket URL
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://{self.host}:{self.port}/json/version")
            data = resp.json()
            ws_url = data["webSocketDebuggerUrl"]

        # Connect to WebSocket
        self.ws = await ws_client.connect(ws_url)

        # Get targets and attach to the first one
        await self._send("Target.getTargets", {})
        response = await self._recv()
        targets = response.get("result", {}).get("targetInfos", [])

        # Find the main page
        for target in targets:
            if target.get("type") == "page":
                self.target_id = target.get("targetId")
                break

        if not self.target_id:
            # Create a new page
            result = await self._send_and_wait("Target.createTarget", {"url": "about:blank"})
            self.target_id = result.get("result", {}).get("targetId")

        # Attach to the target
        result = await self._send_and_wait("Target.attachToTarget", {
            "targetId": self.target_id,
            "flatten": True
        })
        self.session_id = result.get("result", {}).get("sessionId")

        # Enable required domains
        await self._send_and_wait("DOM.enable")
        await self._send_and_wait("Runtime.enable")

    async def _send(self, method: str, params: dict = None):
        """Send a CDP command."""
        if params is None:
            params = {}
        self._id += 1
        message = {
            "id": self._id,
            "method": method,
            "params": params
        }
        if self.session_id:
            message["sessionId"] = self.session_id
        await self.ws.send(json.dumps(message))

    async def _recv(self):
        """Receive a CDP response."""
        response = await self.ws.recv()
        return json.loads(response)

    async def _send_and_wait(self, method: str, params: dict = None):
        """Send a command and wait for response."""
        await self._send(method, params)
        while True:
            response = await self._recv()
            if response.get("id") == self._id:
                return response

    async def navigate(self, url: str):
        """Navigate to URL."""
        # Auto-add https:// if missing
        if url and not url.startswith("http"):
            # Add https://www. prefix for common domains
            if "." not in url:
                # Maybe it's a domain without TLD, try adding .com
                url = f"https://www.{url}.com"
            else:
                url = f"https://www.{url}" if not url.startswith("www.") else f"https://{url}"

        try:
            result = await self._send_and_wait("Page.navigate", {"url": url})
            return result
        except Exception as e:
            # Try to reconnect and retry
            await self._reconnect()
            result = await self._send_and_wait("Page.navigate", {"url": url})
            return result

    async def reload(self):
        """Reload the page."""
        await self._send_and_wait("Page.reload")

    async def get_document(self):
        """Get the document."""
        result = await self._send_and_wait("DOM.getDocument")
        return result.get("result", {}).get("root", {})

    async def query_selector(self, selector: str):
        """Query for a selector."""
        doc = await self.get_document()
        result = await self._send_and_wait("DOM.querySelector", {
            "nodeId": doc.get("nodeId"),
            "selector": selector
        })
        return result.get("result", {}).get("nodeId")

    async def click_element(self, selector: str):
        """Click an element."""
        node_id = await self.query_selector(selector)
        if not node_id:
            return {"error": "Element not found"}

        # Get box model for clicking
        result = await self._send_and_wait("DOM.getBoxModel", {"nodeId": node_id})
        model = result.get("result", {}).get("model", {})
        if model:
            content = model.get("content", [])
            if len(content) >= 4:
                x = (content[0] + content[2]) / 2
                y = (content[1] + content[5]) / 2

                # Input mouse click
                await self._send("Input.dispatchMouseEvent", {
                    "type": "mousePressed",
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1
                })
                await self._send("Input.dispatchMouseEvent", {
                    "type": "mouseReleased",
                    "x": x,
                    "y": y,
                    "button": "left"
                })
                return {"success": True}

        return {"error": "Could not get element position"}

    async def type_text(self, selector: str, text: str):
        """Type text into an element."""
        node_id = await self.query_selector(selector)
        if not node_id:
            return {"error": "Element not found"}

        # Focus the element
        await self._send_and_wait("DOM.focus", {"nodeId": node_id})

        # Type character by character
        for char in text:
            await self._send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "text": char,
                "key": char
            })
            await self._send("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "text": char,
                "key": char
            })

        return {"success": True}

    async def click_by_ref(self, ref: str):
        """Click an element by ref using CDP mouse events."""
        # Extract index from ref (e1 -> 0, e2 -> 1, p1 -> 0, p2 -> 1)
        try:
            # Support both "e" (elements) and "p" (products) prefixes
            prefix = ref[0].lower()
            if prefix not in ('e', 'p'):
                return {"error": f"Invalid ref format: {ref}. Use e1, e2... or p1, p2..."}
            idx = int(ref[1:]) - 1
        except:
            return {"error": f"Invalid ref format: {ref}. Use e1, e2... or p1, p2..."}

        # Get element position using JavaScript
        is_product = ref[0].lower() == 'p'
        if is_product:
            # For products/posts, find links with priority
            js_code = f"""
            (function() {{
                var allLinks = [];
                // Priority 1: Xiaohongshu item pages (search_result and explore)
                var links = document.querySelectorAll('a[href*="/search_result/"], a[href*="/explore/"]');
                for (var i = 0; i < links.length; i++) {{
                    var el = links[i];
                    if (!el.href) continue;
                    if (allLinks.some(function(l) {{ return l.href === el.href; }})) continue;
                    var text = (el.innerText || el.alt || '').trim().substring(0, 60);
                    allLinks.push({{href: el.href, text: text, priority: 1}});
                }}
                // Priority 2: Amazon products
                links = document.querySelectorAll('a[href*="/dp/"], a[href*="/gp/product/"]');
                for (var i = 0; i < links.length; i++) {{
                    var el = links[i];
                    if (!el.href) continue;
                    if (allLinks.some(function(l) {{ return l.href === el.href; }})) continue;
                    var text = (el.innerText || el.alt || '').trim().substring(0, 60);
                    allLinks.push({{href: el.href, text: text, priority: 2}});
                }}
                // Priority 3: YouTube
                links = document.querySelectorAll('a[href*="/watch?v="]');
                for (var i = 0; i < links.length; i++) {{
                    var el = links[i];
                    if (!el.href) continue;
                    if (allLinks.some(function(l) {{ return l.href === el.href; }})) continue;
                    var text = (el.innerText || el.alt || '').trim().substring(0, 60);
                    allLinks.push({{href: el.href, text: text, priority: 3}});
                }}
                // Sort by priority
                allLinks.sort(function(a, b) {{ return a.priority - b.priority; }});
                if (allLinks.length === 0) {{
                    var debugLinks = [];
                    var all = document.querySelectorAll('a[href]');
                    for (var i = 0; i < Math.min(all.length, 5); i++) {{
                        debugLinks.push(all[i].href.substring(0, 50));
                    }}
                    return 'not_found|debug:' + debugLinks.join('|');
                }}
                if ({idx} >= allLinks.length) return 'not_found|index ' + {idx} + ' out of ' + allLinks.length;
                var target = allLinks[{idx}];
                return JSON.stringify({{href: target.href, text: target.text, total: allLinks.length}});
            }})()
            """

            result = await self._send_and_wait("Runtime.evaluate", {
                "expression": js_code,
                "returnByValue": True
            })

            json_str = result.get("result", {}).get("result", {}).get("value", "")
            if json_str.startswith("not_found"):
                return {"error": f"Click failed: {json_str}"}

            import json
            try:
                data = json.loads(json_str)
                product_url = data.get("href", "")
                if not product_url:
                    return {"error": f"XHS click failed: empty URL. Try e refs."}
                if product_url:
                    # Get element position first, then use mouse click
                    # First find the element and its position
                    js_pos = """
                    (function() {
                        var links = document.querySelectorAll('a[href*="/search_result/"], a[href*="/explore/"]');
                        for (var i = 0; i < links.length; i++) {
                            if (links[i].href.indexOf('/search_result/') > 0 || links[i].href.indexOf('/explore/') > 0) {
                                var rect = links[i].getBoundingClientRect();
                                return JSON.stringify({x: rect.left + rect.width/2, y: rect.top + rect.height/2});
                            }
                        }
                        return 'not_found';
                    })()
                    """
                    pos_result = await self._send_and_wait("Runtime.evaluate", {"expression": js_pos, "returnByValue": True})
                    pos_str = pos_result.get("result", {}).get("result", {}).get("value", "")

                    import json
                    if pos_str and pos_str != 'not_found':
                        try:
                            pos = json.loads(pos_str)
                            x, y = pos.get("x", 0), pos.get("y", 0)
                            # Use CDP mouse click
                            await self._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                            await asyncio.sleep(0.5)
                            await self._send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                            await self._send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left"})
                            await asyncio.sleep(3)
                            return {"success": True, "ref": ref, "clicked": True}
                        except:
                            pass

                    # Fallback to direct navigation
                    if product_url.startswith('/'):
                        product_url = "https://www.xiaohongshu.com" + product_url
                    await self.navigate(product_url)
                    await asyncio.sleep(3)
                    return {"success": True, "ref": ref, "navigated_to": product_url}
            except Exception as e:
                return {"error": f"Product click failed: {str(e)}"}
            return {"error": f"Product {ref} not found"}
        else:
            # For regular elements
            js_code = f"""
            (function() {{
                var refs = document.querySelectorAll('a, button, input, [onclick], [role="button"], img, div[data-clickable="true"]');
                var seen = new Set();
                var count = 0;
                for (var i = 0; i < refs.length; i++) {{
                    var el = refs[i];
                    var rect = el.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5 || el.hidden || el.disabled) continue;
                    var key = el.tagName + '-' + el.innerText.substring(0, 30) + '-' + rect.left + '-' + rect.top;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    if (count === {idx}) {{
                        return JSON.stringify({{
                            x: rect.left + rect.width / 2,
                            y: rect.top + rect.height / 2,
                            tag: el.tagName,
                            text: (el.innerText || el.alt || '').substring(0, 30)
                        }});
                    }}
                    count++;
                }}
                return 'not found';
            }})()
            """

        result = await self._send_and_wait("Runtime.evaluate", {
            "expression": js_code,
            "returnByValue": True
        })

        json_str = result.get("result", {}).get("result", {}).get("value", "")
        if json_str == "not found":
            return {"error": f"Element {ref} not found"}

        import json
        try:
            pos = json.loads(json_str)
        except:
            return {"error": "Failed to parse element position"}

        x, y = pos.get("x", 0), pos.get("y", 0)

        # Use CDP mouse events for real click
        await self._send("Input.dispatchMouseEvent", {
            "type": "mouseMoved",
            "x": x,
            "y": y
        })
        await self._send("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": x,
            "y": y,
            "button": "left",
            "clickCount": 1
        })
        await self._send("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": x,
            "y": y,
            "button": "left"
        })

        # Wait briefly for potential navigation
        await asyncio.sleep(1)

        # Check if URL changed
        url_result = await self._send_and_wait("Page.getNavigationHistory")
        current_url = ""
        if url_result.get("result"):
            entries = url_result.get("result", {}).get("entries", [])
            if entries:
                current_url = entries[-1].get("url", "")

        return {"success": True, "ref": ref, "current_url": current_url}

    async def type_by_ref(self, ref: str, text: str):
        """Type text into an element by ref (e1, e2, etc)."""
        # Extract index from ref (e1 -> 0, e2 -> 1)
        try:
            idx = int(ref[1:]) - 1
        except:
            return {"error": f"Invalid ref format: {ref}"}

        # Use JavaScript to type
        import json
        js_code = f"""
        (function() {{
            var refs = document.querySelectorAll('a, button, input, [onclick], [role="button"], img, div[data-clickable="true"]');
            var seen = new Set();
            var count = 0;
            for (var i = 0; i < refs.length; i++) {{
                var el = refs[i];
                var rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5 || el.hidden || el.disabled) continue;
                var key = el.tagName + '-' + el.innerText.substring(0, 30) + '-' + rect.left + '-' + rect.top;
                if (seen.has(key)) continue;
                seen.add(key);
                if (count === {idx}) {{
                    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {{
                        el.value = `{json.dumps(text)}`;
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        return 'typed';
                    }}
                    return 'not input';
                }}
                count++;
            }}
            return 'not found';
        }})()
        """

        result = await self._send_and_wait("Runtime.evaluate", {
            "expression": js_code,
            "returnByValue": True
        })

        value = result.get("result", {}).get("result", {}).get("value", "")
        if value == "typed":
            return {"success": True, "ref": ref}
        elif value == "not input":
            return {"error": f"Element {ref} is not an input"}
        return {"error": f"Element {ref} not found"}

    async def press_key(self, key: str):
        """Press a key."""
        # Map common keys
        key_map = {
            "Enter": "Enter",
            "Return": "Enter",
            "Escape": "Escape",
            "Esc": "Escape",
            "Tab": "Tab",
            "Backspace": "Backspace",
            "ArrowDown": "ArrowDown",
            "ArrowUp": "ArrowUp",
            "ArrowLeft": "ArrowLeft",
            "ArrowRight": "ArrowRight",
        }
        key = key_map.get(key, key)

        await self._send_and_wait("Input.dispatchKeyEvent", {
            "type": "keyDown",
            "key": key,
            "windowsVirtualKeyCode": 0
        })
        await self._send_and_wait("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": key,
            "windowsVirtualKeyCode": 0
        })

    async def get_content(self):
        """Get page content."""
        try:
            result = await self._send_and_wait("Runtime.evaluate", {
                "expression": "document.body.innerText",
                "returnByValue": True
            })
            return result.get("result", {}).get("result", {}).get("value", "")
        except Exception as e:
            # Try to reconnect and retry
            await self._reconnect()
            result = await self._send_and_wait("Runtime.evaluate", {
                "expression": "document.body.innerText",
                "returnByValue": True
            })
            return result.get("result", {}).get("result", {}).get("value", "")

    async def get_snapshot(self, max_nodes: int = 50):
        """Get DOM snapshot with element refs using JavaScript."""
        # Scroll to trigger lazy loading
        await self._send_and_wait("Runtime.evaluate", {
            "expression": "window.scrollTo(0, 500);"
        })
        await asyncio.sleep(1)
        await self._send_and_wait("Runtime.evaluate", {
            "expression": "window.scrollTo(0, 1000);"
        })
        await asyncio.sleep(1)

        # Use JavaScript to extract clickable elements - more comprehensive selector
        js_code = f"""
        (function() {{
            var elements = [];
            // Amazon product links
            var amazonLinks = document.querySelectorAll('a[href*="/dp/"], a[href*="/gp/product/"]');
            for (var i = 0; i < Math.min(amazonLinks.length, 20); i++) {{
                var el = amazonLinks[i];
                if (!el.href) continue;
                var text = (el.innerText || el.alt || '').trim().substring(0, 50);
                if (!text) continue;
                if (elements.some(function(e) {{ return e.href === el.href; }})) continue;
                elements.push({{
                    ref: 'p' + (elements.length + 1),
                    tag: 'a',
                    class: (el.className || '').split(' ')[0],
                    text: text,
                    href: el.href,
                    isProduct: true
                }});
            }}
            // Xiaohongshu post links
            var xhsLinks = document.querySelectorAll('a[href*="/search_result/"], a[href*="/explore/"], a[href*="/user/profile/"]');
            for (var i = 0; i < Math.min(xhsLinks.length, 20); i++) {{
                var el = xhsLinks[i];
                if (!el.href) continue;
                var text = (el.innerText || el.alt || '').trim().substring(0, 50);
                if (!text) continue;
                if (elements.some(function(e) {{ return e.href === el.href; }})) continue;
                elements.push({{
                    ref: 'p' + (elements.length + 1),
                    tag: 'a',
                    class: (el.className || '').split(' ')[0],
                    text: text,
                    href: el.href,
                    isProduct: true
                }});
            }}
            // Regular elements
            var selectors = [
                'a', 'button', 'input', 'textarea', 'select',
                '[role="button"]', '[role="link"]', '[onclick]',
                '[data-clickable="true"]', '[data-celk]'
            ];
            var seen = new Set();
            var count = 0;

            for (var s = 0; s < selectors.length; s++) {{
                try {{
                    var refs = document.querySelectorAll(selectors[s]);
                    for (var i = 0; i < refs.length && count < {max_nodes}; i++) {{
                        var el = refs[i];
                        if (!el) continue;
                        var rect = {{width: 0, height: 0}};
                        try {{ rect = el.getBoundingClientRect(); }} catch(e) {{}}
                        if (rect.width < 5 || rect.height < 5) continue;
                        if (el.hidden || el.disabled) continue;
                        var text = (el.innerText || el.alt || el.value || el.name || '').substring(0, 50).trim();
                        var key = el.tagName + '-' + text + '-' + Math.round(rect.left) + '-' + Math.round(rect.top);
                        if (seen.has(key)) continue;
                        seen.add(key);
                        elements.push({{
                            ref: 'e' + (count + 1),
                            tag: el.tagName.toLowerCase(),
                            id: el.id || '',
                            class: (el.className || '').split(' ')[0],
                            text: text,
                            type: el.type || '',
                            placeholder: el.placeholder || '',
                            href: el.href || '',
                            src: el.src || '',
                            role: el.getAttribute('role') || ''
                        }});
                        count++;
                    }}
                }} catch(e) {{}}
            }}
            return JSON.stringify(elements);
        }})()
        """

        result = await self._send_and_wait("Runtime.evaluate", {
            "expression": js_code,
            "returnByValue": True
        })

        json_str = result.get("result", {}).get("result", {}).get("value", "")
        if not json_str:
            return {"error": "Failed to get snapshot"}

        import json
        try:
            elements = json.loads(json_str)
        except:
            return {"error": "Failed to parse snapshot"}

        # Build ref_map
        ref_map = {}
        for el in elements:
            ref_map[el["ref"]] = el["ref"]  # Store ref string as key

        # Store for later use
        self.ref_map = ref_map

        return {"elements": elements, "ref_map": ref_map}

    async def take_screenshot(self, path: str):
        """Take a screenshot."""
        result = await self._send_and_wait("Page.captureScreenshot", {
            "format": "png"
        })
        data = result.get("result", {}).get("data", "")

        if data:
            import base64
            with open(path, "wb") as f:
                f.write(base64.b64decode(data))
            return {"success": True, "path": path}

        return {"error": "Failed to capture screenshot"}

    async def list_tabs(self):
        """List all open tabs."""
        # Use HTTP endpoint for fresh data
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://{self.host}:{self.port}/json")
                targets = resp.json()
        except Exception as e:
            # Fall back to CDP
            try:
                result = await self._send_and_wait("Target.getTargets")
                targets = result.get("result", {}).get("targetInfos", [])
            except:
                await self._reconnect()
                result = await self._send_and_wait("Target.getTargets")
                targets = result.get("result", {}).get("targetInfos", [])

        tabs = []
        for t in targets:
            if t.get("type") == "page":
                tabs.append({
                    "id": t.get("id"),
                    "title": t.get("title", ""),
                    "url": t.get("url", "")
                })
        return {"tabs": tabs, "current": self.target_id}

    async def _reconnect(self):
        """Reconnect to Chrome and attach to first available tab."""
        # Close old connection if exists
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass

        # Reconnect
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://{self.host}:{self.port}/json/version")
            data = resp.json()
            ws_url = data["webSocketDebuggerUrl"]

        self.ws = await ws_client.connect(ws_url)

        # Get targets and attach to the first one
        await self._send("Target.getTargets", {})
        response = await self._recv()
        targets = response.get("result", {}).get("targetInfos", [])

        # Find the first page tab
        for target in targets:
            if target.get("type") == "page":
                self.target_id = target.get("targetId")
                break

        if not self.target_id:
            result = await self._send_and_wait("Target.createTarget", {"url": "about:blank"})
            self.target_id = result.get("result", {}).get("targetId")

        # Attach to the target
        result = await self._send_and_wait("Target.attachToTarget", {
            "targetId": self.target_id,
            "flatten": True
        })
        self.session_id = result.get("result", {}).get("sessionId")

        # Enable required domains
        await self._send_and_wait("DOM.enable")
        await self._send_and_wait("Runtime.enable")

    async def create_tab(self, url: str = "about:blank"):
        """Create a new tab using CDP Target.createTarget."""
        # First get current targets to see what we start with
        before = await self._send_and_wait("Target.getTargets")
        before_tabs = [t for t in before.get("result", {}).get("targetInfos", []) if t.get("type") == "page"]

        # Create new tab
        result = await self._send_and_wait("Target.createTarget", {"url": url})
        new_target_id = result.get("result", {}).get("targetId")

        if not new_target_id:
            # Check if there's an error in the result
            error = result.get("error") or result.get("result", {}).get("errorText", "Unknown error")
            return {"error": f"Failed to create tab: {error}", "result": result}

        # Check if created
        after = await self._send_and_wait("Target.getTargets")
        after_tabs = [t for t in after.get("result", {}).get("targetInfos", []) if t.get("type") == "page"]

        if len(after_tabs) > len(before_tabs):
            # Find the new tab
            before_ids = set(t.get("targetId") for t in before_tabs)
            new_tabs = [t for t in after_tabs if t.get("targetId") not in before_ids]
            if new_tabs:
                new_target_id = new_tabs[0].get("targetId")

        if not new_target_id:
            return {"error": "Failed to create tab", "tabs": len(after_tabs)}

        # Attach to new tab
        try:
            attach_result = await self._send_and_wait("Target.attachToTarget", {
                "targetId": new_target_id,
                "flatten": True
            })
            self.session_id = attach_result.get("result", {}).get("sessionId")
            self.target_id = new_target_id
            await self._send_and_wait("DOM.enable")
            await self._send_and_wait("Runtime.enable")
            return {"success": True, "tab_id": new_target_id}
        except Exception as e:
            return {"error": f"Failed to attach: {e}"}

    async def switch_tab(self, tab_id: str):
        """Switch to a different tab."""
        # Skip if already on this tab
        if tab_id == self.target_id:
            return {"success": True, "tab_id": tab_id, "skipped": True}

        # Attach to target
        attach_result = await self._send_and_wait("Target.attachToTarget", {
            "targetId": tab_id,
            "flatten": True
        })
        self.session_id = attach_result.get("result", {}).get("sessionId")
        self.target_id = tab_id
        # Enable domains
        await self._send_and_wait("DOM.enable")
        await self._send_and_wait("Runtime.enable")
        return {"success": True, "tab_id": tab_id}

    async def close_tab(self, tab_id: str):
        """Close a tab."""
        result = await self._send_and_wait("Target.closeTarget", {"targetId": tab_id})

        # Close succeeded - need to reconnect to get fresh state
        if result.get("result", {}).get("success"):
            # Reconnect to Chrome to get fresh target list
            await self._reconnect()
            return {"success": True}

        return {"error": "Failed to close tab"}

    async def wait_for_selector(self, selector: str, timeout: int = 30000):
        """Wait for a selector to appear."""
        result = await self._send_and_wait("DOM.waitForSelector", {
            "selector": selector,
            "timeout": timeout
        })
        return {"success": True, "selector": selector}

    async def hover_by_ref(self, ref: str):
        """Hover over an element by ref."""
        # Find the element
        elements = (await self.get_snapshot(max_nodes=100)).get("elements", [])
        element = None
        for el in elements:
            if el.get("ref") == ref:
                element = el
                break

        if not element:
            return {"error": f"Element {ref} not found"}

        node_id = element.get("nodeId")
        if not node_id:
            return {"error": "No nodeId for element"}

        # Get element position
        box_result = await self._send_and_wait("DOM.getBoxModel", {"nodeId": node_id})
        model = box_result.get("result", {}).get("model")
        if not model:
            return {"error": "Could not get box model"}

        # Calculate center
        content = model.get("content", [])
        if len(content) >= 8:
            x = (content[0] + content[2] + content[4] + content[6]) / 4
            y = (content[1] + content[3] + content[5] + content[7]) / 4

            # Hover
            await self._send_and_wait("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": x,
                "y": y
            })
            return {"success": True, "hovered": ref}

        return {"error": "Could not calculate position"}

    async def scroll(self, x: int = 0, y: int = 0):
        """Scroll the page."""
        result = await self._send_and_wait("Runtime.evaluate", {
            "expression": f"window.scrollBy({x}, {y})",
            "returnByValue": True
        })
        return {"success": True, "scrolled_to": f"x={x}, y={y}"}

    async def scroll_to_selector(self, selector: str):
        """Scroll to a specific selector."""
        result = await self._send_and_wait("Runtime.evaluate", {
            "expression": f"""
                (function() {{
                    const el = document.querySelector('{selector}');
                    if (el) {{
                        el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
                        return 'scrolled';
                    }}
                    return 'not found';
                }})()
            """,
            "returnByValue": True
        })
        return {"success": True, "result": result.get("result", {}).get("result", {}).get("value", "")}

    async def resize_viewport(self, width: int, height: int):
        """Resize the viewport."""
        result = await self._send_and_wait("Emulation.setDeviceMetricsOverride", {
            "width": width,
            "height": height,
            "deviceScaleFactor": 1,
            "mobile": False
        })
        return {"success": True, "size": f"{width}x{height}"}

    async def evaluate(self, expression: str):
        """Execute JavaScript and return result."""
        result = await self._send_and_wait("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })
        eval_result = result.get("result", {})
        if eval_result.get("type") == "object":
            # Return serialized
            return {"success": True, "result": str(eval_result.get("value", ""))}
        return {"success": True, "result": eval_result.get("value", "")}

    async def get_cookies(self):
        """Get all cookies."""
        result = await self._send_and_wait("Network.getAllCookies", {})
        cookies = result.get("result", {}).get("cookies", [])
        return {"success": True, "cookies": cookies}

    async def set_cookie(self, name: str, value: str, domain: str = "", url: str = ""):
        """Set a cookie."""
        result = await self._send_and_wait("Network.setCookie", {
            "name": name,
            "value": value,
            "domain": domain,
            "url": url
        })
        return {"success": result.get("result", {}).get("success", False), "name": name}

    async def delete_cookies(self, name: str, domain: str = ""):
        """Delete a cookie."""
        if domain:
            await self._send_and_wait("Network.deleteCookies", {"name": name, "domain": domain})
        else:
            await self._send_and_wait("Network.deleteCookies", {"name": name})
        return {"success": True, "deleted": name}

    async def get_local_storage(self):
        """Get localStorage items."""
        result = await self._send_and_wait("Runtime.evaluate", {
            "expression": "JSON.stringify(localStorage)",
            "returnByValue": True
        })
        value = result.get("result", {}).get("result", {}).get("value", "{}")
        try:
            items = json.loads(value)
        except:
            items = {}
        return {"success": True, "storage": items}

    async def get_session_storage(self):
        """Get sessionStorage items."""
        result = await self._send_and_wait("Runtime.evaluate", {
            "expression": "JSON.stringify(sessionStorage)",
            "returnByValue": True
        })
        value = result.get("result", {}).get("result", {}).get("value", "{}")
        try:
            items = json.loads(value)
        except:
            items = {}
        return {"success": True, "storage": items}

    async def wait_for_url(self, url_pattern: str, timeout: int = 30000):
        """Wait for URL to match pattern."""
        import time
        start = time.time()
        while (time.time() - start) * 1000 < timeout:
            result = await self._send_and_wait("Page.getNavigationHistory", {})
            current_url = result.get("result", {}).get("entries", [{}])[-1].get("url", "")
            if url_pattern in current_url or (url_pattern.startswith("**") and current_url.startswith(url_pattern[2:])):
                return {"success": True, "url": current_url}
            await asyncio.sleep(0.5)
        return {"error": "Timeout waiting for URL"}

    async def wait_for_load(self, timeout: int = 30000):
        """Wait for page to load."""
        import time
        start = time.time()
        while (time.time() - start) * 1000 < timeout:
            result = await self._send_and_wait("Page.getLoadEventFired", {})
            if result.get("result"):
                return {"success": True}
            await asyncio.sleep(0.5)
        return {"error": "Timeout waiting for load"}

    async def wait_for_selector(self, selector: str, timeout: int = 30000):
        """Wait for selector to appear."""
        import time
        start = time.time()
        while (time.time() - start) * 1000 < timeout:
            node_id = await self.query_selector(selector)
            if node_id:
                return {"success": True, "selector": selector}
            await asyncio.sleep(0.5)
        return {"error": f"Timeout waiting for selector: {selector}"}

    async def wait(self, url: str = "", selector: str = "", load: bool = False, timeout: int = 30000):
        """Wait for conditions: url, selector, or load."""
        if url:
            return await self.wait_for_url(url, timeout)
        if selector:
            return await self.wait_for_selector(selector, timeout)
        if load:
            return await self.wait_for_load(timeout)
        return {"error": "No wait condition specified"}

    async def get_console_messages(self):
        """Get console messages."""
        # Enable console domain first
        await self._send("Log.enable", {})
        result = await self._send_and_wait("Log.getEntries", {})
        entries = result.get("result", {}).get("entries", [])
        messages = []
        for entry in entries:
            msg_type = entry.get("type", "log")
            text = entry.get("text", "")
            messages.append(f"[{msg_type}] {text}")
        return {"success": True, "messages": messages[:50]}

    async def get_errors(self):
        """Get page errors."""
        # Enable console domain
        await self._send("Log.enable", {})
        result = await self._send_and_wait("Log.getEntries", {})
        entries = result.get("result", {}).get("entries", [])
        errors = []
        for entry in entries:
            if entry.get("level") == "error":
                errors.append(entry.get("text", "Unknown error"))
        return {"success": True, "errors": errors}

    async def download_file(self, url: str, path: str):
        """Download a file from URL."""
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(resp.content)
                return {"success": True, "path": path}
        return {"error": f"Download failed: {resp.status_code}"}

    async def upload_file(self, selector: str, file_path: str):
        """Upload a file to an input element."""
        import os
        if not os.path.exists(file_path):
            return {"error": f"File not found: {file_path}"}

        # Use file chooser CDP method
        node_id = await self.query_selector(selector)
        if not node_id:
            return {"error": f"Element not found: {selector}"}

        # Get the node's object ID for file upload
        result = await self._send_and_wait("DOM.resolveNode", {"nodeId": node_id})
        object_id = result.get("result", {}).get("object", {}).get("objectId")

        if not object_id:
            return {"error": "Could not get object ID"}

        # Set file for upload
        upload_result = await self._send_and_wait("DOM.setFileInputFiles", {
            "objectId": object_id,
            "files": [{"name": os.path.basename(file_path), "path": file_path}]
        })

        if upload_result.get("result", {}).get("success"):
            return {"success": True, "file": file_path}
        return {"error": "Upload failed"}

    async def start_trace(self, path: str):
        """Start tracing."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        await self._send_and_wait("Tracing.start", {})
        return {"success": True, "path": path}

    async def stop_trace(self, path: str):
        """Stop tracing and save to file."""
        # Stop tracing and collect
        await self._send("Tracing.end", {})

        # Wait for data
        await asyncio.sleep(2)

        # Get trace data
        result = await self._send_and_wait("Tracing.getTrace", {})
        trace_data = result.get("result", {}).get("value", "")

        if trace_data:
            import base64
            with open(path, "w") as f:
                f.write(base64.b64decode(trace_data).decode("utf-8"))
            return {"success": True, "path": path}

        return {"error": "No trace data"}

    async def close(self):
        """Close the connection."""
        if self.ws:
            await self.ws.close()
