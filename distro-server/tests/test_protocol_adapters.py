"""Tests for protocol adapters (ApprovalSystem, QueueDisplaySystem)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

# ── ApprovalSystem: auto-approve mode ──────────────────────────────────


class TestApprovalSystemAutoApprove:
    async def test_auto_approve_returns_first_option(self):
        from amplifier_distro.server.protocol_adapters import ApprovalSystem

        approval = ApprovalSystem(auto_approve=True)
        result = await approval.request_approval("Allow?", ["allow", "deny"])
        assert result == "allow"

    async def test_auto_approve_empty_options_returns_allow(self):
        from amplifier_distro.server.protocol_adapters import ApprovalSystem

        approval = ApprovalSystem(auto_approve=True)
        result = await approval.request_approval("Allow?", [])
        assert result == "allow"


# ── ApprovalSystem: interactive mode ───────────────────────────────────


class TestApprovalSystemInteractive:
    async def test_request_blocks_until_handle_response(self):
        from amplifier_distro.server.protocol_adapters import ApprovalSystem

        approval = ApprovalSystem(auto_approve=False)

        async def responder():
            await asyncio.sleep(0.01)
            for req_id in list(approval._pending.keys()):
                approval.handle_response(req_id, "allow")

        result, _ = await asyncio.gather(
            approval.request_approval("Allow tool?", ["allow", "deny"]),
            responder(),
        )
        assert result == "allow"

    async def test_handle_response_returns_true_for_valid_id(self):
        from amplifier_distro.server.protocol_adapters import ApprovalSystem

        approval = ApprovalSystem(auto_approve=False)

        async def background():
            await asyncio.sleep(0.01)
            req_id = next(iter(approval._pending.keys()))
            return approval.handle_response(req_id, "deny")

        _, ok = await asyncio.gather(
            approval.request_approval("?", ["allow", "deny"], timeout=1.0),
            background(),
        )
        assert ok is True

    async def test_handle_response_returns_false_for_unknown_id(self):
        from amplifier_distro.server.protocol_adapters import ApprovalSystem

        approval = ApprovalSystem(auto_approve=False)
        result = approval.handle_response("no-such-id", "allow")
        assert result is False

    async def test_timeout_returns_default(self):
        from amplifier_distro.server.protocol_adapters import ApprovalSystem

        approval = ApprovalSystem(auto_approve=False)
        result = await approval.request_approval(
            "Allow?", ["allow", "deny"], timeout=0.05, default="deny"
        )
        assert result == "deny"

    async def test_on_approval_request_callback_called(self):
        from amplifier_distro.server.protocol_adapters import ApprovalSystem

        callback = AsyncMock()
        approval = ApprovalSystem(
            auto_approve=False,
            on_approval_request=callback,
        )

        async def background():
            await asyncio.sleep(0.01)
            for req_id in list(approval._pending.keys()):
                approval.handle_response(req_id, "allow")

        await asyncio.gather(
            approval.request_approval("Allow?", ["allow", "deny"]),
            background(),
        )
        callback.assert_awaited_once()


class TestApprovalSystemFirstWriteWins:
    async def test_second_handle_response_returns_false(self):
        from amplifier_distro.server.protocol_adapters import ApprovalSystem

        approval = ApprovalSystem(auto_approve=False)

        async def background():
            await asyncio.sleep(0.01)
            req_id = next(iter(approval._pending.keys()))
            first = approval.handle_response(req_id, "allow")
            second = approval.handle_response(req_id, "deny")
            return first, second

        result, (first_ok, second_ok) = await asyncio.gather(
            approval.request_approval("?", ["allow", "deny"], timeout=1.0),
            background(),
        )
        assert first_ok is True
        assert second_ok is False
        assert result == "allow"

    async def test_handle_response_after_timeout_returns_false(self):
        from amplifier_distro.server.protocol_adapters import ApprovalSystem

        approval = ApprovalSystem(auto_approve=False)
        await approval.request_approval(
            "?", ["allow", "deny"], timeout=0.01, default="deny"
        )
        # All pending requests are cleaned up after timeout
        result = approval.handle_response("no-such-id", "allow")
        assert result is False


# ── QueueDisplaySystem ─────────────────────────────────────────────────


class TestQueueDisplaySystem:
    async def test_show_message_puts_to_queue(self):
        from amplifier_distro.server.protocol_adapters import QueueDisplaySystem

        q: asyncio.Queue = asyncio.Queue()
        display = QueueDisplaySystem(q)
        await display.show_message("hello", level="warning", source="test")

        item = q.get_nowait()
        assert item == (
            "display_message",
            {
                "message": "hello",
                "level": "warning",
                "source": "test",
            },
        )

    async def test_show_message_defaults(self):
        from amplifier_distro.server.protocol_adapters import QueueDisplaySystem

        q: asyncio.Queue = asyncio.Queue()
        display = QueueDisplaySystem(q)
        await display.show_message("hi")

        _, data = q.get_nowait()
        assert data["level"] == "info"
        assert data["source"] == "hook"

    def test_push_pop_nesting(self):
        from amplifier_distro.server.protocol_adapters import QueueDisplaySystem

        q: asyncio.Queue = asyncio.Queue()
        d0 = QueueDisplaySystem(q)
        assert d0.nesting_depth == 0

        d1 = d0.push_nesting()
        assert d1.nesting_depth == 1

        d0_again = d1.pop_nesting()
        assert d0_again.nesting_depth == 0

    def test_pop_nesting_floors_at_zero(self):
        from amplifier_distro.server.protocol_adapters import QueueDisplaySystem

        q: asyncio.Queue = asyncio.Queue()
        d = QueueDisplaySystem(q)
        d2 = d.pop_nesting()
        assert d2.nesting_depth == 0


# ── LogDisplaySystem ────────────────────────────────────────────────────────────


class TestLogDisplaySystem:
    def test_show_message_info_routes_to_logger(self, caplog: pytest.LogCaptureFixture):
        from amplifier_distro.server.protocol_adapters import LogDisplaySystem

        display = LogDisplaySystem()
        with caplog.at_level(logging.INFO, logger="amplifier_distro.display"):
            display.show_message("hello world", level="info", source="test-hook")
        assert "hello world" in caplog.text

    def test_show_message_warning_routes_at_warning_level(
        self, caplog: pytest.LogCaptureFixture
    ):
        from amplifier_distro.server.protocol_adapters import LogDisplaySystem

        display = LogDisplaySystem()
        with caplog.at_level(logging.WARNING, logger="amplifier_distro.display"):
            display.show_message("uh oh", level="warning", source="test-hook")
        assert "uh oh" in caplog.text

    def test_show_message_error_routes_at_error_level(
        self, caplog: pytest.LogCaptureFixture
    ):
        from amplifier_distro.server.protocol_adapters import LogDisplaySystem

        display = LogDisplaySystem()
        with caplog.at_level(logging.ERROR, logger="amplifier_distro.display"):
            display.show_message("kaboom", level="error", source="test-hook")
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
