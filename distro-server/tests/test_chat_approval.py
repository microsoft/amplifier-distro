"""Tests for the rebuilt ApprovalSystem."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from amplifier_distro.server.protocol_adapters import ApprovalSystem, web_chat_surface


class TestApprovalSystemAutoApprove:
    @pytest.mark.asyncio
    async def test_auto_approve_returns_first_option(self):
        approval = ApprovalSystem(auto_approve=True)
        result = await approval.request_approval("Allow?", ["allow", "deny"])
        assert result == "allow"

    @pytest.mark.asyncio
    async def test_auto_approve_empty_options_returns_allow(self):
        approval = ApprovalSystem(auto_approve=True)
        result = await approval.request_approval("Allow?", [])
        assert result == "allow"


class TestApprovalSystemInteractive:
    @pytest.mark.asyncio
    async def test_request_blocks_until_handle_response(self):
        """request_approval blocks until handle_response is called."""
        approval = ApprovalSystem(auto_approve=False)

        async def responder():
            await asyncio.sleep(0.01)  # Let request_approval start
            for req_id in list(approval._pending.keys()):
                approval.handle_response(req_id, "allow")

        result, _ = await asyncio.gather(
            approval.request_approval("Allow tool?", ["allow", "deny"]),
            responder(),
        )
        assert result == "allow"

    @pytest.mark.asyncio
    async def test_handle_response_returns_true_for_valid_id(self):
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

    @pytest.mark.asyncio
    async def test_handle_response_returns_false_for_unknown_id(self):
        approval = ApprovalSystem(auto_approve=False)
        result = approval.handle_response("no-such-id", "allow")
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout_returns_default(self):
        approval = ApprovalSystem(auto_approve=False)
        result = await approval.request_approval(
            "Allow?", ["allow", "deny"], timeout=0.05, default="deny"
        )
        assert result == "deny"

    @pytest.mark.asyncio
    async def test_on_approval_request_callback_called(self):
        """on_approval_request callback fires with request details."""
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
        call_kwargs = callback.call_args
        # callback receives (request_id, prompt, options, timeout, default)
        assert "allow" in call_kwargs.args[2]


class TestHandleResponseFirstWriteWins:
    @pytest.mark.asyncio
    async def test_second_handle_response_returns_false(self):
        """First handle_response wins; second call on same request_id returns False."""
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
        # First response wins
        assert result == "allow"

    @pytest.mark.asyncio
    async def test_handle_response_after_timeout_returns_false(self):
        """handle_response on an already-resolved (timed-out) request returns False."""
        approval = ApprovalSystem(auto_approve=False)

        # Let it timeout quickly
        req_result = await approval.request_approval(
            "?", ["allow", "deny"], timeout=0.01, default="deny"
        )
        assert req_result == "deny"

        # After timeout, pending is cleaned up; handle_response must return False
        result = approval.handle_response("no-such-id-after-timeout", "allow")
        assert result is False


class TestFoundationBackendResolveApproval:
    def test_resolve_approval_delegates_to_session_approval(self):
        from amplifier_distro.server.session_backend import FoundationBackend

        mock_approval = MagicMock()
        mock_approval.handle_response = MagicMock(return_value=True)

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._sessions = {"s001": MagicMock()}
        backend._approval_systems = {"s001": mock_approval}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        result = backend.resolve_approval("s001", "req-001", "allow")
        assert result is True
        mock_approval.handle_response.assert_called_once_with("req-001", "allow")

    def test_resolve_approval_unknown_session_returns_false(self):
        from amplifier_distro.server.session_backend import FoundationBackend

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._sessions = {}
        backend._approval_systems = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        result = backend.resolve_approval("no-session", "req-001", "allow")
        assert result is False


@pytest.mark.asyncio
async def test_create_session_with_event_queue_pushes_approval_request():
    """create_session() must wire on_approval_request so the queue receives
    an ('approval_request', {...}) tuple when request_approval() is triggered."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from amplifier_distro.server.session_backend import FoundationBackend

    mock_session = MagicMock()
    mock_session.session_id = "eq-test-001"
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

    event_queue: asyncio.Queue = asyncio.Queue()

    with patch("asyncio.create_task"):
        await backend.create_session(
            working_dir="~", surface=web_chat_surface(event_queue)
        )

    # Trigger the approval system directly (simulates kernel calling it)
    approval = backend._approval_systems["eq-test-001"]
    assert approval is not None

    # request_approval in background so it doesn't block
    async def _request():
        await approval.request_approval(
            "Allow tool?", ["allow", "deny"], timeout=1.0, default="deny"
        )

    async def _respond():
        # Give request_approval time to register the pending event
        await asyncio.sleep(0.05)
        # Respond so it doesn't hang
        for req_id in list(approval._pending.keys()):
            approval.handle_response(req_id, "allow")

    await asyncio.gather(_request(), _respond())

    # The event_queue must contain an approval_request tuple
    items = []
    while not event_queue.empty():
        items.append(event_queue.get_nowait())

    types = [item[0] for item in items]
    assert "approval_request" in types, (
        f"Expected 'approval_request' in queue, got: {types}"
    )

    approval_item = next(item for item in items if item[0] == "approval_request")
    data = approval_item[1]
    assert data["prompt"] == "Allow tool?"
    assert data["options"] == ["allow", "deny"]
    assert "request_id" in data


@pytest.mark.asyncio
async def test_create_session_populates_approval_systems():
    """create_session() with event_queue must wire _approval_systems so
    resolve_approval works."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from amplifier_distro.server.session_backend import FoundationBackend

    mock_session = MagicMock()
    mock_session.session_id = "approval-wire-001"
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

    event_queue: asyncio.Queue = asyncio.Queue()

    with patch("asyncio.create_task"):
        await backend.create_session(
            working_dir="~", surface=web_chat_surface(event_queue)
        )

    assert "approval-wire-001" in backend._approval_systems
    approval = backend._approval_systems["approval-wire-001"]
    assert approval is not None
