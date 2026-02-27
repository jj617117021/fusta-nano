"""Browser automation tool using browser-use CLI."""

import asyncio
import os
from pathlib import Path
from typing import Any

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

**Actions:** open, state, click, input, select, hover, keys, wait, screenshot, close, scroll, back, eval, get, check, uncheck

See browser-use skill (always loaded) for detailed usage guide."""

    def __init__(self, workspace: Path):
        self.workspace = workspace

    async def execute(self, action: str, **kwargs) -> str:
        """Execute a browser action using browser-use CLI."""
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
                cmd.extend(["scroll", direction])

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
                if not target:
                    return "Error: target is required for wait"
                if wait_type == "text":
                    cmd.extend(["wait", "text", target])
                else:
                    cmd.extend(["wait", "selector", target])

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
                if what in ["text", "value", "attributes", "bbox"]:
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

            else:
                return f"Unknown action: {action}. Use: open, state, click, input, screenshot, close, scroll, back, select, wait, keys, hover, eval, get, check, uncheck"

            # Run CLI command
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
            return f"[ERROR] {str(e)}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["open", "state", "click", "input", "select", "hover", "keys", "wait", "screenshot", "close", "scroll", "back", "eval", "get", "check", "uncheck"]
                },
                "url": {"type": "string", "description": "URL to open"},
                "index": {"type": "integer", "description": "Element index from state"},
                "text": {"type": "string", "description": "Text to input"},
                "option": {"type": "string", "description": "Option text to select from dropdown"},
                "keys": {"type": "string", "description": "Keyboard keys (e.g., Enter, Control+a)"},
                "target": {"type": "string", "description": "Target selector or text to wait for"},
                "type": {"type": "string", "enum": ["selector", "text"], "description": "Wait type"},
                "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
                "code": {"type": "string", "description": "JavaScript code to execute"},
                "what": {"type": "string", "enum": ["title", "html", "text", "value", "attributes", "bbox"], "description": "What to get: title, html, text, value, attributes, bbox"},
            },
            "required": ["action"]
        }
