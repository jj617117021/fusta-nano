"""Browser management for launching and controlling Chrome instances."""

import asyncio
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any


# Default CDP port for nanobot
DEFAULT_CDP_PORT = 18800

# Profile configurations
# Each profile has: port, browser, color
DEFAULT_PROFILES = {
    "nanobot": {"port": 18800, "browser": "chrome", "color": "#FF4500"},
    "chrome": {"port": 18801, "browser": "chrome", "color": "#4285F4"},
    "brave": {"port": 18802, "browser": "brave", "color": "#FF6B00"},
    "edge": {"port": 18803, "browser": "edge", "color": "#0078D4"},
}

# Known browser configurations
BROWSERS = {
    "chrome": {
        "macos": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "linux": "/usr/bin/google-chrome",
        "windows": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    },
    "brave": {
        "macos": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "linux": "/usr/bin/brave-browser",
        "windows": "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe",
    },
    "edge": {
        "macos": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "linux": "/usr/bin/microsoft-edge",
        "windows": "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    },
    "chromium": {
        "macos": "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "linux": "/usr/bin/chromium",
        "windows": "C:\\Program Files\\Chromium\\Application\\chrome.exe",
    },
}


def get_platform() -> str:
    """Get the current platform."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    elif system == "linux":
        return "linux"
    elif system == "windows":
        return "windows"
    return "macos"  # default to macos


def find_browser_path(browser: str = "chrome") -> str | None:
    """Find the browser executable path."""
    plat = get_platform()

    # Check configured path first
    config_path = os.environ.get(f"NANOBOT_{browser.upper()}_PATH")
    if config_path and os.path.exists(config_path):
        return config_path

    # Check known paths
    browser_config = BROWSERS.get(browser.lower())
    if browser_config:
        path = browser_config.get(plat)
        if path and os.path.exists(path):
            return path

    # Try system PATH
    try:
        result = subprocess.run(
            ["which", browser.lower().replace(" ", "-")],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    return None


def get_chrome_processes(cdp_port: int = DEFAULT_CDP_PORT) -> list[dict[str, Any]]:
    """Get list of Chrome processes running with remote debugging."""
    processes = []
    try:
        # Use ps to find Chrome processes
        system = platform.system().lower()
        if system == "darwin" or system == "linux":
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                timeout=5
            )
            for line in result.stdout.split("\n"):
                if "Chrome" in line or "chrome" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            pid = int(parts[1])
                            # Check if this process has our port
                            if f"--remote-debugging-port={cdp_port}" in line or f"--remote-debugging-port " in line:
                                processes.append({
                                    "pid": pid,
                                    "port": cdp_port,
                                    "info": line[:100]
                                })
                        except ValueError:
                            pass
    except Exception:
        pass
    return processes


class BrowserManager:
    """Manages Chrome browser instances."""

    def __init__(self, workspace: Path | None = None):
        self.workspace = workspace or Path.home() / ".nanobot" / "workspace"
        self.browser_dir = self.workspace / "browser"
        self.browser_dir.mkdir(parents=True, exist_ok=True)
        self.profiles = DEFAULT_PROFILES.copy()

    def get_profile_config(self, profile: str) -> dict[str, Any]:
        """Get profile configuration."""
        return self.profiles.get(profile, {
            "port": DEFAULT_CDP_PORT,
            "browser": "chrome",
            "color": "#FF4500"
        })

    def list_profiles(self) -> dict[str, Any]:
        """List all available profiles."""
        return {
            "profiles": self.profiles
        }

    def get_user_data_dir(self, profile: str = "nanobot") -> Path:
        """Get the user data directory for a profile."""
        return self.browser_dir / f"profile_{profile}"

    async def start(
        self,
        browser: str = "chrome",
        port: int = DEFAULT_CDP_PORT,
        profile: str = "nanobot",
        headless: bool = False,
    ) -> dict[str, Any]:
        """Start a browser instance."""
        # Find browser executable
        browser_path = find_browser_path(browser)
        if not browser_path:
            return {
                "success": False,
                "error": f"Browser '{browser}' not found. Install it or set NANOBOT_{browser.upper()}_PATH"
            }

        # Check if already running on this port
        try:
            import httpx
            response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
            if response.status_code == 200:
                return {
                    "success": True,
                    "message": f"Browser already running on port {port}",
                    "port": port,
                    "profile": profile,
                }
        except Exception:
            pass

        # Prepare user data directory
        user_data_dir = self.get_user_data_dir(profile)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        # Build command
        cmd = [
            browser_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        if headless:
            cmd.extend(["--headless", "--disable-gpu"])

        try:
            # Start browser
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            # Wait for browser to be ready
            max_wait = 10
            for i in range(max_wait):
                await asyncio.sleep(1)
                try:
                    import httpx
                    response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
                    if response.status_code == 200:
                        return {
                            "success": True,
                            "message": f"Browser started on port {port}",
                            "port": port,
                            "profile": profile,
                            "pid": process.pid,
                            "browser": browser,
                        }
                except Exception:
                    pass

            # Timeout - kill the process
            process.terminate()
            return {
                "success": False,
                "error": "Browser failed to start within timeout"
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to start browser: {str(e)}"
            }

    async def stop(self, port: int = DEFAULT_CDP_PORT) -> dict[str, Any]:
        """Stop browser instance on the given port."""
        try:
            # Try to gracefully close via CDP first
            import httpx
            try:
                # Get the WebSocket URL
                response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
                if response.status_code == 200:
                    data = response.json()
                    ws_url = data.get("webSocketDebuggerUrl", "")
                    if ws_url:
                        # Try to close via CDP
                        import websockets
                        try:
                            async with websockets.connect(ws_url) as ws:
                                await ws.send('{"id":1,"method":"Browser.close"}')
                        except Exception:
                            pass
            except Exception:
                pass

            # Kill process by port
            system = platform.system().lower()
            if system == "darwin" or system == "linux":
                # Find and kill process using the port
                subprocess.run(
                    ["sh", "-c", f"lsof -ti:{port} | xargs kill -9 2>/dev/null"],
                    capture_output=True,
                    timeout=5
                )
            elif system == "windows":
                subprocess.run(
                    ["powershell", "-Command", f"Get-Process -Id (Get-NetTCPConnection -LocalPort {port}).OwningProcess -ErrorAction SilentlyContinue | Stop-Process -Force"],
                    capture_output=True,
                    timeout=5
                )

            return {
                "success": True,
                "message": f"Browser stopped on port {port}",
                "port": port
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to stop browser: {str(e)}"
            }

    async def status(self, port: int = DEFAULT_CDP_PORT) -> dict[str, Any]:
        """Check browser status."""
        try:
            import httpx
            response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
            if response.status_code == 200:
                data = response.json()
                return {
                    "success": True,
                    "running": True,
                    "port": port,
                    "browser": data.get("Browser", "Unknown"),
                    "webSocket": data.get("webSocketDebuggerUrl", ""),
                }
            else:
                return {
                    "success": True,
                    "running": False,
                    "port": port,
                }
        except Exception:
            return {
                "success": True,
                "running": False,
                "port": port,
            }
