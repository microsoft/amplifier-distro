"""Tests for QueueDisplaySystem → event queue wiring."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDisplayMessageQueueWiring:
    @pytest.mark.asyncio
    async def test_on_message_puts_to_queue(self):
        """When create_session gets event_queue, display messages go into it."""
        from amplifier_distro.server.protocol_adapters import QueueDisplaySystem

        q: asyncio.Queue = asyncio.Queue()
        display = QueueDisplaySystem(q)

        await display.show_message("Hello from display", level="info", source="test")

        item = q.get_nowait()
        assert item == (
            "display_message",
            {
                "message": "Hello from display",
                "level": "info",
                "source": "test",
            },
        )

    @pytest.mark.asyncio
    async def test_create_session_wires_display_when_queue_provided(self):
        """create_session() with event_queue calls coordinator.set('display', ...)."""
        from amplifier_distro.server.session_backend import FoundationBackend

        mock_session = MagicMock()
        mock_session.session_id = "display-test-001"
        mock_session.project_id = "p"
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.hooks = MagicMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._load_bundle = AsyncMock(return_value=mock_prepared)
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()
        backend._approval_systems = {}
        backend._bundle_version = ""

        q: asyncio.Queue = asyncio.Queue()

        with patch("asyncio.create_task"):
            await backend.create_session(working_dir="~", event_queue=q)

        # coordinator.set should have been called with "display"
        set_calls = mock_session.coordinator.set.call_args_list
        display_calls = [c for c in set_calls if c.args[0] == "display"]
        assert len(display_calls) > 0, "coordinator.set('display', ...) not called"

    @pytest.mark.asyncio
    async def test_no_queue_skips_display_wiring(self):
        """Without event_queue, coordinator.set is not called."""
        from amplifier_distro.server.session_backend import FoundationBackend

        mock_session = MagicMock()
        mock_session.session_id = "display-test-002"
        mock_session.project_id = "p"
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.hooks = MagicMock()

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._load_bundle = AsyncMock(return_value=mock_prepared)
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()
        backend._approval_systems = {}
        backend._bundle_version = ""

        with patch("asyncio.create_task"):
            await backend.create_session(working_dir="~")  # no event_queue

        # coordinator.set should NOT have been called
        mock_session.coordinator.set.assert_not_called()
