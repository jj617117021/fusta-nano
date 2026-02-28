"""Browser automation tool using browser-use CLI."""

import asyncio
import os
from pathlib import Path
from typing import Any

from loguru import logger
from nanobot.agent.tools.base import Tool


# Use system browser-use (homebrew version)
BROWSER_USE_CMD = "/opt/homebrew/bin/browser-use"

# Chrome user data directory (for real Chrome with your profile)
CHROME_USER_DATA_DIR = os.path.expanduser("~/Library/Application Support/Google/Chrome")


class BrowserTool(Tool):
    """Browser automation using browser-use CLI (same as OpenClaw)."""

    name = "browser"
    description = """Browser automation using browser-use CLI with real Chrome profile.

**Workflow:** open URL → state (get elements) → click/input using index

**Actions:** open, state, click, input, select, hover, keys, wait, screenshot, close, scroll, back, eval, get, check, uncheck, dblclick, rightclick, switch, close_tab, cookies, python

**Advanced:** smart_click, smart_input, find (use Playwright for better reliability)

See browser-use skill (always loaded) for detailed usage guide."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._playwright_client = None

    async def _get_playwright_client(self):
        """获取或初始化 Playwright 客户端."""
        if self._playwright_client is None:
            from nanobot.agent.tools.playwright_client import PlaywrightClient
            self._playwright_client = PlaywrightClient(host="127.0.0.1", port=18800)
            try:
                await self._playwright_client.connect()
            except Exception as e:
                self._playwright_client = None
                raise ConnectionError(f"Failed to connect to Playwright: {e}")
        return self._playwright_client

    async def execute(self, action: str, **kwargs) -> str:
        """Execute a browser action. Uses Playwright for advanced actions, CLI for basic ones."""
        logger.info(f"[browser] action={action} kwargs={kwargs}")

        # 使用 Playwright 的高级 actions
        if action in ["smart_click", "smart_input", "find"]:
            result = await self._playwright_execute(action, **kwargs)
            logger.info(f"[browser] action={action} result={result[:200] if len(result) > 200 else result}")
            return result

        # 默认使用 CLI
        result = await self._cli_execute(action, **kwargs)
        logger.info(f"[browser] action={action} result={result[:200] if len(result) > 200 else result}")
        return result

    async def _playwright_execute(self, action: str, **kwargs) -> str:
        """使用 Playwright 执行高级操作."""
        try:
            client = await self._get_playwright_client()

            if action == "smart_click":
                ref = kwargs.get("ref", "e1")
                retry = kwargs.get("retry", True)
                if retry:
                    result = await client.click_with_retry(ref)
                else:
                    result = await client.click_by_ref(ref)
                return str(result)

            elif action == "smart_input":
                ref = kwargs.get("ref", "e1")
                text = kwargs.get("text", "")
                result = await client.type_by_ref(ref, text)
                return str(result)

            elif action == "find":
                strategy = kwargs.get("strategy", "")
                value = kwargs.get("value", "")
                find_action = kwargs.get("action", "")
                result = await client.find_element(strategy, value, find_action, **kwargs)
                return str(result)

            return f"Unknown advanced action: {action}"

        except Exception as e:
            return f"[ERROR] {e}"

    async def _cli_execute(self, action: str, **kwargs) -> str:
        """使用 browser-use CLI 执行操作."""
        try:
            cmd = [BROWSER_USE_CMD, "--session", "nanobot", "--browser", "real", "--profile", "Default", "--headed"]

            if action == "open":
                url = kwargs.get("url", "")
                if not url:
                    return "Error: url is required"
                cmd.extend(["open", url])

            elif action == "state":
                cmd.append("state")

            elif action == "click":
                index = kwargs.get("index", 0)
                cmd.extend(["click", str(index)])

            elif action == "input":
                index = kwargs.get("index", 0)
                text = kwargs.get("text", "")
                cmd.extend(["input", str(index), text])

            elif action == "screenshot":
                path = str(self.workspace / "screenshot.png")
                cmd.extend(["screenshot", path])

            elif action == "close":
                cmd.append("close")

            elif action == "scroll":
                direction = kwargs.get("direction", "down")
                amount = kwargs.get("amount", "")
                cmd.extend(["scroll", direction])
                if amount:
                    cmd.extend(["--amount", str(amount)])

            elif action == "back":
                cmd.append("back")

            elif action == "select":
                index = kwargs.get("index", 0)
                option = kwargs.get("option", "")
                if not option:
                    return "Error: option is required for select"
                cmd.extend(["select", str(index), option])

            elif action == "wait":
                wait_type = kwargs.get("type", "selector")
                target = kwargs.get("target", "")
                state = kwargs.get("state", "")
                timeout = kwargs.get("timeout", "")
                if not target:
                    return "Error: target is required for wait"
                cmd.extend(["wait", wait_type, target])
                if state:
                    cmd.extend(["--state", state])
                if timeout:
                    cmd.extend(["--timeout", str(timeout)])

            elif action == "keys":
                keys = kwargs.get("keys", "")
                if not keys:
                    return "Error: keys is required"
                cmd.extend(["keys", keys])

            elif action == "hover":
                index = kwargs.get("index", 0)
                cmd.extend(["hover", str(index)])

            elif action == "eval":
                code = kwargs.get("code", "")
                if not code:
                    return "Error: code is required for eval"
                cmd.extend(["eval", code])

            elif action == "get":
                what = kwargs.get("what", "title")
                index = kwargs.get("index")
                selector = kwargs.get("selector", "")
                if what == "html" and selector:
                    cmd.extend([what, "--selector", selector])
                elif what in ["text", "value", "attributes", "bbox"]:
                    if index is None:
                        return "Error: index is required for get text/value/attributes/bbox"
                    cmd.extend([what, str(index)])
                else:
                    cmd.extend([what])

            elif action == "check":
                index = kwargs.get("index", 0)
                # Use eval with .click() to properly trigger checkbox events
                code = f"document.querySelectorAll('input[type=checkbox]')[{index}]?.click()"
                cmd.extend(["eval", code])

            elif action == "uncheck":
                index = kwargs.get("index", 0)
                # Use eval with .click() to properly trigger checkbox events
                code = f"document.querySelectorAll('input[type=checkbox]')[{index}]?.click()"
                cmd.extend(["eval", code])

            # === 新增 Actions (按照 OpenClaw browser-use) ===

            elif action == "dblclick":
                index = kwargs.get("index", 0)
                cmd.extend(["dblclick", str(index)])

            elif action == "rightclick":
                index = kwargs.get("index", 0)
                cmd.extend(["rightclick", str(index)])

            elif action == "switch":
                tab = kwargs.get("tab", 0)
                cmd.extend(["switch", str(tab)])

            elif action == "close_tab":
                cmd.extend(["close-tab"])

            elif action == "screenshot":
                path = kwargs.get("path", str(self.workspace / "screenshot.png"))
                full = kwargs.get("full", False)
                cmd.extend(["screenshot", path])
                if full:
                    cmd.extend(["--full"])

            elif action == "cookies":
                subaction = kwargs.get("subaction", "get")
                name = kwargs.get("name", "")
                value = kwargs.get("value", "")
                domain = kwargs.get("domain", "")
                cookie_path = kwargs.get("path", "")

                cmd.extend(["cookies", subaction])
                if subaction == "set" and name:
                    cmd.extend([name, value])
                    if domain:
                        cmd.extend(["--domain", domain])
                elif subaction == "export" and cookie_path:
                    cmd.extend([cookie_path])
                elif subaction == "import" and cookie_path:
                    cmd.extend([cookie_path])
                elif subaction == "clear":
                    pass  # cookies clear takes no args
                elif subaction == "get":
                    url = kwargs.get("url", "")
                    if url:
                        cmd.extend(["--url", url])

            elif action == "python":
                code = kwargs.get("code", "")
                if not code:
                    return "Error: code is required for python"
                cmd.extend(["python", code])

            else:
                logger.warning(f"[browser] unknown action: {action}")
                return f"Unknown action: {action}. Use: open, state, click, input, screenshot, close, scroll, back, select, wait, keys, hover, eval, get, check, uncheck, dblclick, rightclick, switch, close_tab, cookies, python"

            # Run CLI command
            logger.debug(f"[browser] executing: {' '.join(cmd)}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()

            result = stdout.decode() + stderr.decode()
            if not result:
                return "Done"
            return result

        except Exception as e:
            logger.error(f"[browser] error: {e}")
            return f"[ERROR] {str(e)}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "open", "state", "click", "input", "select", "hover",
                        "keys", "wait", "screenshot", "close", "scroll", "back",
                        "eval", "get", "check", "uncheck",
                        "dblclick", "rightclick", "switch", "close_tab", "cookies", "python"
                    ]
                },
                "url": {"type": "string", "description": "URL to open"},
                "index": {"type": "integer", "description": "Element index from state"},
                "text": {"type": "string", "description": "Text to input"},
                "option": {"type": "string", "description": "Option text to select from dropdown"},
                "keys": {"type": "string", "description": "Keyboard keys (e.g., Enter, Control+a)"},
                "target": {"type": "string", "description": "Target selector or text to wait for"},
                "type": {"type": "string", "enum": ["selector", "text"], "description": "Wait type"},
                "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
                "amount": {"type": "integer", "description": "Scroll amount in pixels (default: 500)"},
                "code": {"type": "string", "description": "JavaScript or Python code to execute"},
                "what": {"type": "string", "enum": ["title", "html", "text", "value", "attributes", "bbox"], "description": "What to get: title, html, text, value, attributes, bbox"},
                "selector": {"type": "string", "description": "CSS selector for get html"},
                "state": {"type": "string", "enum": ["hidden", "attached"], "description": "Wait for element hidden or attached"},
                "timeout": {"type": "integer", "description": "Wait timeout in ms"},
                "tab": {"type": "integer", "description": "Tab index to switch to"},
                "path": {"type": "string", "description": "File path for screenshot/cookies import/export"},
                "full": {"type": "boolean", "description": "Full page screenshot"},
                "subaction": {"type": "string", "enum": ["get", "set", "clear", "import", "export"], "description": "Cookies subaction"},
                "name": {"type": "string", "description": "Cookie name"},
                "value": {"type": "string", "description": "Cookie value"},
                "domain": {"type": "string", "description": "Cookie domain"},
            },
            "required": ["action"]
        }
