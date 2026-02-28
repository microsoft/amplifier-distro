"""Session handoff hook - generates summary at session end.

Fires on SESSION_END, reads the session's recent messages, calls a
haiku-class model to generate a structured summary, and writes it to
the session's handoff.md file (one handoff per session, not per project).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from amplifier_core import HookResult

logger = logging.getLogger(__name__)

HANDOFF_PROMPT = """\
You are a session handoff assistant. Analyze the conversation below and produce
a structured handoff note in markdown format.

The note MUST include these sections:
- **What Was Accomplished**: Concrete deliverables (files created, bugs fixed, decisions made)
- **What's In Progress**: Incomplete items with current state
- **Key Decisions Made**: Decisions with rationale that affect future sessions
- **Suggested Next Steps**: Actionable items in priority order

Be concrete and specific. Include file paths where relevant.
Do NOT include the YAML frontmatter - that will be added separately.

Here is the conversation:
{conversation}
"""


@dataclass
class HandoffConfig:
    """Configuration for the handoff hook."""

    enabled: bool = True
    min_turns: int = 2
    max_context_messages: int = 50
    projects_dir: str = ""

    def __post_init__(self) -> None:
        if not self.projects_dir:
            home = os.environ.get("AMPLIFIER_HOME", os.path.expanduser("~/.amplifier"))
            self.projects_dir = os.path.join(home, "projects")


@dataclass
class HandoffState:
    """Tracks session state for handoff generation."""

    session_id: str = ""
    project_slug: str = ""
    turn_count: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    working_directory: str = ""


class HandoffHook:
    """Generates session handoff notes on SESSION_END."""

    def __init__(self, config: HandoffConfig) -> None:
        self.config = config
        self.state = HandoffState()
        self._coordinator: Any = None

    async def on_session_start(self, event: str, data: dict[str, Any]) -> HookResult:
        """Track session metadata on start and inject the most recent handoff."""
        self.state.session_id = data.get("session_id", "")
        self.state.working_directory = data.get("working_directory", "")
        self.state.project_slug = self._derive_project_slug(
            self.state.working_directory
        )

        if not self.config.enabled:
            return HookResult(action="continue")

        if data.get("parent_id"):
            # Sub-sessions skip injection — they inherit context from the parent.
            return HookResult(action="continue")

        content = self._find_latest_handoff(self.state.project_slug)
        if content:
            logger.debug(
                "Injecting prior handoff for project %r", self.state.project_slug
            )
            return HookResult(
                action="inject_context",
                context_injection=content,
                context_injection_role="system",
            )

        return HookResult(action="continue")

    async def on_prompt_complete(self, event: str, data: dict[str, Any]) -> HookResult:
        """Track conversation turns and messages."""
        self.state.turn_count += 1

        prompt = data.get("prompt", "")
        response = data.get("response", "")
        if prompt:
            self.state.messages.append({"role": "user", "content": str(prompt)})
        if response:
            self.state.messages.append({"role": "assistant", "content": str(response)})

        # Keep only recent messages
        max_msgs = self.config.max_context_messages
        if len(self.state.messages) > max_msgs:
            self.state.messages = self.state.messages[-max_msgs:]

        return HookResult(action="continue")

    async def on_tool_post(self, event: str, data: dict[str, Any]) -> HookResult:
        """Track file changes from tool calls."""
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})

        if tool_name in ("write_file", "edit_file", "apply_patch"):
            file_path = tool_input.get("file_path") or tool_input.get("path", "")
            if file_path and file_path not in self.state.files_changed:
                self.state.files_changed.append(file_path)

        return HookResult(action="continue")

    async def on_session_end(self, event: str, data: dict[str, Any]) -> HookResult:
        """Generate handoff note when session ends."""
        if not self.config.enabled:
            return HookResult(action="continue")

        if self.state.turn_count < self.config.min_turns:
            logger.debug(
                "Skipping handoff: only %d turns (min: %d)",
                self.state.turn_count,
                self.config.min_turns,
            )
            return HookResult(action="continue")

        if not self.state.messages:
            logger.debug("Skipping handoff: no messages recorded")
            return HookResult(action="continue")

        try:
            await self._generate_handoff()
        except Exception:
            logger.exception("Failed to generate handoff note")

        return HookResult(action="continue")

    async def _generate_handoff(self) -> None:
        """Generate and write the handoff note."""
        # Build conversation summary for the LLM
        conversation = self._format_conversation()

        # Use the coordinator to make an LLM call
        prompt = HANDOFF_PROMPT.format(conversation=conversation)

        try:
            response = await self._coordinator.complete(
                messages=[{"role": "user", "content": prompt}],
                model_preference="fast",  # Use cheapest/fastest available model
            )
            summary = (
                response.get("content", "")
                if isinstance(response, dict)
                else str(response)
            )
        except Exception:
            logger.exception("LLM call failed for handoff generation")
            # Fall back to a simple summary
            summary = self._generate_fallback_summary()

        # Build the full handoff document
        handoff_content = self._build_handoff_document(summary)

        # Write to project directory
        self._write_handoff(handoff_content)

    def _format_conversation(self) -> str:
        """Format recent messages for the LLM prompt."""
        lines = []
        for msg in self.state.messages[-30:]:  # Last 30 messages max
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            # Truncate very long messages
            if len(content) > 2000:
                content = content[:2000] + "\n[...truncated...]"
            lines.append(f"**{role}**: {content}")
        return "\n\n".join(lines)

    def _generate_fallback_summary(self) -> str:
        """Generate a basic summary without LLM when the call fails."""
        lines = ["## What Was Accomplished\n"]
        lines.append(f"- Session with {self.state.turn_count} turns completed")
        if self.state.files_changed:
            lines.append(
                f"- Files modified: {', '.join(self.state.files_changed[:10])}"
            )
        lines.append("\n## Suggested Next Steps\n")
        lines.append("- Review the session transcript for details")
        return "\n".join(lines)

    def _build_handoff_document(self, summary: str) -> str:
        """Build the complete handoff.md with YAML frontmatter."""
        now = datetime.now(timezone.utc).isoformat()
        files_yaml = "\n".join(f"  - {f}" for f in self.state.files_changed[:20])
        if not files_yaml:
            files_yaml = "  []"

        frontmatter = f"""---
session_id: {self.state.session_id}
timestamp: {now}
project: {self.state.project_slug}
duration_turns: {self.state.turn_count}
files_changed:
{files_yaml}
---"""

        return f"{frontmatter}\n\n{summary}\n"

    def _write_handoff(self, content: str) -> None:
        """Write handoff.md to the session directory."""
        if not self.state.project_slug:
            logger.warning("No project slug — cannot write handoff")
            return

        if not self.state.session_id:
            logger.warning("No session ID — cannot write handoff")
            return

        session_dir = (
            Path(self.config.projects_dir)
            / self.state.project_slug
            / "sessions"
            / self.state.session_id
        )
        session_dir.mkdir(parents=True, exist_ok=True)

        handoff_path = session_dir / "handoff.md"
        handoff_path.write_text(content, encoding="utf-8")
        logger.info("Handoff written to %s", handoff_path)

    def _find_latest_handoff(self, slug: str) -> str | None:
        """Return content of the most recent handoff.md for this project, or None.

        Globs <projects_dir>/<slug>/sessions/*/handoff.md, sorts by file mtime
        (newest first), reads the top result, and returns its content as a string.
        Returns None if no files exist, or if the newest file is empty.
        """
        sessions_dir = Path(self.config.projects_dir) / slug / "sessions"
        if not sessions_dir.exists():
            return None
        candidates = sorted(
            (p for p in sessions_dir.glob("*/handoff.md") if p.exists()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        try:
            content = candidates[0].read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("Could not read handoff file: %s", candidates[0])
            return None
        if not content:
            logger.warning("Most recent handoff file is empty: %s", candidates[0])
            return None
        return content

    def _derive_project_slug(self, working_dir: str) -> str:
        """Derive project slug from working directory."""
        if not working_dir:
            return "unknown"
        return Path(working_dir).name


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Mount the handoff hook module."""
    config = config or {}

    hook_config = HandoffConfig(
        enabled=config.get("enabled", True),
        min_turns=config.get("min_turns", 2),
        max_context_messages=config.get("max_context_messages", 50),
        projects_dir=config.get("projects_dir", ""),
    )

    hook = HandoffHook(hook_config)
    hook._coordinator = coordinator

    coordinator.hooks.register(
        "session:start", hook.on_session_start, priority=10, name="handoff-start"
    )
    coordinator.hooks.register(
        "prompt:complete", hook.on_prompt_complete, priority=90, name="handoff-track"
    )
    coordinator.hooks.register(
        "tool:post", hook.on_tool_post, priority=90, name="handoff-files"
    )
    coordinator.hooks.register(
        "session:end", hook.on_session_end, priority=90, name="handoff-generate"
    )

    return {
        "name": "hooks-handoff",
        "version": "0.1.0",
        "description": "Session handoff generation on SESSION_END",
        "config": {
            "enabled": hook_config.enabled,
            "min_turns": hook_config.min_turns,
        },
    }
