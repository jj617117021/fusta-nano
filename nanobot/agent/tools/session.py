"""Session management tool for creating and managing isolated sessions."""

import uuid
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.session.manager import SessionManager


class SessionTool(Tool):
    """Manage conversation sessions - create, list, and switch between sessions."""

    name = "session"
    description = """Manage conversation sessions.

**Actions:**
- **create**: Create a new isolated session (returns a new session key)
- **list**: List all existing sessions
- **info**: Get info about a specific session
- **switch**: Switch to a different session (returns the session key)
- **clear**: Clear messages in a session
- **delete**: Delete a session completely

Examples:
- Create new session: {"action": "create"}
- List sessions: {"action": "list"}
- Delete session: {"action": "delete", "key": "isolated:xxx"}
"""

    def __init__(self, session_manager: SessionManager):
        self.sessions = session_manager
        self._current_key: str | None = None

    async def execute(self, action: str = "list", key: str | None = None, **kwargs: Any) -> str:
        """Execute a session action."""
        action = action.lower()

        if action == "create":
            # Create a new isolated session with random UUID
            new_key = f"isolated:{uuid.uuid4()}"
            session = self.sessions.get_or_create(new_key)
            session.metadata["created_by"] = "session_tool"
            session.metadata["isolated"] = True
            self.sessions.save(session)
            self._current_key = new_key
            return f"[VERIFIED] Created new isolated session: {new_key}"

        elif action == "list":
            # List all sessions
            sessions = self.sessions.list_sessions()
            if not sessions:
                return "No sessions found."

            lines = ["[SESSIONS]"]
            for s in sessions[:20]:
                key = s.get("key", "unknown")
                updated = s.get("updated_at", "unknown")
                lines.append(f"- {key} (updated: {updated[:19] if updated else 'unknown'})")

            if len(sessions) > 20:
                lines.append(f"... and {len(sessions) - 20} more")

            return "\n".join(lines)

        elif action == "info":
            # Get info about a specific session
            if not key:
                return "Error: key required for info action"

            session = self.sessions.get_or_create(key)
            msg_count = len(session.messages)
            created = session.created_at.isoformat() if session.created_at else "unknown"
            updated = session.updated_at.isoformat() if session.updated_at else "unknown"
            isolated = session.metadata.get("isolated", False)

            return f"""[SESSION INFO]
Key: {key}
Isolated: {isolated}
Messages: {msg_count}
Created: {created}
Updated: {updated}"""

        elif action == "switch":
            # Switch to a different session
            if not key:
                return "Error: key required for switch action"

            # Verify session exists
            session = self.sessions.get_or_create(key)
            self._current_key = key
            return f"[VERIFIED] Switched to session: {key}"

        elif action == "clear":
            # Clear messages in a session
            if not key:
                key = self._current_key

            if not key:
                return "Error: key required for clear action"

            session = self.sessions.get_or_create(key)
            session.clear()
            self.sessions.save(session)
            return f"[VERIFIED] Cleared session: {key}"

        elif action == "delete":
            # Delete a session completely
            if not key:
                return "Error: key required for delete action"

            # Get the path and delete
            path = self.sessions._get_session_path(key)
            if path.exists():
                path.unlink()
                self.sessions.invalidate(key)
                return f"[VERIFIED] Deleted session: {key}"

            # Also try legacy path
            legacy_path = self.sessions._get_legacy_session_path(key)
            if legacy_path.exists():
                legacy_path.unlink()
                return f"[VERIFIED] Deleted session: {key}"

            return f"Session not found: {key}"

        else:
            return f"Unknown action: {action}. Use: create, list, info, switch, clear, delete"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "info", "switch", "clear", "delete"],
                    "description": "Action to perform"
                },
                "key": {
                    "type": "string",
                    "description": "Session key (e.g., 'discord:123456' or 'isolated:uuid')"
                }
            },
            "required": ["action"]
        }
