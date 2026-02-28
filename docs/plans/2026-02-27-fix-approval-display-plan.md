# fix/approval-display Implementation Plan

> **For execution:** Use `/execute-plan` mode or the subagent-driven-development recipe.

**Goal:** Replace the `event_queue` parameter with a `SessionSurface` abstraction so all surfaces (web chat, Slack, voice, headless) can provide their own approval and display implementations.

**Architecture:** New `SessionSurface` dataclass + factory functions + `LogDisplaySystem` in `protocol_adapters.py`, replacing `_wire_event_queue` with `_attach_surface` in `session_backend.py`, updating the `SessionBackend` protocol and `MockBackend`, then updating all call sites in source and tests.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, pytest, uv

---

## Orientation

All work happens inside the worktree. Every file path below is relative to the repo root at  
`/Users/samule/repo/amplifier-distro-msft`.

**Working directory for all test runs:**
```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/ -v
```

**Baseline:** 945 tests passing. The suite must stay at or above 945 after every commit.

**Key source files:**
- `WORKTREE/src/amplifier_distro/server/protocol_adapters.py` — add `LogDisplaySystem`, `SessionSurface`, factory functions
- `WORKTREE/src/amplifier_distro/server/session_backend.py` — add `_attach_surface`, update `create_session` / `resume_session`, update `SessionBackend` protocol, update `MockBackend`
- `WORKTREE/src/amplifier_distro/server/apps/chat/connection.py` — update call sites (4 locations)
- `WORKTREE/src/amplifier_distro/server/apps/voice/connection.py` — update call sites (2 locations)

Where `WORKTREE` = `.worktrees/fix-approval-display/distro-server`.

**Key test files:**
- `WORKTREE/tests/test_protocol_adapters.py` — new tests for new classes/functions
- `WORKTREE/tests/test_session_backend.py` — existing tests need call-site updates
- `WORKTREE/tests/test_chat_approval.py` — existing tests need call-site updates
- `WORKTREE/tests/test_chat_backend_queue.py` — existing tests need call-site updates
- `WORKTREE/tests/test_chat_display_messages.py` — existing tests need call-site updates
- `WORKTREE/tests/test_chat_connection.py` — existing tests need call-site updates

---

## Task 1: Write failing test — `LogDisplaySystem.show_message` routes to logger

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/tests/test_protocol_adapters.py`

**Step 1: Open the existing test file and append a new test class at the bottom**

The file currently ends at line 184 (after `TestQueueDisplaySystem`). Add the following block **after** the last line of `TestQueueDisplaySystem`:

```python

# ── LogDisplaySystem ───────────────────────────────────────────────────────────


class TestLogDisplaySystem:
    @pytest.mark.asyncio
    async def test_show_message_info_routes_to_logger(self, caplog):
        """show_message with level='info' writes to amplifier_distro.display logger."""
        import logging

        from amplifier_distro.server.protocol_adapters import LogDisplaySystem

        display = LogDisplaySystem()
        with caplog.at_level(logging.INFO, logger="amplifier_distro.display"):
            await display.show_message("hello world", level="info", source="test-hook")

        assert "hello world" in caplog.text

    @pytest.mark.asyncio
    async def test_show_message_warning_routes_at_warning_level(self, caplog):
        import logging

        from amplifier_distro.server.protocol_adapters import LogDisplaySystem

        display = LogDisplaySystem()
        with caplog.at_level(logging.WARNING, logger="amplifier_distro.display"):
            await display.show_message("uh oh", level="warning", source="sys")

        assert "uh oh" in caplog.text

    @pytest.mark.asyncio
    async def test_show_message_error_routes_at_error_level(self, caplog):
        import logging

        from amplifier_distro.server.protocol_adapters import LogDisplaySystem

        display = LogDisplaySystem()
        with caplog.at_level(logging.ERROR, logger="amplifier_distro.display"):
            await display.show_message("kaboom", level="error", source="sys")

        assert "kaboom" in caplog.text

    def test_push_nesting_returns_self(self):
        from amplifier_distro.server.protocol_adapters import LogDisplaySystem

        display = LogDisplaySystem()
        assert display.push_nesting() is display

    def test_pop_nesting_returns_self(self):
        from amplifier_distro.server.protocol_adapters import LogDisplaySystem

        display = LogDisplaySystem()
        assert display.pop_nesting() is display

    def test_nesting_depth_is_zero(self):
        from amplifier_distro.server.protocol_adapters import LogDisplaySystem

        display = LogDisplaySystem()
        assert display.nesting_depth == 0
```

You also need to add `import pytest` at the top of the file if it's not already there. Check line 1-10 — if `import pytest` is missing, add it after `import asyncio`.

**Step 2: Run the new tests to verify they fail**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_protocol_adapters.py::TestLogDisplaySystem -v
```

Expected: `ImportError` or `AttributeError` — `LogDisplaySystem` does not exist yet.  
If you see any other error, stop and re-read the test code.

---

## Task 2: Implement `LogDisplaySystem` → verify passes → commit

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/src/amplifier_distro/server/protocol_adapters.py`

**Step 1: Read the current end of the file**

The file currently ends at line 122 with `return self._nesting_depth`. You will append after that.

**Step 2: Add the `LogDisplaySystem` class at the end of the file**

```python


class LogDisplaySystem:
    """Display system that routes hook messages to Python logger.

    Used by headless surfaces (Slack, scheduled jobs, tests) that have
    no connected client to receive display_message events.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("amplifier_distro.display")

    async def show_message(
        self,
        message: str,
        level: Literal["info", "warning", "error"] = "info",
        source: str = "hook",
    ) -> None:
        log_level = {
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
        }.get(level, logging.INFO)
        self._logger.log(log_level, "[%s] %s", source, message)

    def push_nesting(self) -> "LogDisplaySystem":
        return self

    def pop_nesting(self) -> "LogDisplaySystem":
        return self

    @property
    def nesting_depth(self) -> int:
        return 0
```

`Literal` and `logging` are already imported at the top of the file — do not add duplicate imports.

**Step 3: Run the new tests to verify they pass**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_protocol_adapters.py::TestLogDisplaySystem -v
```

Expected: 6 PASSED.

**Step 4: Run the full suite to confirm nothing broke**

```
uv run pytest tests/ -v
```

Expected: 945+ PASSED.

**Step 5: Commit**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display
git add distro-server/src/amplifier_distro/server/protocol_adapters.py \
        distro-server/tests/test_protocol_adapters.py
git commit -m "feat(protocol_adapters): add LogDisplaySystem for headless surfaces"
```

---

## Task 3: Write failing test — `SessionSurface` dataclass has correct fields

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/tests/test_protocol_adapters.py`

**Step 1: Append a new test class after `TestLogDisplaySystem`**

```python

# ── SessionSurface dataclass ───────────────────────────────────────────────────


class TestSessionSurface:
    def test_session_surface_defaults_all_none(self):
        """SessionSurface() creates an instance with all fields None."""
        from amplifier_distro.server.protocol_adapters import SessionSurface

        surface = SessionSurface()
        assert surface.event_queue is None
        assert surface.approval_system is None
        assert surface.display_system is None
        assert surface.on_bundle_reload is None

    def test_session_surface_accepts_queue(self):
        import asyncio

        from amplifier_distro.server.protocol_adapters import SessionSurface

        q: asyncio.Queue = asyncio.Queue()
        surface = SessionSurface(event_queue=q)
        assert surface.event_queue is q

    def test_session_surface_accepts_all_fields(self):
        """SessionSurface can be constructed with arbitrary values for each field."""
        import asyncio

        from amplifier_distro.server.protocol_adapters import SessionSurface

        q: asyncio.Queue = asyncio.Queue()
        sentinel_approval = object()
        sentinel_display = object()
        sentinel_reload = object()

        surface = SessionSurface(
            event_queue=q,
            approval_system=sentinel_approval,
            display_system=sentinel_display,
            on_bundle_reload=sentinel_reload,
        )
        assert surface.event_queue is q
        assert surface.approval_system is sentinel_approval
        assert surface.display_system is sentinel_display
        assert surface.on_bundle_reload is sentinel_reload
```

**Step 2: Run to verify failure**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_protocol_adapters.py::TestSessionSurface -v
```

Expected: `ImportError` — `SessionSurface` does not exist yet.

---

## Task 4: Implement `SessionSurface` dataclass → verify passes → commit

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/src/amplifier_distro/server/protocol_adapters.py`

**Step 1: Add imports at the top of `protocol_adapters.py`**

The file currently starts with:
```python
from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable
from typing import Any, Literal
```

You need to add `Awaitable` to the `collections.abc` import and `dataclass` / `field` to a new import. Change these two lines:

```python
from collections.abc import Callable
```
→
```python
from collections.abc import Awaitable, Callable
```

And add after the existing imports (before `logger = ...`):
```python
from dataclasses import dataclass, field
```

**Step 2: Add `SessionSurface` dataclass immediately before `ApprovalSystem`**

Insert this block between the imports block and `class ApprovalSystem:` (i.e., after line 17 `logger = logging.getLogger(__name__)`):

```python


@dataclass
class SessionSurface:
    """Encapsulates all surface concerns for a session.

    A surface is the thing on the other end of the session:
    - web_chat_surface: browser client connected via WebSocket
    - headless_surface: no client (Slack messages, recipes, scheduled jobs)

    Factory functions (web_chat_surface, headless_surface) build correctly
    wired surfaces so callers never need to construct this directly.
    """

    event_queue: asyncio.Queue | None = None  # type: ignore[type-arg]
    approval_system: Any | None = None
    display_system: Any | None = None
    on_bundle_reload: Callable[[], Awaitable[None]] | None = None
```

**Step 3: Run the new tests to verify they pass**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_protocol_adapters.py::TestSessionSurface -v
```

Expected: 3 PASSED.

**Step 4: Run the full suite**

```
uv run pytest tests/ -v
```

Expected: 945+ PASSED.

**Step 5: Commit**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display
git add distro-server/src/amplifier_distro/server/protocol_adapters.py \
        distro-server/tests/test_protocol_adapters.py
git commit -m "feat(protocol_adapters): add SessionSurface dataclass"
```

---

## Task 5: Write failing test — `headless_surface()` returns surface with auto-approve and `LogDisplaySystem`

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/tests/test_protocol_adapters.py`

**Step 1: Append a new test class**

```python

# ── headless_surface factory ───────────────────────────────────────────────────


class TestHeadlessSurface:
    def test_headless_surface_returns_session_surface(self):
        from amplifier_distro.server.protocol_adapters import (
            SessionSurface,
            headless_surface,
        )

        surface = headless_surface()
        assert isinstance(surface, SessionSurface)

    def test_headless_surface_event_queue_is_none(self):
        from amplifier_distro.server.protocol_adapters import headless_surface

        surface = headless_surface()
        assert surface.event_queue is None

    def test_headless_surface_has_auto_approve_approval_system(self):
        from amplifier_distro.server.protocol_adapters import (
            ApprovalSystem,
            headless_surface,
        )

        surface = headless_surface()
        assert isinstance(surface.approval_system, ApprovalSystem)
        assert surface.approval_system._auto_approve is True

    def test_headless_surface_has_log_display_system(self):
        from amplifier_distro.server.protocol_adapters import (
            LogDisplaySystem,
            headless_surface,
        )

        surface = headless_surface()
        assert isinstance(surface.display_system, LogDisplaySystem)

    def test_headless_surface_on_bundle_reload_is_none(self):
        from amplifier_distro.server.protocol_adapters import headless_surface

        surface = headless_surface()
        assert surface.on_bundle_reload is None
```

**Step 2: Run to verify failure**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_protocol_adapters.py::TestHeadlessSurface -v
```

Expected: `ImportError` — `headless_surface` does not exist yet.

---

## Task 6: Implement `headless_surface()` → verify passes → commit

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/src/amplifier_distro/server/protocol_adapters.py`

**Step 1: Append the factory function at the end of the file** (after `LogDisplaySystem`)

```python


def headless_surface() -> SessionSurface:
    """Surface for sessions with no connected client.

    Used for Slack messages, recipes, scheduled jobs, and any other
    context where there is no browser WebSocket to receive events.

    Approval auto-approves (never blocks). Display routes to logger.
    """
    return SessionSurface(
        approval_system=ApprovalSystem(auto_approve=True),
        display_system=LogDisplaySystem(),
    )
```

**Step 2: Run the new tests to verify they pass**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_protocol_adapters.py::TestHeadlessSurface -v
```

Expected: 5 PASSED.

**Step 3: Run the full suite**

```
uv run pytest tests/ -v
```

Expected: 945+ PASSED.

**Step 4: Commit**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display
git add distro-server/src/amplifier_distro/server/protocol_adapters.py \
        distro-server/tests/test_protocol_adapters.py
git commit -m "feat(protocol_adapters): add headless_surface() factory"
```

---

## Task 7: Write failing test — `web_chat_surface(queue)` returns surface with queue-backed approval/display

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/tests/test_protocol_adapters.py`

**Step 1: Append a new test class**

```python

# ── web_chat_surface factory ───────────────────────────────────────────────────


class TestWebChatSurface:
    def test_web_chat_surface_returns_session_surface(self):
        import asyncio

        from amplifier_distro.server.protocol_adapters import (
            SessionSurface,
            web_chat_surface,
        )

        q: asyncio.Queue = asyncio.Queue()
        surface = web_chat_surface(q)
        assert isinstance(surface, SessionSurface)

    def test_web_chat_surface_stores_queue(self):
        import asyncio

        from amplifier_distro.server.protocol_adapters import web_chat_surface

        q: asyncio.Queue = asyncio.Queue()
        surface = web_chat_surface(q)
        assert surface.event_queue is q

    def test_web_chat_surface_has_interactive_approval_system(self):
        import asyncio

        from amplifier_distro.server.protocol_adapters import (
            ApprovalSystem,
            web_chat_surface,
        )

        q: asyncio.Queue = asyncio.Queue()
        surface = web_chat_surface(q)
        assert isinstance(surface.approval_system, ApprovalSystem)
        # Must NOT auto-approve — it must wait for user input
        assert surface.approval_system._auto_approve is False

    def test_web_chat_surface_has_queue_display_system(self):
        import asyncio

        from amplifier_distro.server.protocol_adapters import (
            QueueDisplaySystem,
            web_chat_surface,
        )

        q: asyncio.Queue = asyncio.Queue()
        surface = web_chat_surface(q)
        assert isinstance(surface.display_system, QueueDisplaySystem)

    @pytest.mark.asyncio
    async def test_web_chat_surface_approval_request_pushes_to_queue(self):
        """Approval requests from the surface land on the event queue."""
        import asyncio

        from amplifier_distro.server.protocol_adapters import web_chat_surface

        q: asyncio.Queue = asyncio.Queue()
        surface = web_chat_surface(q)

        # Trigger the approval callback directly (simulates coordinator call)
        on_req = surface.approval_system._on_approval_request
        on_req(
            "req-001",
            "Allow tool?",
            ["allow", "deny"],
            300.0,
            "deny",
        )

        event_name, data = q.get_nowait()
        assert event_name == "approval_request"
        assert data["request_id"] == "req-001"
        assert data["prompt"] == "Allow tool?"

    @pytest.mark.asyncio
    async def test_web_chat_surface_display_pushes_to_queue(self):
        """Display messages from the surface land on the event queue."""
        import asyncio

        from amplifier_distro.server.protocol_adapters import web_chat_surface

        q: asyncio.Queue = asyncio.Queue()
        surface = web_chat_surface(q)

        await surface.display_system.show_message(
            "Thinking…", level="info", source="hook"
        )

        event_name, data = q.get_nowait()
        assert event_name == "display_message"
        assert data["message"] == "Thinking…"
```

**Step 2: Run to verify failure**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_protocol_adapters.py::TestWebChatSurface -v
```

Expected: `ImportError` — `web_chat_surface` does not exist yet.

---

## Task 8: Implement `web_chat_surface()` → verify passes → commit

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/src/amplifier_distro/server/protocol_adapters.py`

**Step 1: Append the factory function at the end of the file** (after `headless_surface`)

```python


def web_chat_surface(queue: asyncio.Queue) -> SessionSurface:  # type: ignore[type-arg]
    """Surface for WebSocket-connected browser clients.

    Wires approval requests and display messages through the event queue
    so the browser receives them as streaming events.
    """

    def _on_approval_request(
        request_id: str,
        prompt: str,
        options: list[str],
        timeout: float,
        default: str,
    ) -> None:
        try:
            queue.put_nowait(
                (
                    "approval_request",
                    {
                        "request_id": request_id,
                        "prompt": prompt,
                        "options": options,
                        "timeout": timeout,
                        "default": default,
                    },
                )
            )
        except asyncio.QueueFull:
            logger.warning("Event queue full, dropping approval_request")

    return SessionSurface(
        event_queue=queue,
        approval_system=ApprovalSystem(
            on_approval_request=_on_approval_request,
            auto_approve=False,
        ),
        display_system=QueueDisplaySystem(queue),
    )
```

**Step 2: Run the new tests to verify they pass**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_protocol_adapters.py::TestWebChatSurface -v
```

Expected: 6 PASSED.

**Step 3: Run the full suite**

```
uv run pytest tests/ -v
```

Expected: 945+ PASSED.

**Step 4: Commit**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display
git add distro-server/src/amplifier_distro/server/protocol_adapters.py \
        distro-server/tests/test_protocol_adapters.py
git commit -m "feat(protocol_adapters): add web_chat_surface() factory"
```

---

## Task 9: Write failing test — `create_session(surface=None)` uses headless defaults

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/tests/test_session_backend.py`

**Step 1: Read the end of `test_session_backend.py` to find the right insertion point**

The file has 954 lines. Scroll to the bottom. The last test class is near line 940+. You will append a new test class after the last existing test.

**Step 2: Append this new test class at the very end of the file**

```python


# ── SessionSurface integration ─────────────────────────────────────────────────


class TestCreateSessionSurface:
    """create_session() accepts surface= parameter instead of event_queue=."""

    async def test_create_session_accepts_surface_parameter(self, bridge_backend):
        """create_session(surface=...) is accepted without TypeError."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from amplifier_distro.server.protocol_adapters import headless_surface

        mock_session = MagicMock()
        mock_session.session_id = "sess-surface-001"
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.hooks = MagicMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        bridge_backend._load_bundle = AsyncMock(return_value=mock_prepared)

        from amplifier_distro.server.session_backend import FoundationBackend

        with patch("asyncio.create_task"):
            with patch(
                "amplifier_distro.server.session_backend.register_transcript_hooks"
            ):
                with patch(
                    "amplifier_distro.server.session_backend.register_metadata_hooks"
                ):
                    with patch(
                        "amplifier_distro.server.session_backend.register_spawning"
                    ):
                        # Must not raise TypeError about unexpected keyword argument
                        info = await FoundationBackend.create_session(
                            bridge_backend,
                            working_dir="/tmp",
                            surface=headless_surface(),
                        )
        assert info.session_id == "sess-surface-001"

    async def test_create_session_surface_none_uses_headless_defaults(
        self, bridge_backend
    ):
        """create_session(surface=None) auto-wires headless surface — no errors."""
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_session = MagicMock()
        mock_session.session_id = "sess-headless-001"
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.hooks = MagicMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        bridge_backend._load_bundle = AsyncMock(return_value=mock_prepared)

        from amplifier_distro.server.session_backend import FoundationBackend

        with patch("asyncio.create_task"):
            with patch(
                "amplifier_distro.server.session_backend.register_transcript_hooks"
            ):
                with patch(
                    "amplifier_distro.server.session_backend.register_metadata_hooks"
                ):
                    with patch(
                        "amplifier_distro.server.session_backend.register_spawning"
                    ):
                        # surface=None should resolve to headless_surface() internally
                        info = await FoundationBackend.create_session(
                            bridge_backend,
                            working_dir="/tmp",
                            surface=None,
                        )
        assert info.session_id == "sess-headless-001"
        # Approval system should have been stored (headless auto-approves)
        assert "sess-headless-001" in bridge_backend._approval_systems

    async def test_create_session_with_web_chat_surface_stores_approval(
        self, bridge_backend
    ):
        """create_session(surface=web_chat_surface(q)) wires approval into _approval_systems."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from amplifier_distro.server.protocol_adapters import web_chat_surface

        q: _asyncio.Queue = _asyncio.Queue()
        mock_session = MagicMock()
        mock_session.session_id = "sess-webchat-001"
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.hooks = MagicMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        bridge_backend._load_bundle = AsyncMock(return_value=mock_prepared)

        from amplifier_distro.server.session_backend import FoundationBackend

        with patch("asyncio.create_task"):
            with patch(
                "amplifier_distro.server.session_backend.register_transcript_hooks"
            ):
                with patch(
                    "amplifier_distro.server.session_backend.register_metadata_hooks"
                ):
                    with patch(
                        "amplifier_distro.server.session_backend.register_spawning"
                    ):
                        await FoundationBackend.create_session(
                            bridge_backend,
                            working_dir="/tmp",
                            surface=web_chat_surface(q),
                        )

        assert "sess-webchat-001" in bridge_backend._approval_systems
```

**Step 3: Run to verify failure**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_session_backend.py::TestCreateSessionSurface -v
```

Expected: `TypeError` — `create_session()` does not accept `surface=` yet.

---

## Task 10: Update `create_session`, add `_attach_surface`, update `SessionBackend` protocol → verify passes → commit

This is the largest single task. Read each step carefully before starting.

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/src/amplifier_distro/server/session_backend.py`

### Step 1: Add `SessionSurface` import at the top of `session_backend.py`

Find the existing imports near the top of `session_backend.py`. Locate the line:

```python
from amplifier_distro.server.spawn_registration import register_spawning
```

**After** that line, add:

```python
from amplifier_distro.server.protocol_adapters import SessionSurface
```

> **Warning:** Do NOT add a circular import. `protocol_adapters.py` does not import from `session_backend.py`, so this is safe. But `headless_surface` and `web_chat_surface` are imported lazily inside methods to avoid the import happening at module load time (when foundation may not be installed). Use the pattern you see in `_load_bundle()` — lazy import at call time.

Actually, importing `SessionSurface` at the top is fine since it's a plain dataclass with no side effects. The lazy imports are only needed for heavy foundation objects.

### Step 2: Add the `_attach_surface` method to `FoundationBackend`

This method replaces `_wire_event_queue`. Add it directly after `_wire_event_queue` (which ends around line 499). **Do not delete `_wire_event_queue` yet** — you will delete it in a later step after everything works.

```python
    def _attach_surface(
        self, session: Any, session_id: str, surface: SessionSurface
    ) -> None:
        """Attach surface capabilities (streaming, display, approval) to a session.

        Replaces _wire_event_queue. Accepts a SessionSurface instead of a raw
        asyncio.Queue, delegating approval and display to the surface's own
        implementations.

        Guards against double hook registration on page refresh / resume:
        streaming hooks are only registered once per session; subsequent calls
        update only the approval and display systems.
        """
        coordinator = session.coordinator

        # ── Streaming hooks (only when surface carries an event queue) ──────
        if surface.event_queue is not None:
            _q = surface.event_queue

            if session_id in self._wired_sessions:
                # Already wired — update approval/display only (new queue connection).
                # Don't re-register hooks.
                if surface.approval_system is not None and hasattr(coordinator, "set"):
                    coordinator.set("approval", surface.approval_system)
                if surface.approval_system is not None:
                    self._approval_systems[session_id] = surface.approval_system
                if surface.display_system is not None and hasattr(coordinator, "set"):
                    coordinator.set("display", surface.display_system)
                return

            self._wired_sessions.add(session_id)

            from amplifier_core.events import ALL_EVENTS
            from amplifier_core.models import HookResult

            async def on_stream(event: str, data: dict) -> HookResult:
                try:
                    _q.put_nowait((event, data))
                except asyncio.QueueFull:
                    logger.warning("Event queue full, dropping event: %s", event)
                return HookResult(action="continue", data=data)

            hooks = coordinator.hooks

            registered = 0
            failed_evts = []
            for evt in ALL_EVENTS:
                try:
                    hooks.register(evt, on_stream)
                    registered += 1
                except Exception as exc:  # noqa: BLE001
                    failed_evts.append((evt, exc))

            # Delegate events are not in ALL_EVENTS — register explicitly
            for evt in [
                "delegate:agent_spawned",
                "delegate:agent_resumed",
                "delegate:agent_completed",
                "delegate:error",
            ]:
                try:
                    hooks.register(evt, on_stream)
                    registered += 1
                except Exception as exc:  # noqa: BLE001
                    failed_evts.append((evt, exc))

            # Bridge hook — re-emit orchestrator:complete as prompt:complete.
            async def on_orchestrator_complete(event: str, data: dict) -> HookResult:
                await coordinator.hooks.emit(
                    "prompt:complete", {**data, "session_id": session_id}
                )
                return HookResult(action="continue", data=data)

            try:
                hooks.register(
                    "orchestrator:complete", on_orchestrator_complete, priority=50
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to register prompt:complete bridge: %s", exc)

            logger.info(
                "Event hook wiring: %d registered, %d failed for session %s",
                registered,
                len(failed_evts),
                session_id,
            )
            if failed_evts:
                for evt, exc in failed_evts:
                    logger.warning("  hook registration failed [%s]: %s", evt, exc)

        # ── Display system ────────────────────────────────────────────────────
        if surface.display_system is not None and hasattr(coordinator, "set"):
            coordinator.set("display", surface.display_system)

        # ── Approval system ───────────────────────────────────────────────────
        if surface.approval_system is not None:
            if hasattr(coordinator, "set"):
                coordinator.set("approval", surface.approval_system)
            self._approval_systems[session_id] = surface.approval_system
```

### Step 3: Update `create_session` signature and body

Locate the `create_session` method in `FoundationBackend` (starts around line 501). Change the signature from:

```python
    async def create_session(
        self,
        working_dir: str = "~",
        bundle_name: str | None = None,
        description: str = "",
        event_queue: asyncio.Queue | None = None,
    ) -> SessionInfo:
```

to:

```python
    async def create_session(
        self,
        working_dir: str = "~",
        bundle_name: str | None = None,
        description: str = "",
        surface: SessionSurface | None = None,
    ) -> SessionInfo:
```

Then locate this block in the body (around line 554-557):

```python
        # Wire streaming/display/approval when event_queue provided
        if event_queue is not None:
            self._wire_event_queue(session, session_id, event_queue)
```

Replace it with:

```python
        # Resolve surface — default to headless if none provided
        from amplifier_distro.server.protocol_adapters import headless_surface

        _surface = surface if surface is not None else headless_surface()
        self._attach_surface(session, session_id, _surface)
```

### Step 4: Update `SessionBackend` protocol

Locate the `SessionBackend` protocol class (starts around line 96). Find `create_session` in the protocol:

```python
    async def create_session(
        self,
        working_dir: str = "~",
        bundle_name: str | None = None,
        description: str = "",
        event_queue: asyncio.Queue | None = None,
    ) -> SessionInfo:
```

Change to:

```python
    async def create_session(
        self,
        working_dir: str = "~",
        bundle_name: str | None = None,
        description: str = "",
        surface: SessionSurface | None = None,
    ) -> SessionInfo:
```

### Step 5: Update `MockBackend.create_session` signature

Locate `MockBackend.create_session` (around line 179). Change:

```python
    async def create_session(
        self,
        working_dir: str = "~",
        bundle_name: str | None = None,
        description: str = "",
        event_queue: Any = None,
    ) -> SessionInfo:
```

to:

```python
    async def create_session(
        self,
        working_dir: str = "~",
        bundle_name: str | None = None,
        description: str = "",
        surface: Any = None,
    ) -> SessionInfo:
```

### Step 6: Run the new tests to verify they pass

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_session_backend.py::TestCreateSessionSurface -v
```

Expected: 3 PASSED.

### Step 7: Run the full suite and check for breakage

```
uv run pytest tests/ -v 2>&1 | tail -30
```

You will see failures in tests that still pass `event_queue=`. **That is expected and correct** — those call sites haven't been updated yet. Count the failures: you should see failures only in these test files:
- `test_session_backend.py` (2 tests with `event_queue=`)
- `test_chat_display_messages.py` (1 test)
- `test_chat_connection.py` (1 test)
- `test_chat_approval.py` (2 tests)
- `test_chat_backend_queue.py` (2 tests)

If you see failures in OTHER files, stop and investigate before continuing.

### Step 8: Commit (with known failing tests — we fix them in Task 12)

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display
git add distro-server/src/amplifier_distro/server/session_backend.py \
        distro-server/tests/test_session_backend.py
git commit -m "feat(session_backend): add _attach_surface, update create_session to surface= API"
```

---

## Task 11: Update `resume_session` → write test → verify → commit

**Files:**
- Modify: `.worktrees/fix-approval-display/distro-server/src/amplifier_distro/server/session_backend.py`
- Modify: `.worktrees/fix-approval-display/distro-server/tests/test_session_backend.py`

### Step 1: Write the failing test first

Append to the **end** of `test_session_backend.py`:

```python


class TestResumeSessionSurface:
    """resume_session() accepts surface= parameter instead of event_queue=."""

    async def test_resume_session_accepts_surface_parameter(self, bridge_backend):
        """resume_session(surface=...) does not raise TypeError."""
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from amplifier_distro.server.protocol_adapters import web_chat_surface

        q: _asyncio.Queue = _asyncio.Queue()
        mock_session = MagicMock()
        mock_session.session_id = "sess-resume-surface-001"
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.hooks = MagicMock()

        mock_handle = MagicMock()
        mock_handle.session = mock_session
        bridge_backend._sessions["sess-resume-surface-001"] = mock_handle

        from amplifier_distro.server.session_backend import FoundationBackend

        with patch("asyncio.create_task"):
            # Must not raise TypeError
            await FoundationBackend.resume_session(
                bridge_backend,
                "sess-resume-surface-001",
                "/tmp",
                surface=web_chat_surface(q),
            )

        assert "sess-resume-surface-001" in bridge_backend._approval_systems

    async def test_resume_session_surface_none_does_not_error(self, bridge_backend):
        """resume_session(surface=None) is a no-op — no approval wiring."""
        from unittest.mock import MagicMock

        mock_handle = MagicMock()
        bridge_backend._sessions["sess-noop-001"] = mock_handle

        from amplifier_distro.server.session_backend import FoundationBackend

        # surface=None → skip reconnect and wiring, no error
        await FoundationBackend.resume_session(
            bridge_backend, "sess-noop-001", "/tmp", surface=None
        )
        # No approval system stored for this session
        assert "sess-noop-001" not in bridge_backend._approval_systems
```

**Step 2: Run to verify failure**

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_session_backend.py::TestResumeSessionSurface -v
```

Expected: `TypeError` — `resume_session()` still has `event_queue` parameter.

### Step 3: Update `SessionBackend` protocol `resume_session`

In `session_backend.py`, find the `resume_session` in the `SessionBackend` protocol:

```python
    async def resume_session(
        self,
        session_id: str,
        working_dir: str,
        event_queue: asyncio.Queue | None = None,
    ) -> None:
```

Change to:

```python
    async def resume_session(
        self,
        session_id: str,
        working_dir: str,
        surface: SessionSurface | None = None,
    ) -> None:
```

### Step 4: Update `MockBackend.resume_session`

Find `MockBackend.resume_session` (around line 251):

```python
    async def resume_session(
        self, session_id: str, working_dir: str, event_queue: Any = None
    ) -> None:
```

Change to:

```python
    async def resume_session(
        self, session_id: str, working_dir: str, surface: Any = None
    ) -> None:
```

### Step 5: Update `FoundationBackend.resume_session`

Find `FoundationBackend.resume_session` (around line 918). The full current body is:

```python
    async def resume_session(
        self,
        session_id: str,
        working_dir: str,
        event_queue: asyncio.Queue | None = None,
    ) -> None:
        """Restore the LLM context for a session after a server restart."""
        if event_queue is not None:
            self._ended_sessions.discard(session_id)

        if self._sessions.get(session_id) is None:
            await self._reconnect(session_id, working_dir=working_dir)

        if event_queue is not None:
            handle = self._sessions.get(session_id)
            if handle is not None:
                self._wire_event_queue(handle.session, session_id, event_queue)

            # Ensure worker queue + task exist after resume
            if session_id not in self._session_queues:
                self._session_queues[session_id] = asyncio.Queue()
            if (
                session_id not in self._worker_tasks
                or self._worker_tasks[session_id].done()
            ):
                self._worker_tasks[session_id] = asyncio.create_task(
                    self._session_worker(session_id)
                )
```

Replace the **entire method** with:

```python
    async def resume_session(
        self,
        session_id: str,
        working_dir: str,
        surface: SessionSurface | None = None,
    ) -> None:
        """Restore the LLM context for a session after a server restart."""
        if surface is not None:
            self._ended_sessions.discard(session_id)

        if self._sessions.get(session_id) is None:
            await self._reconnect(session_id, working_dir=working_dir)

        if surface is not None:
            handle = self._sessions.get(session_id)
            if handle is not None:
                self._attach_surface(handle.session, session_id, surface)

            # Ensure worker queue + task exist after resume
            if session_id not in self._session_queues:
                self._session_queues[session_id] = asyncio.Queue()
            if (
                session_id not in self._worker_tasks
                or self._worker_tasks[session_id].done()
            ):
                self._worker_tasks[session_id] = asyncio.create_task(
                    self._session_worker(session_id)
                )
```

### Step 6: Run the new tests to verify they pass

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/test_session_backend.py::TestResumeSessionSurface -v
```

Expected: 2 PASSED.

### Step 7: Commit

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display
git add distro-server/src/amplifier_distro/server/session_backend.py \
        distro-server/tests/test_session_backend.py
git commit -m "feat(session_backend): update resume_session to surface= API"
```

---

## Task 12: Update all call sites → run full suite → commit

This task updates every place in the codebase that still passes `event_queue=` to `create_session` or `resume_session`. You must update both source files and test files. Do source files first, then tests.

All paths below are inside `.worktrees/fix-approval-display/distro-server/`.

---

### 12a — Update `apps/chat/connection.py`

**File:** `src/amplifier_distro/server/apps/chat/connection.py`

**Step 1: Find the import block at the top of this file** and add:

```python
from amplifier_distro.server.protocol_adapters import web_chat_surface
```

Add it near the other `amplifier_distro` imports.

**Step 2: Apply the 4 substitutions below.**

Each substitution is exact — use your editor's find-and-replace on the exact strings shown.

**Substitution A** (line ~240, in the `resume_session` call):

Find:
```python
                    event_queue=self.event_queue,
                )
                info = await self._backend.get_session_info(str(resume_session_id))
```

Replace with:
```python
                    surface=web_chat_surface(self.event_queue),
                )
                info = await self._backend.get_session_info(str(resume_session_id))
```

**Substitution B** (line ~255, in `create_session` with bundle):

Find:
```python
                    bundle_name=bundle,
                    event_queue=self.event_queue,
                )
                session_id = info.session_id
                session_cwd = str(info.working_dir)
```

Replace with:
```python
                    bundle_name=bundle,
                    surface=web_chat_surface(self.event_queue),
                )
                session_id = info.session_id
                session_cwd = str(info.working_dir)
```

**Substitution C** (line ~335, in the bundle-change `create_session`):

Find:
```python
                    bundle_name=new_bundle,
                    event_queue=self.event_queue,
                )
                self._session_id = info.session_id
```

Replace with:
```python
                    bundle_name=new_bundle,
                    surface=web_chat_surface(self.event_queue),
                )
                self._session_id = info.session_id
```

**Substitution D** (line ~348, in the cwd-change `create_session`):

Find:
```python
                    working_dir=new_cwd,
                    event_queue=self.event_queue,
                )
                self._session_id = info.session_id
```

Replace with:
```python
                    working_dir=new_cwd,
                    surface=web_chat_surface(self.event_queue),
                )
                self._session_id = info.session_id
```

**Step 3: Verify no `event_queue=self.event_queue` remains in this file**

```
grep -n "event_queue=self.event_queue" src/amplifier_distro/server/apps/chat/connection.py
```

Expected: no output (zero matches).

---

### 12b — Update `apps/voice/connection.py`

**File:** `src/amplifier_distro/server/apps/voice/connection.py`

**Step 1: Find the import block** and add:

```python
from amplifier_distro.server.protocol_adapters import web_chat_surface
```

**Step 2: Apply the 2 substitutions.**

**Substitution A** (line ~75, in `start()` method):

Find:
```python
            description="voice",
            working_dir=workspace_root,
            event_queue=self._event_queue,
        )
```

Replace with:
```python
            description="voice",
            working_dir=workspace_root,
            surface=web_chat_surface(self._event_queue),
        )
```

**Substitution B** (line ~101, in the `create_session` helper method that passes `**kwargs`):

Find:
```python
        return await self._backend.create_session(
            event_queue=self._event_queue,
            **kwargs,
        )
```

Replace with:
```python
        return await self._backend.create_session(
            surface=web_chat_surface(self._event_queue),
            **kwargs,
        )
```

**Step 3: Verify no `event_queue=self._event_queue` remains in backend calls**

```
grep -n "event_queue=self._event_queue" src/amplifier_distro/server/apps/voice/connection.py
```

You should still see 1 match — the one inside `EventStreamingHook(event_queue=self._event_queue)` on line ~68. **That one stays unchanged** — it is a constructor argument for a hook object, not a backend API call.

---

### 12c — Update test files

For each test file, make the substitutions shown. The pattern for all test changes is the same: replace `event_queue=<queue_var>` with `surface=web_chat_surface(<queue_var>)`, and add the import for `web_chat_surface`.

---

**File:** `tests/test_session_backend.py`

Add this import near the top (after existing imports):
```python
from amplifier_distro.server.protocol_adapters import web_chat_surface
```

**Change 1** (line ~702):

Find:
```python
            bridge_backend, working_dir="/tmp", event_queue=event_queue
```
Replace with:
```python
            bridge_backend, working_dir="/tmp", surface=web_chat_surface(event_queue)
```

**Change 2** (line ~924):

Find:
```python
        info = await backend.create_session(working_dir="~", event_queue=q)
```
Replace with:
```python
        info = await backend.create_session(working_dir="~", surface=web_chat_surface(q))
```

**Change 3** (line ~953):

Find:
```python
        await backend.resume_session("s", "~", event_queue=q)
```
Replace with:
```python
        await backend.resume_session("s", "~", surface=web_chat_surface(q))
```

---

**File:** `tests/test_chat_display_messages.py`

Add import:
```python
from amplifier_distro.server.protocol_adapters import web_chat_surface
```

**Change 1** (line ~60):

Find:
```python
            await backend.create_session(working_dir="~", event_queue=q)
```
Replace with:
```python
            await backend.create_session(working_dir="~", surface=web_chat_surface(q))
```

---

**File:** `tests/test_chat_connection.py`

Add import:
```python
from amplifier_distro.server.protocol_adapters import web_chat_surface
```

**Change 1** (line ~169):

Find:
```python
            event_queue=conn.event_queue,
```
Replace with:
```python
            surface=web_chat_surface(conn.event_queue),
```

---

**File:** `tests/test_chat_approval.py`

Add import:
```python
from amplifier_distro.server.protocol_adapters import web_chat_surface
```

**Change 1** (line ~203):

Find:
```python
        await backend.create_session(working_dir="~", event_queue=event_queue)
```
Replace with:
```python
        await backend.create_session(working_dir="~", surface=web_chat_surface(event_queue))
```

**Change 2** (line ~272):

Find:
```python
        await backend.create_session(working_dir="~", event_queue=event_queue)
```
Replace with:
```python
        await backend.create_session(working_dir="~", surface=web_chat_surface(event_queue))
```

---

**File:** `tests/test_chat_backend_queue.py`

Add import:
```python
from amplifier_distro.server.protocol_adapters import web_chat_surface
```

**Change 1** (line ~20):

Find:
```python
        info = await backend.create_session(working_dir="~", event_queue=q)
```
Replace with:
```python
        info = await backend.create_session(working_dir="~", surface=web_chat_surface(q))
```

**Change 2** (line ~63):

Find:
```python
            await bare_backend.create_session(working_dir="~", event_queue=q)
```
Replace with:
```python
            await bare_backend.create_session(working_dir="~", surface=web_chat_surface(q))
```

---

### 12d — Verify no remaining `event_queue=` call sites

After all edits, run this grep to confirm zero backend call sites remain:

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
grep -rn "event_queue=" src/ tests/ \
  | grep -v "EventStreamingHook" \
  | grep -v "# "
```

Expected: zero lines. If you see any, fix them before continuing.

The only legitimate `event_queue=` remaining in the file tree is:
- `src/amplifier_distro/server/apps/voice/connection.py` — `EventStreamingHook(event_queue=...)` constructor (not a backend call)
- Any variable declarations like `self._event_queue = asyncio.Queue(...)` (not a backend call)

---

### 12e — Run the full suite

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/ -v 2>&1 | tail -20
```

Expected: **945+ PASSED, 0 FAILED**.

If any tests fail, read the failure message carefully. The most common causes:
1. You missed an import — add `from amplifier_distro.server.protocol_adapters import web_chat_surface`
2. You changed the wrong `event_queue=` line — check the diff with `git diff`

---

### 12f — Remove `_wire_event_queue` (now dead code)

Now that `_attach_surface` handles everything, `_wire_event_queue` is unreachable. Delete it.

In `session_backend.py`, find and delete the entire `_wire_event_queue` method (from the `def _wire_event_queue(` line through its closing line, approximately lines 342–499).

After deleting, run the suite one more time to confirm nothing depended on it:

```
uv run pytest tests/ -v 2>&1 | tail -10
```

Expected: 945+ PASSED.

---

### 12g — Commit

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display
git add -A
git commit -m "refactor: replace event_queue= with surface= at all call sites

- Update chat/connection.py (4 sites): resume_session + 3x create_session
- Update voice/connection.py (2 sites): start() and create_session helper
- Update 6 test files to use web_chat_surface(queue)
- Remove dead _wire_event_queue method (replaced by _attach_surface)"
```

---

## Final Verification

Run the complete suite one last time from the worktree:

```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-approval-display/distro-server
uv run pytest tests/ -v
```

**Success criteria:**
- [ ] 945+ tests passing
- [ ] 0 tests failing
- [ ] `grep -rn "event_queue=" src/ tests/ | grep -v EventStreamingHook | grep -v "#"` → zero lines
- [ ] `LogDisplaySystem`, `SessionSurface`, `headless_surface`, `web_chat_surface` all importable from `amplifier_distro.server.protocol_adapters`
- [ ] `create_session` and `resume_session` accept `surface=` on both `SessionBackend` protocol, `MockBackend`, and `FoundationBackend`
- [ ] `_wire_event_queue` is gone

---

## Appendix: Complete file change summary

| File | Action | What changed |
|------|--------|-------------|
| `server/protocol_adapters.py` | Modified | Added `SessionSurface`, `LogDisplaySystem`, `headless_surface()`, `web_chat_surface()` |
| `server/session_backend.py` | Modified | Added `_attach_surface()`, updated `create_session`/`resume_session` signatures, updated `SessionBackend` protocol and `MockBackend`, removed `_wire_event_queue` |
| `apps/chat/connection.py` | Modified | 4 call sites: `event_queue=` → `surface=web_chat_surface(...)` |
| `apps/voice/connection.py` | Modified | 2 call sites: `event_queue=` → `surface=web_chat_surface(...)` |
| `tests/test_protocol_adapters.py` | Modified | New test classes for `LogDisplaySystem`, `SessionSurface`, `headless_surface`, `web_chat_surface` |
| `tests/test_session_backend.py` | Modified | New `TestCreateSessionSurface`, `TestResumeSessionSurface`; updated 3 existing call sites |
| `tests/test_chat_approval.py` | Modified | 2 call sites updated |
| `tests/test_chat_backend_queue.py` | Modified | 2 call sites updated |
| `tests/test_chat_display_messages.py` | Modified | 1 call site updated |
| `tests/test_chat_connection.py` | Modified | 1 call site updated |
