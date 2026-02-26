"""Tests for session cancellation support."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_distro.server.session_backend import FoundationBackend, _SessionHandle


class TestSessionHandleCancel:
    @pytest.mark.asyncio
    async def test_cancel_graceful_calls_coordinator(self):
        mock_session = MagicMock()
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.request_cancel = MagicMock()

        handle = _SessionHandle(
            session_id="s001",
            project_id="p001",
            working_dir=Path("/tmp"),
            session=mock_session,
        )

        await handle.cancel("graceful")
        mock_session.coordinator.request_cancel.assert_called_once_with("graceful")

    @pytest.mark.asyncio
    async def test_cancel_no_session_does_not_raise(self):
        """If _session is None, cancel() is a safe no-op."""
        handle = _SessionHandle(
            session_id="s002",
            project_id="p002",
            working_dir=Path("/tmp"),
            session=None,
        )
        await handle.cancel("graceful")  # Should not raise

    @pytest.mark.asyncio
    async def test_cancel_no_coordinator_does_not_raise(self):
        """If session has no coordinator, cancel() is a safe no-op."""
        mock_session = MagicMock(spec=[])  # no attributes
        handle = _SessionHandle(
            session_id="s003",
            project_id="p003",
            working_dir=Path("/tmp"),
            session=mock_session,
        )
        await handle.cancel("graceful")  # Should not raise


class TestFoundationBackendCancelSession:
    @pytest.mark.asyncio
    async def test_cancel_session_delegates_to_handle(self):
        mock_handle = MagicMock()
        mock_handle.cancel = AsyncMock()

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._sessions = {"sess-cancel-001": mock_handle}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        await backend.cancel_session("sess-cancel-001", "graceful")
        mock_handle.cancel.assert_awaited_once_with("graceful")

    @pytest.mark.asyncio
    async def test_cancel_session_unknown_id_does_not_raise(self):
        """Cancelling a session that doesn't exist is a safe no-op."""
        backend = FoundationBackend.__new__(FoundationBackend)
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        await backend.cancel_session("no-such-session", "immediate")  # no raise

    @pytest.mark.asyncio
    async def test_cancel_session_immediate_level_passed_through(self):
        mock_handle = MagicMock()
        mock_handle.cancel = AsyncMock()

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._sessions = {"s": mock_handle}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        await backend.cancel_session("s", "immediate")
        mock_handle.cancel.assert_awaited_once_with("immediate")
