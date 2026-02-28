"""Tests for event queue wiring in FoundationBackend."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from amplifier_distro.server.protocol_adapters import web_chat_surface
from amplifier_distro.server.session_backend import FoundationBackend, MockBackend


class TestMockBackendQueueIgnored:
    """MockBackend gracefully ignores event_queue (it doesn't stream)."""

    @pytest.mark.asyncio
    async def test_create_session_accepts_event_queue(self):
        backend = MockBackend()
        q: asyncio.Queue = asyncio.Queue()
        info = await backend.create_session(
            working_dir="~", surface=web_chat_surface(q)
        )
        assert info.session_id is not None


class TestFoundationBackendQueueWiring:
    """FoundationBackend wires event hooks via _attach_surface when surface= given."""

    @pytest.fixture()
    def bare_backend(self) -> FoundationBackend:
        """FoundationBackend with __init__ bypassed (no foundation required)."""
        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()
        backend._approval_systems = {}
        return backend

    def _mock_session(self, session_id: str = "test-session-001") -> MagicMock:
        """Create a mock foundation session with coordinator and hooks."""
        mock_session = MagicMock()
        mock_session.session_id = session_id
        mock_session.project_id = "test-project"
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.hooks = MagicMock()
        return mock_session

    @pytest.mark.asyncio
    async def test_create_session_wires_on_stream_when_queue_provided(
        self, bare_backend
    ):
        """When event_queue is provided, hooks.register is called for streaming."""
        mock_session = self._mock_session("test-session-001")
        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        bare_backend._load_bundle = AsyncMock(return_value=mock_prepared)

        q: asyncio.Queue = asyncio.Queue()

        with patch("asyncio.create_task"):
            await bare_backend.create_session(
                working_dir="~", surface=web_chat_surface(q)
            )

        # hooks.register should have been called (for streaming wiring)
        mock_session.coordinator.hooks.register.assert_called()

    @pytest.mark.asyncio
    async def test_create_session_no_queue_skips_streaming_wiring(self, bare_backend):
        """Without event_queue, streaming hooks (ALL_EVENTS) are not registered.
        Transcript hooks (tool:post, orchestrator:complete) are always registered."""
        mock_session = self._mock_session("test-session-002")
        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        bare_backend._load_bundle = AsyncMock(return_value=mock_prepared)

        with patch("asyncio.create_task"):
            await bare_backend.create_session(working_dir="~")

        # Transcript hooks are always registered regardless of event_queue
        call_args = [
            c.kwargs.get("event") or (c.args[0] if c.args else None)
            for c in mock_session.coordinator.hooks.register.call_args_list
        ]
        assert "tool:post" in call_args
        assert "orchestrator:complete" in call_args
        # Streaming events (from ALL_EVENTS) should NOT be registered without queue
        assert "content_block:delta" not in call_args

    @pytest.mark.asyncio
    async def test_execute_calls_handle_run(self, bare_backend):
        """execute() calls handle.run() and returns None."""
        handle = MagicMock()
        handle.run = AsyncMock(return_value="response text")
        bare_backend._sessions = {"sess-001": handle}

        await bare_backend.execute("sess-001", "hello world")
        handle.run.assert_called_once_with("hello world")

    @pytest.mark.asyncio
    async def test_execute_raises_on_unknown_session(self, bare_backend):
        with pytest.raises(ValueError, match="Unknown session"):
            await bare_backend.execute("no-such-session", "hello")
