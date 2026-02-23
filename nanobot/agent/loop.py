"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

import json_repair
from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.agent.tools.tavily import TavilySearchTool
from nanobot.agent.tools.image import ImageUnderstandTool
from nanobot.agent.tools.image_generate import ImageGenerateTool
from nanobot.agent.tools.browser import BrowserTool
from nanobot.agent.tools.session import SessionTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ExecToolConfig
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 20,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        tavily_api_key: str | None = None,
        gemini_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        media_config: "MediaConfig | None" = None,
        vision_api_key: str = "",
    ):
        from nanobot.config.schema import ExecToolConfig, MediaConfig
        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.tavily_api_key = tavily_api_key
        self.gemini_api_key = gemini_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.media_config = media_config or MediaConfig()
        self.vision_api_key = vision_api_key
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace, media_config=self.media_config, vision_api_key=self.vision_api_key)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        # File tools (workspace for relative paths, restrict if configured)
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        self.tools.register(ReadFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(WriteFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(EditFileTool(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(ListDirTool(workspace=self.workspace, allowed_dir=allowed_dir))

        # Shell tool
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            restrict_to_workspace=self.restrict_to_workspace,
        ))

        # Web tools
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(TavilySearchTool(api_key=self.tavily_api_key or os.environ.get("TAVILY_API_KEY")))
        self.tools.register(WebFetchTool())

        # Message tool
        message_tool = MessageTool(send_callback=self.bus.publish_outbound)
        self.tools.register(message_tool)

        # Spawn tool (for subagents)
        spawn_tool = SpawnTool(manager=self.subagents)
        self.tools.register(spawn_tool)

        # Image understanding tool
        self.tools.register(ImageUnderstandTool(context_builder=self.context))
        self.tools.register(ImageGenerateTool(
            api_key=self.gemini_api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"),
            workspace=str(self.workspace)
        ))

        # Browser tool
        self.tools.register(BrowserTool(workspace=self.workspace))
        self.tools.register(SessionTool(session_manager=self.sessions))

        # Cron tool (for scheduling)
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

        # Loop detection: track (tool_name, args_hash) to detect repetitive calls
        self._tool_call_history: list[tuple[str, str]] = []
        self._loop_detection_max: int = 3  # Max consecutive identical calls before stopping

    def _track_tool_call(self, tool_name: str, args: dict) -> bool:
        """
        Track tool call for loop detection.
        Returns True if loop detected (3+ consecutive identical calls), False otherwise.
        """
        # Create a hashable key from tool_name + sorted args
        args_str = json.dumps(args, sort_keys=True, ensure_ascii=False)
        call_key = (tool_name, args_str)
        
        # Check if this is the same as the last call
        if self._tool_call_history and self._tool_call_history[-1] == call_key:
            self._tool_call_history.append(call_key)
        else:
            # Reset history if different call
            self._tool_call_history = [call_key]
        
        # Check if we've exceeded the threshold
        if len(self._tool_call_history) >= self._loop_detection_max:
            # Check if ALL calls in history are identical
            if all(key == self._tool_call_history[0] for key in self._tool_call_history):
                logger.warning("Loop detected: {} consecutive identical tool calls", len(self._tool_call_history))
                return True
        
        return False

    def _reset_tool_tracking(self) -> None:
        """Reset tool call tracking at the start of each user request."""
        self._tool_call_history = []

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.set_context(channel, chat_id, message_id)

        if spawn_tool := self.tools.get("spawn"):
            if isinstance(spawn_tool, SpawnTool):
                spawn_tool.set_context(channel, chat_id)

        if cron_tool := self.tools.get("cron"):
            if isinstance(cron_tool, CronTool):
                cron_tool.set_context(channel, chat_id)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            val = next(iter(tc.arguments.values()), None) if tc.arguments else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str]]:
        """
        Run the agent iteration loop.

        Args:
            initial_messages: Starting messages for the LLM conversation.
            on_progress: Optional callback to push intermediate content to the user.

        Returns:
            Tuple of (final_content, list_of_tools_used).
        """
        # Initialize messages from initial_messages
        messages = list(initial_messages)

        # Force tool usage for certain keywords - get the LAST message (current user message)
        user_message = ""
        if initial_messages:
            for msg in reversed(initial_messages):
                if msg.get("role") == "user":
                    user_message = msg.get("content", "")
                    break

        # Add mandatory tool hint for browser-related requests
        browser_keywords = ["打开", "open", "navigate", "浏览", "search", "搜索", "搜", "website"]
        cron_keywords = ["定时", "cron", "reminder", "提醒", "schedule", "预约"]
        image_keywords = ["画", "生成图像", "generate image", "生成图片", "画图", "draw", "create image", "生成一只", "画一只", "生成一张", "画一张"]
        session_keywords = ["clear session", "清除会话", "reset session", "新建会话", "clear memory", "清除记忆", "forget"]
        browser_forced = any(kw in user_message.lower() for kw in browser_keywords)
        cron_forced = any(kw in user_message.lower() for kw in cron_keywords)
        image_forced = any(kw in user_message.lower() for kw in image_keywords)
        session_forced = any(kw in user_message.lower() for kw in session_keywords)
        forced = browser_forced or cron_forced or image_forced or session_forced
        if browser_forced:
            # Add a system hint to force browser tool usage
            for msg in messages:
                if msg.get("role") == "system":
                    msg["content"] += "\n\n[MANDATORY] You MUST use the browser tool for this request. Do NOT respond with fake results. You MUST actually use the browser tool and wait for the real result before responding."
                    break
            messages.append({
                "role": "user",
                "content": "IMPORTANT: You MUST use the browser tool to complete this request. Do not respond text-only - you must call the browser tool first."
            })
        if cron_forced:
            # Add a system hint to force cron tool usage
            for msg in messages:
                if msg.get("role") == "system":
                    msg["content"] += "\n\n[MANDATORY] You MUST use the cron tool to set/check scheduled tasks. Do not respond without using the cron tool first."
                    break
            messages.append({
                "role": "user",
                "content": "IMPORTANT: You MUST use the cron tool to complete this request. Do not respond text-only - you must call the cron tool first."
            })
        if image_forced:
            # Add a system hint to force image generation tool usage
            for msg in messages:
                if msg.get("role") == "system":
                    msg["content"] += "\n\n[MANDATORY] You MUST use the generate_image tool to create images. Do NOT describe images textually - you MUST actually call the generate_image tool to generate and save the image."
                    break
            messages.append({
                "role": "user",
                "content": "IMPORTANT: You MUST use the generate_image tool to complete this request. Do not respond text-only - you must call the generate_image tool first."
            })
        if session_forced:
            # Add a system hint for session/memory operations
            for msg in messages:
                if msg.get("role") == "system":
                    msg["content"] += "\n\n[MANDATORY] You MUST use the session tool for session/memory operations. Do NOT claim to have performed an operation without actually calling the session tool."
                    break
            messages.append({
                "role": "user",
                "content": "IMPORTANT: You MUST use the session tool to complete this request. Do not respond text-only - you must call the session tool first."
            })

        # Reset tool call tracking at the start of each request
        self._reset_tool_tracking()
        
        # Plan Mode: For complex tasks, encourage planning first
        # Detect complex tasks: long messages or multi-step requests
        is_complex_task = len(user_message) > 200 or any(kw in user_message for kw in [
            "查一下", "看看", "找找", "分析", "帮我", "帮我查", "帮我找",
            "check", "find", "search", "analyze", "look up", "research"
        ])
        
        # Track if this task should follow a plan
        self._has_plan = False
        self._plan_steps: list[str] = []
        
        if is_complex_task:
            # Add planning hint for complex tasks (works with or without forced tools)
            for msg in messages:
                if msg.get("role") == "system":
                    msg["content"] += "\n\n[PLANNING MODE] For complex tasks, first think about the steps needed and output a brief plan. IMPORTANT: Format your plan like this so I can track progress:\n\n**TODO:**\n- [ ] **Step 1 name**: description\n- [ ] **Step 2 name**: description\n- [ ] **Step 3 name**: description\n\nThen execute each step and mark them as [x] when done."
                    break
            messages.append({
                "role": "user",
                "content": "For this complex task, please first output a plan with clear step names like '- [ ] **Search**: 搜索内容', then execute each step and mark them as [x] when done."
            })
            self._has_plan = True
        
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        text_only_retried = False

        while iteration < self.max_iterations:
            iteration += 1

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if response.has_tool_calls:
                if on_progress:
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean)
                    await on_progress(self._tool_hint(response.tool_calls))

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])
                    
                    # Loop detection: check if this is a repetitive call
                    if self._track_tool_call(tool_call.name, tool_call.arguments):
                        loop_message = f"[LOOP DETECTED] Detected {self._loop_detection_max} consecutive identical tool calls: {tool_call.name} with identical arguments. Stopping to prevent infinite loop. Please try a different approach."
                        logger.warning(loop_message)
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, loop_message
                        )
                        final_content = loop_message
                        break
                    
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    
                    # Tool Result Verification: Check for failure indicators
                    result_lower = str(result).lower()
                    failure_indicators = ["failed", "error", "exception", "timeout", "not found", "permission denied", "无法", "错误", "失败"]
                    is_failure = any(indicator in result_lower for indicator in failure_indicators)
                    
                    if is_failure:
                        # Add a hint to force the model to handle the failure
                        failure_hint = f"\n\n[TOOL RESULT VERIFICATION] The tool returned an error/failure: {result[:200]}. You MUST either: (1) Try a different approach, or (2) Admit the failure to the user. Do NOT pretend the tool succeeded!"
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result + failure_hint
                        )
                    else:
                        messages = self.context.add_tool_result(
                            messages, tool_call.id, tool_call.name, result
                        )
                    
                    # Plan Adherence Check removed - let model handle final progress display
                
                # If loop was detected and we broke out of the tool loop, exit the main loop too
                if final_content and "LOOP DETECTED" in final_content:
                    break
            else:
                final_content = self._strip_think(response.content)
                # Some models send an interim text response before tool calls.
                # If browser tool is expected, keep retrying until tool is used.
                max_retries = 5 if forced else 1
                if not tools_used and final_content and iteration < max_retries:
                    logger.debug("Interim text response (no tools used yet), retrying ({}/{}): {}", iteration, max_retries, final_content[:80])
                    final_content = None
                    continue
                break

        return final_content, tools_used

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        await self._connect_mcp()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(
                    self.bus.consume_inbound(),
                    timeout=1.0
                )
                try:
                    response = await self._process_message(msg)
                    await self.bus.publish_outbound(response or OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id, content="",
                    ))
                except Exception as e:
                    logger.error("Error processing message: {}", e)
                    await self.bus.publish_outbound(OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content=f"Sorry, I encountered an error: {str(e)}"
                    ))
            except asyncio.TimeoutError:
                continue

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.
            session_key: Override session key (used by process_direct).
            on_progress: Optional callback for intermediate output (defaults to bus publish).

        Returns:
            The response message, or None if no response needed.
        """
        # System messages route back via chat_id ("channel:chat_id")
        if msg.channel == "system":
            return await self._process_system_message(msg)

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)

        # Handle slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/new":
            # Capture messages before clearing (avoid race condition with background task)
            messages_to_archive = session.messages.copy()
            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)

            async def _consolidate_and_cleanup():
                temp_session = Session(key=session.key)
                temp_session.messages = messages_to_archive
                await self._consolidate_memory(temp_session, archive_all=True)

            asyncio.create_task(_consolidate_and_cleanup())
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started. Memory consolidation in progress.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 nanobot commands:\n/new — Start a new conversation\n/help — Show available commands")

        if len(session.messages) > self.memory_window and session.key not in self._consolidating:
            self._consolidating.add(session.key)

            async def _consolidate_and_unlock():
                try:
                    await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)

            asyncio.create_task(_consolidate_and_unlock())

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        initial_messages = await self.context.build_messages(
            history=session.get_history(max_messages=self.memory_window),
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content,
                metadata=meta,
            ))

        final_content, tools_used = await self._run_agent_loop(
            initial_messages, on_progress=on_progress or _bus_progress,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)

        session.add_message("user", msg.content)
        session.add_message("assistant", final_content,
                            tools_used=tools_used if tools_used else None)
        self.sessions.save(session)

        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                return None

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},  # Pass through for channel-specific needs (e.g. Slack thread_ts)
        )

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).

        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info("Processing system message from {}", msg.sender_id)

        # Parse origin from chat_id (format: "channel:chat_id")
        if ":" in msg.chat_id:
            parts = msg.chat_id.split(":", 1)
            origin_channel = parts[0]
            origin_chat_id = parts[1]
        else:
            # Fallback
            origin_channel = "cli"
            origin_chat_id = msg.chat_id

        session_key = f"{origin_channel}:{origin_chat_id}"
        session = self.sessions.get_or_create(session_key)
        self._set_tool_context(origin_channel, origin_chat_id, msg.metadata.get("message_id"))
        initial_messages = await self.context.build_messages(
            history=session.get_history(max_messages=self.memory_window),
            current_message=msg.content,
            channel=origin_channel,
            chat_id=origin_chat_id,
        )
        final_content, _ = await self._run_agent_loop(initial_messages)

        if final_content is None:
            final_content = "Background task completed."

        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message("assistant", final_content)
        self.sessions.save(session)

        return OutboundMessage(
            channel=origin_channel,
            chat_id=origin_chat_id,
            content=final_content
        )

    async def _consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Consolidate old messages into MEMORY.md + HISTORY.md.

        Args:
            archive_all: If True, clear all messages and reset session (for /new command).
                       If False, only write to files without modifying session.
        """
        memory = MemoryStore(self.workspace)

        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} total messages archived", len(session.messages))
        else:
            keep_count = self.memory_window // 2
            if len(session.messages) <= keep_count:
                logger.debug("Session {}: No consolidation needed (messages={}, keep={})", session.key, len(session.messages), keep_count)
                return

            messages_to_process = len(session.messages) - session.last_consolidated
            if messages_to_process <= 0:
                logger.debug("Session {}: No new messages to consolidate (last_consolidated={}, total={})", session.key, session.last_consolidated, len(session.messages))
                return

            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return
            logger.info("Memory consolidation started: {} total, {} new to consolidate, {} keep", len(session.messages), len(old_messages), keep_count)

        lines = []
        for m in old_messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")
        conversation = "\n".join(lines)
        current_memory = memory.read_long_term()

        prompt = f"""You are a memory consolidation agent. Process this conversation and return a JSON object with exactly two keys:

1. "history_entry": A paragraph (2-5 sentences) summarizing the key events/decisions/topics. Start with a timestamp like [YYYY-MM-DD HH:MM]. Include enough detail to be useful when found by grep search later.

2. "memory_update": The updated long-term memory content. Add any new facts: user location, preferences, personal info, habits, project context, technical decisions, tools/services used. If nothing new, return the existing content unchanged.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{conversation}

**IMPORTANT**: Both values MUST be strings, not objects or arrays.

Example:
{{
  "history_entry": "[2026-02-14 22:50] User asked about...",
  "memory_update": "- Host: HARRYBOOK-T14P\n- Name: Nado"
}}

Respond with ONLY valid JSON, no markdown fences."""

        try:
            response = await self.provider.chat(
                messages=[
                    {"role": "system", "content": "You are a memory consolidation agent. Respond only with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                model=self.model,
            )
            text = (response.content or "").strip()
            if not text:
                logger.warning("Memory consolidation: LLM returned empty response, skipping")
                return
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            result = json_repair.loads(text)
            if not isinstance(result, dict):
                logger.warning("Memory consolidation: unexpected response type, skipping. Response: {}", text[:200])
                return

            if entry := result.get("history_entry"):
                # Defensive: ensure entry is a string (LLM may return dict)
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                memory.append_history(entry)
            if update := result.get("memory_update"):
                # Defensive: ensure update is a string
                if not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                if update != current_memory:
                    memory.write_long_term(update)

            if archive_all:
                session.last_consolidated = 0
            else:
                session.last_consolidated = len(session.messages) - keep_count
            logger.info("Memory consolidation done: {} messages, last_consolidated={}", len(session.messages), session.last_consolidated)
        except Exception as e:
            logger.error("Memory consolidation failed: {}", e)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).

        Args:
            content: The message content.
            session_key: Session identifier (overrides channel:chat_id for session lookup).
            channel: Source channel (for tool context routing).
            chat_id: Source chat ID (for tool context routing).
            on_progress: Optional callback for intermediate output.

        Returns:
            The agent's response.
        """
        await self._connect_mcp()
        msg = InboundMessage(
            channel=channel,
            sender_id="user",
            chat_id=chat_id,
            content=content
        )

        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""
