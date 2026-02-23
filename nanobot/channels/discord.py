"""Discord channel implementation using Discord Gateway websocket."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import websockets
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import DiscordConfig


DISCORD_API_BASE = "https://discord.com/api/v10"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20MB
MAX_MESSAGE_LEN = 2000  # Discord message character limit


def _split_message(content: str, max_len: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split content into chunks within max_len, preferring line breaks."""
    if not content:
        return []
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    while content:
        if len(content) <= max_len:
            chunks.append(content)
            break
        cut = content[:max_len]
        pos = cut.rfind('\n')
        if pos <= 0:
            pos = cut.rfind(' ')
        if pos <= 0:
            pos = max_len
        chunks.append(content[:pos])
        content = content[pos:].lstrip()
    return chunks


class DiscordChannel(BaseChannel):
    """Discord channel using Gateway websocket."""

    name = "discord"

    def __init__(self, config: DiscordConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: DiscordConfig = config
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._seq: int | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Start the Discord gateway connection."""
        if not self.config.token:
            logger.error("Discord bot token not configured")
            return

        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)

        while self._running:
            try:
                logger.info("Connecting to Discord gateway...")
                async with websockets.connect(self.config.gateway_url) as ws:
                    self._ws = ws
                    await self._gateway_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Discord gateway error: {}", e)
                if self._running:
                    logger.info("Reconnecting to Discord gateway in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the Discord channel."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._http:
            await self._http.aclose()
            self._http = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through Discord REST API."""
        if not self._http:
            logger.warning("Discord HTTP client not initialized")
            return

        url = f"{DISCORD_API_BASE}/channels/{msg.chat_id}/messages"
        headers = {"Authorization": f"Bot {self.config.token}"}

        # Extract image paths from content
        # Format 1: [IMAGE_FILE:/path/to/image.png] - goes to image_paths
        # Format 2: [IMAGE_MEDIA:/path/to/image.png] - goes to media (not processed by LLM)
        # Format 3: Saved to: /path/to/image.png
        # Format 4: **保存位置:** /path/to/image.png (Chinese format)
        print(f"[DISCORD DEBUG] Received content: {msg.content[:300] if msg.content else 'None'}")
        logger.info("Discord send: content={}", msg.content[:200] if msg.content else "None")
        image_paths = []
        content_parts = []
        for line in (msg.content or "").split("\n"):
            if line.startswith("[IMAGE_MEDIA:"):
                # This format goes directly to media field, bypassing LLM transformation
                path = line.replace("[IMAGE_MEDIA:", "").replace("]", "").strip()
                logger.info("Found IMAGE_MEDIA: {}", path)
                image_paths.append(path)
                continue  # Don't add to content_parts
            elif line.startswith("[IMAGE_FILE:"):
                # Extract path from [IMAGE_FILE:/path/to/image.png]
                path = line.replace("[IMAGE_FILE:", "").replace("]", "").strip()
                logger.info("Found IMAGE_FILE: {}", path)
                image_paths.append(path)
            elif "[Saved to]" in line or "Saved to:" in line:
                # Extract path from "[Saved to] /path/to/image.png" or "Saved to: /path/to/image.png"
                import re
                match = re.search(r'\[?Saved to\]?[:\s]+([^\s]+\.png)', line, re.IGNORECASE)
                if match:
                    path = match.group(1)
                    logger.info("Found Saved to: {}", path)
                    image_paths.append(path)
            elif "保存位置" in line:
                # Extract path from "**保存位置:** `/path/to/image.png`" or "保存位置: /path/to/image.png"
                import re
                # Handle markdown formatting: **保存位置:** `/path/image.png` or 保存位置：/path/image.png
                # Match from 保存位置 to the .png file path
                match = re.search(r'保存位置.*?(/[^\s]+\.png)', line, re.IGNORECASE)
                if match:
                    path = match.group(1)
                    logger.info("Found 保存位置: {}", path)
                    image_paths.append(path)
            elif "图片已保存到" in line or "图片已保存" in line:
                # Extract from "图片已保存到：/path/to/image.png" or "图片已保存到：`/path`"
                import re
                match = re.search(r'[图片已保存到|图片已保存][：:\s]*[：]?\s*[\`*]?([^\s`]+png)[`]*', line, re.IGNORECASE)
                if match:
                    path = match.group(1)
                    logger.info("Found 图片已保存到: {}", path)
                    image_paths.append(path)
                else:
                    # Try more general pattern
                    match = re.search(r'(/[^\s]+\.png)', line)
                    if match:
                        path = match.group(1)
                        logger.info("Found path in 图片已保存: {}", path)
                        image_paths.append(path)
            elif line.startswith("!["):
                # Handle markdown image format: ![description](/path/to/image.png) or ![](url)
                # These are often generated by LLM and show as blank in Discord
                import re
                match = re.search(r'!\[.*?\]\(([^)]+\.(?:png|jpg|jpeg|gif|webp))\)', line)
                if match:
                    potential_path = match.group(1)
                    # Check if it's a local path (not a URL)
                    if potential_path.startswith('/') or '~' in potential_path:
                        logger.info("Found markdown image with local path: {}", potential_path)
                        image_paths.append(potential_path)
                        continue  # Don't add to content_parts
                    elif potential_path.startswith('http'):
                        # It's a remote URL - Discord can embed this directly!
                        logger.info("Found markdown image with remote URL: {}", potential_path)
                        # For remote URLs, we can't upload to Discord directly
                        # But we can keep the line as-is for Discord to embed
                        pass
            else:
                content_parts.append(line)

        logger.info("Extracted image_paths: {}", image_paths)

        # Also check media field for image paths
        for media_path in (msg.media or []):
            if media_path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                image_paths.append(media_path)

        try:
            # Send images first if any
            for img_path in image_paths:
                try:
                    logger.info("Sending image to Discord: {}", img_path)
                    with open(img_path, "rb") as f:
                        img_data = f.read()
                    # Upload as file attachment
                    files = {"file": (img_path.rsplit("/", 1)[-1], img_data, "image/png")}
                    response = await self._http.post(
                        url,
                        headers={"Authorization": f"Bot {self.config.token}"},
                        data={"content": ""},
                        files=files
                    )
                    if response.status_code != 200:
                        logger.error("Failed to send image to Discord: {} - {}", response.status_code, response.text)
                    else:
                        logger.info("Image sent successfully to Discord!")
                except Exception as e:
                    logger.error("Error sending image to Discord: {}", e)

            # Send text content
            content = "\n".join(content_parts)
            # Remove the "Saved to" line since we already sent the image
            content = content.replace("\n[Saved to] /", "\n[Saved to]").strip()

            if content:
                chunks = _split_message(content)
                for i, chunk in enumerate(chunks):
                    payload: dict[str, Any] = {"content": chunk}

                    # Only set reply reference on the first chunk
                    if i == 0 and msg.reply_to:
                        payload["message_reference"] = {"message_id": msg.reply_to}
                        payload["allowed_mentions"] = {"replied_user": False}

                    if not await self._send_payload(url, headers, payload):
                        break  # Abort remaining chunks on failure
        finally:
            await self._stop_typing(msg.chat_id)

    async def _send_payload(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> bool:
        """Send a single Discord API payload with retry on rate-limit. Returns True on success."""
        for attempt in range(3):
            try:
                response = await self._http.post(url, headers=headers, json=payload)
                if response.status_code == 429:
                    data = response.json()
                    retry_after = float(data.get("retry_after", 1.0))
                    logger.warning("Discord rate limited, retrying in {}s", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                response.raise_for_status()
                return True
            except Exception as e:
                if attempt == 2:
                    logger.error("Error sending Discord message: {}", e)
                else:
                    await asyncio.sleep(1)
        return False

    async def _gateway_loop(self) -> None:
        """Main gateway loop: identify, heartbeat, dispatch events."""
        if not self._ws:
            return

        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from Discord gateway: {}", raw[:100])
                continue

            op = data.get("op")
            event_type = data.get("t")
            seq = data.get("s")
            payload = data.get("d")

            if seq is not None:
                self._seq = seq

            if op == 10:
                # HELLO: start heartbeat and identify
                interval_ms = payload.get("heartbeat_interval", 45000)
                await self._start_heartbeat(interval_ms / 1000)
                await self._identify()
            elif op == 0 and event_type == "READY":
                logger.info("Discord gateway READY")
            elif op == 0 and event_type == "MESSAGE_CREATE":
                await self._handle_message_create(payload)
            elif op == 7:
                # RECONNECT: exit loop to reconnect
                logger.info("Discord gateway requested reconnect")
                break
            elif op == 9:
                # INVALID_SESSION: reconnect
                logger.warning("Discord gateway invalid session")
                break

    async def _identify(self) -> None:
        """Send IDENTIFY payload."""
        if not self._ws:
            return

        identify = {
            "op": 2,
            "d": {
                "token": self.config.token,
                "intents": self.config.intents,
                "properties": {
                    "os": "nanobot",
                    "browser": "nanobot",
                    "device": "nanobot",
                },
            },
        }
        await self._ws.send(json.dumps(identify))

    async def _start_heartbeat(self, interval_s: float) -> None:
        """Start or restart the heartbeat loop."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        async def heartbeat_loop() -> None:
            while self._running and self._ws:
                payload = {"op": 1, "d": self._seq}
                try:
                    await self._ws.send(json.dumps(payload))
                except Exception as e:
                    logger.warning("Discord heartbeat failed: {}", e)
                    break
                await asyncio.sleep(interval_s)

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    async def _handle_message_create(self, payload: dict[str, Any]) -> None:
        """Handle incoming Discord messages."""
        author = payload.get("author") or {}
        if author.get("bot"):
            return

        sender_id = str(author.get("id", ""))
        channel_id = str(payload.get("channel_id", ""))
        content = payload.get("content") or ""

        if not sender_id or not channel_id:
            return

        if not self.is_allowed(sender_id):
            return

        # Check channel allow list based on guild
        guild_id = str(payload.get("guild_id", ""))

        # Check global channels list first
        if hasattr(self.config, 'channels') and self.config.channels:
            if channel_id not in self.config.channels:
                return

        # Check guild-specific allowlist
        if guild_id and hasattr(self.config, 'channel_allowlist') and self.config.channel_allowlist:
            allowed_channels = self.config.channel_allowlist.get(guild_id, [])
            if allowed_channels and channel_id not in allowed_channels:
                return

        content_parts = [content] if content else []
        media_paths: list[str] = []
        media_dir = Path.home() / ".nanobot" / "media"

        for attachment in payload.get("attachments") or []:
            url = attachment.get("url")
            filename = attachment.get("filename") or "attachment"
            size = attachment.get("size") or 0
            if not url or not self._http:
                continue
            if size and size > MAX_ATTACHMENT_BYTES:
                content_parts.append(f"[attachment: {filename} - too large]")
                continue
            try:
                media_dir.mkdir(parents=True, exist_ok=True)
                file_path = media_dir / f"{attachment.get('id', 'file')}_{filename.replace('/', '_')}"
                resp = await self._http.get(url)
                resp.raise_for_status()
                file_path.write_bytes(resp.content)
                media_paths.append(str(file_path))
                content_parts.append(f"[attachment: {file_path}]")
            except Exception as e:
                logger.warning("Failed to download Discord attachment: {}", e)
                content_parts.append(f"[attachment: {filename} - download failed]")

        reply_to = (payload.get("referenced_message") or {}).get("id")

        await self._start_typing(channel_id)

        await self._handle_message(
            sender_id=sender_id,
            chat_id=channel_id,
            content="\n".join(p for p in content_parts if p) or "[empty message]",
            media=media_paths,
            metadata={
                "message_id": str(payload.get("id", "")),
                "guild_id": payload.get("guild_id"),
                "reply_to": reply_to,
            },
        )

    async def _start_typing(self, channel_id: str) -> None:
        """Start periodic typing indicator for a channel."""
        await self._stop_typing(channel_id)

        async def typing_loop() -> None:
            url = f"{DISCORD_API_BASE}/channels/{channel_id}/typing"
            headers = {"Authorization": f"Bot {self.config.token}"}
            while self._running:
                try:
                    await self._http.post(url, headers=headers)
                except Exception:
                    pass
                await asyncio.sleep(8)

        self._typing_tasks[channel_id] = asyncio.create_task(typing_loop())

    async def _stop_typing(self, channel_id: str) -> None:
        """Stop typing indicator for a channel."""
        task = self._typing_tasks.pop(channel_id, None)
        if task:
            task.cancel()
