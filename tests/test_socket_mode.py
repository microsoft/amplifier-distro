"""Tests for SocketModeAdapter lifecycle — stop() drains pending tasks."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_events_api_frame(
    ts: str = "1234567890.000001",
    channel: str = "C_TEST",
    user: str = "U_USER",
    text: str = "hello",
) -> dict:
    """Build a minimal events_api WebSocket frame."""
    return {
        "type": "events_api",
        "envelope_id": f"env-{ts}",
        "payload": {
            "event": {
                "type": "message",
                "user": user,
                "text": text,
                "channel": channel,
                "ts": ts,
            }
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter():
    """SocketModeAdapter wired with no-op mocks (no network)."""
    from amplifier_distro.server.apps.slack.socket_mode import SocketModeAdapter

    config = MagicMock()
    event_handler = MagicMock()
    event_handler.handle_event_payload = AsyncMock(return_value={"ok": True})

    inst = SocketModeAdapter(config, event_handler)
    # _ws is None → _ack() becomes a no-op (no real WebSocket)
    inst._ws = None
    # Pretend we know our bot ID so self-message filtering is deterministic
    inst._bot_user_id = "U_BOT"
    return inst


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSocketModeStop:
    """Verify stop() drains pending tasks before cancelling the main loop."""

    async def test_stop_waits_for_pending_tasks(self, adapter):
        """stop() must await pending tasks up to 30 s before cancelling."""
        completion_order = []

        async def slow_handler(event_payload):
            await asyncio.sleep(0.05)
            completion_order.append("handler_done")

        adapter._event_handler.handle_event_payload = slow_handler
        frame = _make_events_api_frame(ts="stop-test-001")

        await adapter._handle_frame(frame)
        assert len(adapter._pending_tasks) > 0, "Expected a pending task"

        await adapter.stop()

        assert "handler_done" in completion_order, (
            "stop() returned before pending task completed"
        )
        assert len(adapter._pending_tasks) == 0

    async def test_stop_cancels_tasks_that_exceed_timeout(self, adapter):
        """Tasks still running after timeout must be cancelled, not left hanging."""
        import asyncio as _asyncio

        async def hanging_handler(event_payload):
            await _asyncio.sleep(9999)

        adapter._event_handler.handle_event_payload = hanging_handler
        frame = _make_events_api_frame(ts="stop-test-002")

        await adapter._handle_frame(frame)
        await asyncio.sleep(0)  # let task start

        async def instant_timeout(tasks, timeout=None):
            return set(), set(tasks)

        with patch("asyncio.wait", side_effect=instant_timeout):
            await adapter.stop()

        # All tasks should be cancelled/done
        for task in adapter._pending_tasks:
            assert task.done() or task.cancelled()
