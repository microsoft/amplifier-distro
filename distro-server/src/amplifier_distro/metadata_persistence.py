"""Metadata persistence for distro server sessions.

Writes metadata.json on the first orchestrator:complete event (so
the session directory already exists from transcript persistence) and
updates it on every subsequent turn.  Uses distro's own atomic_write
for crash safety.

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

    The caller is responsible for ensuring *session_dir* exists (the
    transcript hook creates it).  This function does NOT mkdir to avoid
    creating a session directory before any transcript is written.
    """
    if not session_dir.exists():
        return
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
    """Writes metadata.json on orchestrator:complete.

    On the first invocation, writes the initial metadata fields
    (session_id, created, bundle, working_dir, description) that were
    captured at session-creation time.  On every invocation, updates
    ``turn_count`` and ``last_updated``.

    Best-effort: never fails the agent loop.
    """

    def __init__(
        self,
        session: Any,
        session_dir: Path,
        initial_metadata: dict[str, Any] | None = None,
    ) -> None:
        self._session = session
        self._session_dir = session_dir
        self._initial_metadata = initial_metadata

    async def __call__(self, event: str, data: dict[str, Any]) -> Any:
        try:
            context = self._session.coordinator.get("context")
            if not context or not hasattr(context, "get_messages"):
                return HookResult(action="continue")

            messages = await context.get_messages()
            turn_count = sum(
                1 for m in messages if isinstance(m, dict) and m.get("role") == "user"
            )

            updates: dict[str, Any] = {
                "turn_count": turn_count,
                "last_updated": datetime.now(tz=UTC).isoformat(),
            }

            # Flush initial metadata on first fire (directory now exists
            # because TranscriptSaveHook runs at the same priority).
            if self._initial_metadata is not None:
                updates = {**self._initial_metadata, **updates}
                self._initial_metadata = None

            write_metadata(self._session_dir, updates)

            # Bridge: emit prompt:complete so hooks-session-naming fires.
            # Some orchestrators (e.g. loop-streaming) only emit
            # orchestrator:complete but not prompt:complete.  The naming
            # hook registers on prompt:complete and needs session_id in
            # the event data.
            session_id = getattr(self._session, "session_id", None)
            if session_id:
                await self._session.coordinator.hooks.emit(
                    "prompt:complete",
                    {**data, "session_id": session_id},
                )
        except Exception:  # noqa: BLE001
            logger.warning("Metadata save failed", exc_info=True)

        return HookResult(action="continue")


def register_metadata_hooks(
    session: Any,
    session_dir: Path,
    initial_metadata: dict[str, Any] | None = None,
) -> None:
    """Register metadata persistence hook on a session.

    *initial_metadata*, when provided, is flushed to metadata.json on
    the first ``orchestrator:complete`` event (alongside the transcript
    write that creates the session directory).

    Safe to call on both fresh and resumed sessions.
    Silently no-ops if hooks API is unavailable.
    """
    try:
        hook = MetadataSaveHook(session, session_dir, initial_metadata)
        hooks = session.coordinator.hooks
        hooks.register(
            event="orchestrator:complete",
            handler=hook,
            priority=_PRIORITY,
            name="bridge-metadata:orchestrator:complete",
        )
        logger.debug("Metadata hook registered -> %s", session_dir / METADATA_FILENAME)
    except Exception:  # noqa: BLE001
        logger.debug("Could not register metadata hooks", exc_info=True)
