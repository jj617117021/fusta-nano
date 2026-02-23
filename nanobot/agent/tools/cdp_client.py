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
        """Click an element by ref (like OpenClaw).

        Uses accessibility tree to find and click elements.
        All refs are like e1, e2, e3... (no p refs).
        """
        # Extract index from ref (e1 -> 0, e2 -> 1)
        try:
            prefix = ref[0].lower()
            if prefix != 'e':
                return {"error": f"Invalid ref format: {ref}. Use e1, e2..."}
            idx = int(ref[1:]) - 1
        except:
            return {"error": f"Invalid ref format: {ref}. Use e1, e2..."}

        # Find element by index and click using JavaScript
        js_code = f"""
        (function() {{
            // Get all clickable/interactive elements
            var selectors = [
                'a', 'button', '[role="button"]', '[role="link"]',
                'input[type="button"]', 'input[type="submit"]', 'input[type="checkbox"]', 'input[type="radio"]',
                '[onclick]', '[data-clickable="true"]'
            ];

            var allElements = [];
            for (var s = 0; s < selectors.length; s++) {{
                try {{
                    var els = document.querySelectorAll(selectors[s]);
                    for (var i = 0; i < els.length; i++) {{
                        var el = els[i];
                        var role = el.getAttribute('role') || el.tagName.toLowerCase();
                        var text = (el.innerText || el.textContent || el.value || el.alt || '').trim();
                        var rect = {{width: 0, height: 0}};
                        try {{ rect = el.getBoundingClientRect(); }} catch(e) {{}}
                        // Skip invisible elements
                        if (rect.width < 5 || rect.height < 5) continue;
                        if (el.hidden || el.disabled) continue;

                        allElements.push({{
                            role: role,
                            text: text.substring(0, 60),
                            el: el
                        }});
                    }}
                }} catch(e) {{}}
            }}

            if ({idx} >= allElements.length) {{
                return 'not_found|index ' + {idx} + ' out of ' + allElements.length;
            }}

            var target = allElements[{idx}];
            var el = target.el;

            // Use JavaScript click() - more reliable for React apps
            el.click();

            return JSON.stringify({{
                success: true,
                role: target.role,
                text: target.text
            }});
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
            if data.get("success"):
                await asyncio.sleep(2)
                return {"success": True, "ref": ref, "clicked": True}
        except:
            pass

        return {"error": "Click failed"}

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

    async def get_snapshot(self, max_nodes: int = 50, save_scroll: bool = True):
        """Get DOM snapshot with element refs.

        Returns elements with refs like e1, e2, e3... based on DOM queries.
        Simplified approach - uses DOM directly instead of accessibility API.

        Args:
            max_nodes: Maximum number of elements to return
            save_scroll: If True, save and restore scroll position after snapshot
        """
        # Save current scroll position
        original_scroll_y = None
        if save_scroll:
            result = await self._send_and_wait("Runtime.evaluate", {
                "expression": "window.scrollY",
                "returnByValue": True
            })
            original_scroll_y = result.get("result", {}).get("result", {}).get("value")

        # Use simple DOM-based approach - more reliable for Xiaohongshu
        js_code = f"""
        (function() {{
            var elements = [];
            // Get all clickable/interactive elements
            var selectors = [
                'a', 'button', '[role="button"]', '[role="link"]',
                'input[type="button"]', 'input[type="submit"]',
                '[onclick]', '[data-clickable="true"]'
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

                        var text = (el.innerText || el.textContent || el.value || el.alt || '').trim();
                        if (!text) continue;
                        text = text.substring(0, 50);

                        var key = el.tagName + '-' + text + '-' + Math.round(rect.left) + '-' + Math.round(rect.top);
                        if (seen.has(key)) continue;
                        seen.add(key);

                        elements.push({{
                            ref: 'e' + (count + 1),
                            tag: el.tagName.toLowerCase(),
                            text: text,
                            role: el.getAttribute('role') || '',
                            href: el.href || ''
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
            ref_map[el["ref"]] = el

        # Store for later use
        self.ref_map = ref_map

        # Restore original scroll position
        if save_scroll and original_scroll_y is not None:
            await self._send_and_wait("Runtime.evaluate", {
                "expression": f"window.scrollTo(0, {original_scroll_y});"
            })

        return {"elements": elements, "ref_map": ref_map}

    async def _get_dom_snapshot(self, max_nodes: int = 50):
        """Fallback: Get DOM snapshot using JavaScript (legacy behavior)."""
        js_code = f"""
        (function() {{
            var elements = [];
            var selectors = [
                'a', 'button', 'input', 'textarea', 'select',
                '[role="button"]', '[role="link"]', '[onclick]',
                '[data-clickable="true"]'
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
                            text: text,
                            href: el.href || ''
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

        # Restore original scroll position
        if save_scroll and original_scroll_y is not None:
            await self._send_and_wait("Runtime.evaluate", {
                "expression": f"window.scrollTo(0, {original_scroll_y});"
            })

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
