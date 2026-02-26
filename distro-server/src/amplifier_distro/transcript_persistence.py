"""Transcript persistence for distro server sessions.

Registers hooks on tool:post and orchestrator:complete that write
transcript.jsonl incrementally during execution.  Uses distro's own
atomic_write for crash safety.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from amplifier_core.models import HookResult

from amplifier_distro.conventions import TRANSCRIPT_FILENAME
from amplifier_distro.fileutil import atomic_write

logger = logging.getLogger(__name__)

_PRIORITY = 900
_EXCLUDED_ROLES = frozenset({"system", "developer"})

# Resolve sanitize_message once at import time.
try:
    from amplifier_foundation import sanitize_message as _foundation_sanitize
except ImportError:
    _foundation_sanitize = None  # type: ignore[assignment]


def _sanitize(msg: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a message for JSON persistence.

    Uses amplifier_foundation.sanitize_message() when available, with a
    workaround for content:null stripping (providers need content:null on
    tool-call messages).
    # TODO: upstream fix for sanitize_message dropping content:null
    """
    had_content_null = "content" in msg and msg["content"] is None

    sanitized = _foundation_sanitize(msg) if _foundation_sanitize is not None else msg

    # Restore content:null -- sanitize_message strips None values but
    # providers reject tool-call messages missing the content field.
    if had_content_null and "content" not in sanitized:
        sanitized["content"] = None

    return sanitized


def write_transcript(session_dir: Path, messages: list[dict[str, Any]]) -> None:
    """Write messages to transcript.jsonl, filtering system/developer roles.

    Full rewrite (not append) -- context compaction can change earlier messages.
    Uses atomic_write for crash safety.

    Args:
        session_dir: Directory to write transcript.jsonl into.
        messages: List of message dicts from context.get_messages().
    """
    lines: list[str] = []
    for msg in messages:
        try:
            msg_dict = (
                msg
                if isinstance(msg, dict)
                else getattr(msg, "model_dump", lambda _m=msg: _m)()
            )
            if msg_dict.get("role") in _EXCLUDED_ROLES:
                continue
            sanitized = _sanitize(msg_dict)
            lines.append(json.dumps(sanitized, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            logger.debug("Skipping unserializable message", exc_info=True)

    content = "\n".join(lines) + "\n" if lines else ""
    session_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(session_dir / TRANSCRIPT_FILENAME, content)


class TranscriptSaveHook:
    """Persists transcript.jsonl incrementally during execution.

    Registered on tool:post (mid-turn durability) and
    orchestrator:complete (end-of-turn, catches no-tool turns).
    Debounces by message count -- skips write if unchanged.
    Best-effort: never fails the agent loop.
    """

    def __init__(self, session: Any, session_dir: Path) -> None:
        self._session = session
        self._session_dir = session_dir
        self._last_count = 0

    async def __call__(self, event: str, data: dict[str, Any]) -> Any:
        try:
            context = self._session.coordinator.get("context")
            if not context or not hasattr(context, "get_messages"):
                return HookResult(action="continue")

            messages = await context.get_messages()
            count = len(messages)

            # Debounce: skip if message count unchanged
            if count <= self._last_count:
                return HookResult(action="continue")

            write_transcript(self._session_dir, messages)
            self._last_count = count  # update only after successful write

        except Exception:  # noqa: BLE001
            logger.warning("Transcript save failed", exc_info=True)

        return HookResult(action="continue")


def register_transcript_hooks(session: Any, session_dir: Path) -> None:
    """Register transcript persistence hooks on a session.

    Safe to call on both fresh and resumed sessions.
    Silently no-ops if hooks API is unavailable.
    """
    try:
        hook = TranscriptSaveHook(session, session_dir)
        hooks = session.coordinator.hooks
        hooks.register(
            event="tool:post",
            handler=hook,
            priority=_PRIORITY,
            name="bridge-transcript:tool:post",
        )
        hooks.register(
            event="orchestrator:complete",
            handler=hook,
            priority=_PRIORITY,
            name="bridge-transcript:orchestrator:complete",
        )
        logger.debug(
            "Transcript hooks registered -> %s", session_dir / TRANSCRIPT_FILENAME
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not register transcript hooks", exc_info=True)
