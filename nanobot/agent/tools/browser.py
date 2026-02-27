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

**IMPORTANT - Always follow this workflow:**
1. browser({"action": "open", "url": "https://..."}) - Open URL first
2. browser({"action": "state"}) - Get clickable elements with indices (ALWAYS do this before clicking/typing)
3. Use the index numbers from state to interact with elements

**When to use what:**

| Scenario | Action | Example |
|----------|--------|---------|
| Click button/link | click + index | {"action": "click", "index": 5} |
| Type in input field | input + index + text | {"action": "input", "index": 3, "text": "hello"} |
| Select dropdown option | select + index + option | {"action": "select", "index": 2, "option": "Beijing"} |
| Checkbox/Radio button | eval + JS | {"action": "eval", "code": "document.querySelectorAll('input[type=checkbox]')[0].click()"} |
| Hover over element | hover + index | {"action": "hover", "index": 1} |
| Press keyboard | keys + keys | {"action": "keys", "keys": "Enter"} |
| Wait for element | wait + target + type | {"action": "wait", "target": ".loading", "type": "selector"} |
| Scroll down/up | scroll + direction | {"action": "scroll", "direction": "down"} |
| Go back | back | {"action": "back"} |
| Take screenshot | screenshot | {"action": "screenshot"} |
| Get page title | get + what: title | {"action": "get", "what": "title"} |
| Get element text | get + what: text + index | {"action": "get", "what": "text", "index": 0} |
| Get input value | get + what: value + index | {"action": "get", "what": "value", "index": 3} |
| Get element attributes | get + what: attributes + index | {"action": "get", "what": "attributes", "index": 2} |
| Execute JavaScript | eval + code | {"action": "eval", "code": "document.cookie"} |
| Close browser | close | {"action": "close"} |

**Checkbox/Complex interactions:**
- Use eval to interact with checkboxes when click doesn't work
- Example: {"action": "eval", "code": "document.querySelector('#agree').checked = true"}

**All available actions:**
open, state, click, input, select, hover, keys, wait, screenshot, close, scroll, back, eval, get

**Workflow example:**
1. browser({"action": "open", "url": "https://example.com"})
2. browser({"action": "state"}) - See elements [0] button, [1] input, [2] dropdown
3. browser({"action": "click", "index": 1}) - Click the input
4. browser({"action": "input", "index": 1, "text": "search term"})"""

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

            else:
                return f"Unknown action: {action}. Use: open, state, click, input, screenshot, close, scroll, back, select, wait, keys, hover, eval, get"

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
                    "enum": ["open", "state", "click", "input", "select", "hover", "keys", "wait", "screenshot", "close", "scroll", "back", "eval", "get"]
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
