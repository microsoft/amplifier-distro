"""Metadata persistence for distro server sessions.

Writes metadata.json at session creation and updates it on
orchestrator:complete.  Uses distro's own atomic_write for crash safety.

Follows the same pattern as transcript_persistence.py -- a module-level
write helper plus a hook class registered by the backend.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amplifier_core.models import HookResult

from amplifier_distro.conventions import METADATA_FILENAME
from amplifier_distro.fileutil import atomic_write

logger = logging.getLogger(__name__)

_PRIORITY = 900  # same tier as transcript persistence


def write_metadata(session_dir: Path, metadata: dict[str, Any]) -> None:
    """Write metadata dict to metadata.json atomically.

    Merges *metadata* on top of any existing file content so fields set
    by other writers (e.g. hooks-session-naming) are preserved.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = session_dir / METADATA_FILENAME

    existing: dict[str, Any] = {}
    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    merged = {**existing, **metadata}
    content = json.dumps(merged, indent=2, ensure_ascii=False)
    atomic_write(metadata_path, content)


class MetadataSaveHook:
    """Updates metadata.json on orchestrator:complete.

    Bumps ``turn_count`` (number of user messages in context) and
    ``last_updated``.  Best-effort: never fails the agent loop.
    """

    def __init__(self, session: Any, session_dir: Path) -> None:
        self._session = session
        self._session_dir = session_dir

    async def __call__(self, event: str, data: dict[str, Any]) -> Any:
        try:
            context = self._session.coordinator.get("context")
            if not context or not hasattr(context, "get_messages"):
                return HookResult(action="continue")

            messages = await context.get_messages()
            turn_count = sum(
                1 for m in messages if isinstance(m, dict) and m.get("role") == "user"
            )

            write_metadata(
                self._session_dir,
                {
                    "turn_count": turn_count,
                    "last_updated": datetime.now(tz=UTC).isoformat(),
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("Metadata save failed", exc_info=True)

        return HookResult(action="continue")


def register_metadata_hooks(session: Any, session_dir: Path) -> None:
    """Register metadata persistence hook on a session.

    Safe to call on both fresh and resumed sessions.
    Silently no-ops if hooks API is unavailable.
    """
    try:
        hook = MetadataSaveHook(session, session_dir)
        hooks = session.coordinator.hooks
        hooks.register(
            event="orchestrator:complete",
            handler=hook,
            priority=_PRIORITY,
            name="bridge-metadata:orchestrator:complete",
        )
        logger.debug(
            "Metadata hook registered -> %s", session_dir / METADATA_FILENAME
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not register metadata hooks", exc_info=True)