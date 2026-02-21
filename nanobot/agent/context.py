"""Context builder for assembling agent prompts."""

import base64
import io
import mimetypes
import platform
from pathlib import Path
from typing import Any

from PIL import Image

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.

    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]

    def __init__(self, workspace: Path, media_config: "MediaConfig | None" = None, vision_api_key: str = ""):
        from nanobot.config.schema import MediaConfig
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self.media_config = media_config or MediaConfig()
        self.vision_api_key = vision_api_key  # API key for vision model
    
    def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """
        Build the system prompt from bootstrap files, memory, and skills.
        
        Args:
            skill_names: Optional list of skills to include.
        
        Returns:
            Complete system prompt.
        """
        parts = []
        
        # Core identity
        parts.append(self._get_identity())
        
        # Bootstrap files
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)
        
        # Memory context
        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")
        
        # Skills - progressive loading
        # 1. Always-loaded skills: include full content
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")
        
        # 2. Available skills: only show summary (agent uses read_file to load)
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")
        
        return "\n\n---\n\n".join(parts)
    
    def _get_identity(self) -> str:
        """Get the core identity section."""
        from datetime import datetime
        import time as _time
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        
        return f"""# nanobot 🐈

You are nanobot, a helpful AI assistant. You have access to tools that allow you to:
- Read, write, and edit files
- Execute shell commands
- Search the web and fetch web pages
- Send messages to users on chat channels
- Spawn subagents for complex background tasks

## Current Time
{now} ({tz})

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable)
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

IMPORTANT: When responding to direct questions or conversations, reply directly with your text response.
Only use the 'message' tool when you need to send a message to a specific chat channel (like WhatsApp).
For normal conversation, just respond with text - do not call the message tool.

Always be helpful, accurate, and concise. Before calling tools, briefly tell the user what you're about to do (one short sentence in the user's language).
When remembering something important, write to {workspace_path}/memory/MEMORY.md
To recall past events, grep {workspace_path}/memory/HISTORY.md"""
    
    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        
        return "\n\n".join(parts) if parts else ""
    
    async def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build the complete message list for an LLM call.

        Args:
            history: Previous conversation messages.
            current_message: The new user message.
            skill_names: Optional skills to include.
            media: Optional list of local file paths for images/media.
            channel: Current channel (telegram, feishu, etc.).
            chat_id: Current chat/user ID.

        Returns:
            List of messages including system prompt.
        """
        messages = []

        # System prompt
        system_prompt = self.build_system_prompt(skill_names)
        if channel and chat_id:
            system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"
        messages.append({"role": "system", "content": system_prompt})

        # History
        messages.extend(history)

        # Current message (with optional image attachments and understanding)
        user_content = await self._build_user_content_async(current_message, media)
        messages.append({"role": "user", "content": user_content})

        return messages

    async def _build_user_content_async(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional images and AI understanding.

        If image understanding is enabled, describes each image using a vision model
        and includes the description in the message.
        """
        if not media:
            return text

        # Check if we need image understanding
        use_understanding = (
            self.media_config.image.understanding
            and self.vision_provider
        )

        descriptions = []
        if use_understanding:
            for path in media:
                p = Path(path)
                if p.is_file():
                    desc = await self.describe_image(p)
                    if desc:
                        descriptions.append(desc)

        # Build images list
        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue

            # Process image if media processing is enabled
            if self.media_config.image.enabled:
                processed_data = self._process_image(p)
            else:
                processed_data = p.read_bytes()
                mime = "image/jpeg"

            b64 = base64.b64encode(processed_data).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        # Build content
        content_parts = []

        # Add image descriptions if available
        if descriptions:
            content_parts.append("[Image Descriptions]\n" + "\n\n".join(f"- {d}" for d in descriptions))

        # Add images
        if images:
            content_parts.extend(images)

        # Add original text
        content_parts.append({"type": "text", "text": text})

        if not content_parts:
            return text
        return content_parts

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional base64-encoded images.

        Images are processed (resized, compressed) before encoding if media processing is enabled.
        """
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue

            # Process image if media processing is enabled
            if self.media_config.image.enabled:
                processed_data = self._process_image(p)
            else:
                processed_data = p.read_bytes()
                mime = "image/jpeg"  # Default to jpeg for processed images

            b64 = base64.b64encode(processed_data).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]

    def _process_image(self, path: Path) -> tuple[bytes, str]:
        """Process image: resize, compress, and limit file size.

        Returns:
            Tuple of (processed_bytes, mime_type)
        """
        img = Image.open(path)
        config = self.media_config.image

        # Convert to RGB if necessary (handles RGBA, palette, etc.)
        if img.mode not in ("RGB", "L"):  # L is grayscale
            img = img.convert("RGB")

        # Resize if larger than max_size (maintaining aspect ratio)
        max_dim = config.max_size
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

        # Compress to JPEG with quality setting
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=config.quality, optimize=True)
        data = output.getvalue()

        # Further compress if still over max_bytes
        if len(data) > config.max_bytes:
            # Reduce quality until under limit
            quality = config.quality
            while quality > 10 and len(data) > config.max_bytes:
                output = io.BytesIO()
                img.save(output, format="JPEG", quality=quality, optimize=True)
                data = output.getvalue()
                quality -= 10

        return data, "image/jpeg"

    async def describe_image(self, image_path: Path) -> str | None:
        """Describe an image using MiniMax VLM API.

        Args:
            image_path: Path to the image file.

        Returns:
            Description of the image, or None if description fails.
        """
        if not self.media_config.image.understanding:
            return None

        if not self.vision_api_key:
            return None

        try:
            # Process image first
            processed_data, mime = self._process_image(image_path)
            b64 = base64.b64encode(processed_data).decode()

            # Call MiniMax VLM API directly (like OpenCLAW does)
            import httpx
            url = "https://api.minimax.io/v1/coding_plan/vlm"

            headers = {
                "Authorization": f"Bearer {self.vision_api_key}",
                "Content-Type": "application/json",
                "MM-API-Source": "nanobot"
            }

            payload = {
                "prompt": "Describe this image in detail. Focus on: objects, people, text, colors, setting, and any notable features.",
                "image_url": f"data:{mime};base64,{b64}"
            }

            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, headers=headers, timeout=30.0)
                response.raise_for_status()
                result = response.json()

            # Extract content from response
            content = result.get("content", "")
            if content:
                return content
            return None
        except Exception as e:
            # Log error but don't fail - just return None
            import logging
            logging.getLogger(__name__).warning(f"Failed to describe image: {e}")
            return None
            return None
    
    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.
        
        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result.
        
        Returns:
            Updated message list.
        """
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result
        })
        return messages
    
    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.
        
        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.
            reasoning_content: Thinking output (Kimi, DeepSeek-R1, etc.).
        
        Returns:
            Updated message list.
        """
        msg: dict[str, Any] = {"role": "assistant"}

        # Always include content — some providers (e.g. StepFun) reject
        # assistant messages that omit the key entirely.
        msg["content"] = content

        if tool_calls:
            msg["tool_calls"] = tool_calls

        # Include reasoning content when provided (required by some thinking models)
        if reasoning_content:
            msg["reasoning_content"] = reasoning_content

        messages.append(msg)
        return messages
