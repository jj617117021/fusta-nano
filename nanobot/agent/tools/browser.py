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
    description = """Browser automation using browser-use CLI.

**Actions:**
- open: {"url": "https://..."} - Open a URL
- state - Get clickable elements with indices
- click: {"index": 1} - Click element by index
- input: {"index": 1, "text": "hello"} - Click and type
- screenshot - Take screenshot
- close - Close browser

**Workflow:**
1. browser({"action": "open", "url": "https://xiaohongshu.com"})
2. browser({"action": "state"}) - Get elements with indices
3. browser({"action": "click", "index": 5}) - Click element

**Example:**
- browser({"action": "open", "url": "https://linkedin.com/jobs"})"""

    def __init__(self, workspace: Path):
        self.workspace = workspace

    async def execute(self, action: str, **kwargs) -> str:
        """Execute a browser action using browser-use CLI."""
        try:
            cmd = [BROWSER_USE_CMD, "--browser", "real", "--headed"]

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

            else:
                return f"Unknown action: {action}. Use: open, state, click, input, screenshot, close, scroll, back"

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
                    "enum": ["open", "state", "click", "input", "screenshot", "close", "scroll", "back"]
                },
                "url": {"type": "string", "description": "URL to open"},
                "index": {"type": "integer", "description": "Element index from state"},
                "text": {"type": "string", "description": "Text to input"},
                "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
            },
            "required": ["action"]
        }
