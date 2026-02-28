"""Tests for VoiceConnection.

Regression tests for three API-mismatch bugs where connection.py was written
against a slightly different interface than FoundationBackend provides:

  Bug 1: create_session() has no `app_name` param → must use `description`
  Bug 2: register_hooks() does not exist → hook wiring is internal to create_session
  Bug 3: cancel_session() has no `immediate` kwarg → must use `level` string
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_distro.server.apps.voice.connection import (
    _EVENT_QUEUE_MAX_SIZE,
    VoiceConnection,
)


def make_backend(session_id: str = "voice-sess-001"):
    """Mock backend that matches the real FoundationBackend signature."""
    backend = MagicMock()
    info = MagicMock()
    info.session_id = session_id
    info.coordinator = None  # most tests don't need spawn capability
    backend.create_session = AsyncMock(return_value=info)
    backend.cancel_session = AsyncMock(return_value=None)
    backend.end_session = AsyncMock(return_value=None)
    backend.mark_disconnected = AsyncMock(return_value=None)
    # Mock get_hook_unregister to return a callable (required by fix/approval-display)
    backend.get_hook_unregister = MagicMock(return_value=MagicMock())
    return backend


def make_repository():
    repo = MagicMock()
    repo.update_status = MagicMock()
    repo.end_conversation = MagicMock()
    return repo


# ---------------------------------------------------------------------------
# Bug 1 regression: create() must use `description=`, not `app_name=`
# ---------------------------------------------------------------------------


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_create_does_not_raise_type_error(self):
        """Bug 1: create_session() has no app_name param.

        Before the fix, VoiceConnection.create() passed app_name="voice" which
        raised TypeError: create_session() got an unexpected keyword argument
        'app_name'.
        """
        backend = make_backend("sess-voice-123")
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        # Must not raise TypeError
        session_id = await conn.create("/tmp/workspace")
        assert session_id == "sess-voice-123"

    @pytest.mark.asyncio
    async def test_create_passes_description_not_app_name(self):
        """Bug 1: verify the exact keyword passed to create_session is `description`."""
        backend = make_backend()
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.create("/tmp/workspace")

        backend.create_session.assert_awaited_once()
        kwargs = backend.create_session.call_args.kwargs
        assert "description" in kwargs, "must pass description= to create_session"
        assert kwargs["description"] == "voice"
        assert "app_name" not in kwargs, "app_name does not exist on create_session"

    @pytest.mark.asyncio
    async def test_create_passes_working_dir(self):
        """create() forwards workspace_root as working_dir."""
        backend = make_backend()
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.create("/home/user/project")

        kwargs = backend.create_session.call_args.kwargs
        assert kwargs.get("working_dir") == "/home/user/project"

    @pytest.mark.asyncio
    async def test_create_passes_event_queue(self):
        """create() wires the event_queue into create_session via surface."""
        backend = make_backend()
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.create("/tmp")

        kwargs = backend.create_session.call_args.kwargs
        surface = kwargs.get("surface")
        assert surface is not None
        assert surface.event_queue is conn.event_queue

    @pytest.mark.asyncio
    async def test_create_stores_session_id(self):
        """After create(), session_id property reflects the backend session."""
        backend = make_backend("sess-stored-456")
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        returned_id = await conn.create("/tmp")

        assert conn.session_id == "sess-stored-456"
        assert returned_id == "sess-stored-456"


# ---------------------------------------------------------------------------
# Bug 2 regression: register_hooks() must NOT be called — it doesn't exist
# ---------------------------------------------------------------------------


class TestNoRegisterHooks:
    @pytest.mark.asyncio
    async def test_create_does_not_call_register_hooks(self):
        """Bug 2: register_hooks() does not exist on FoundationBackend.

        Hook wiring is automatic inside create_session() when event_queue is
        passed.  Calling register_hooks would raise AttributeError after Bug 1
        was fixed.
        """
        backend = make_backend()
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.create("/tmp")

        # register_hooks does not exist on FoundationBackend — must never be called
        if hasattr(backend, "register_hooks"):
            backend.register_hooks.assert_not_called()

    @pytest.mark.asyncio
    async def test_hook_unregister_set_after_create(self):
        """create() passes surface to backend for hook management.

        The surface parameter ensures hooks are properly registered and
        unregistered on disconnect, preventing dead hook accumulation across reconnects.
        """
        backend = make_backend()
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.create("/tmp")

        # Verify surface was passed to create_session for hook management
        kwargs = backend.create_session.call_args.kwargs
        assert kwargs.get("surface") is not None


# ---------------------------------------------------------------------------
# Bug 3 regression: cancel() must pass level= string, not immediate= bool
# ---------------------------------------------------------------------------


class TestCancelSession:
    @pytest.mark.asyncio
    async def test_cancel_immediate_passes_level_immediate(self):
        """Bug 3: cancel_session() takes level='immediate', not immediate=True."""
        backend = make_backend("sess-cancel-001")
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.create("/tmp")
        await conn.cancel(level="immediate")

        backend.cancel_session.assert_awaited_once_with(
            "sess-cancel-001", level="immediate"
        )

    @pytest.mark.asyncio
    async def test_cancel_graceful_passes_level_graceful(self):
        """cancel(level='graceful') passes level='graceful' to cancel_session."""
        backend = make_backend("sess-cancel-002")
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.create("/tmp")
        await conn.cancel(level="graceful")

        backend.cancel_session.assert_awaited_once_with(
            "sess-cancel-002", level="graceful"
        )

    @pytest.mark.asyncio
    async def test_cancel_default_is_graceful(self):
        """cancel() with no args defaults to graceful."""
        backend = make_backend("sess-cancel-003")
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.create("/tmp")
        await conn.cancel()

        backend.cancel_session.assert_awaited_once_with(
            "sess-cancel-003", level="graceful"
        )

    @pytest.mark.asyncio
    async def test_cancel_does_not_pass_immediate_kwarg(self):
        """cancel_session() has no `immediate` parameter — verify it's never passed."""
        backend = make_backend("sess-cancel-004")
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.create("/tmp")
        await conn.cancel(level="immediate")

        _, kwargs = backend.cancel_session.call_args
        assert "immediate" not in kwargs, (
            "immediate= is not a valid cancel_session param"
        )

    @pytest.mark.asyncio
    async def test_cancel_no_op_without_session(self):
        """cancel() before create() is a no-op — no backend call."""
        backend = make_backend()
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        await conn.cancel(level="immediate")

        backend.cancel_session.assert_not_awaited()


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------


class TestStructural:
    def test_event_queue_is_bounded(self):
        """event_queue must be bounded to prevent unbounded memory growth."""
        backend = make_backend()
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        assert conn.event_queue.maxsize > 0

    def test_event_queue_maxsize_is_10000(self):
        """event_queue maxsize must be 10000."""
        backend = make_backend()
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        assert _EVENT_QUEUE_MAX_SIZE == 10000
        assert conn.event_queue.maxsize == _EVENT_QUEUE_MAX_SIZE

    def test_session_id_none_before_create(self):
        """session_id is None before create() is called."""
        backend = make_backend()
        repo = make_repository()

        conn = VoiceConnection(repo, backend)
        assert conn.session_id is None
