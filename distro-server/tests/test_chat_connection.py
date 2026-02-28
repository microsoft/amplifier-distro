"""Tests for ChatConnection."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from amplifier_distro.server.apps.chat.connection import _STOP
from amplifier_distro.server.protocol_adapters import web_chat_surface


def make_ws(messages: list[dict]):
    """Create a mock WebSocket that replays messages then raises disconnect."""
    from starlette.websockets import WebSocketDisconnect

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()

    msg_iter = iter(messages)

    async def receive_json():
        try:
            return next(msg_iter)
        except StopIteration:
            raise WebSocketDisconnect(code=1000) from None

    ws.receive_json = receive_json
    return ws


def make_backend(session_id: str = "test-sess-001"):
    backend = MagicMock()
    info = MagicMock()
    info.session_id = session_id
    info.working_dir = "/tmp/test"
    backend.create_session = AsyncMock(return_value=info)
    backend.resume_session = AsyncMock(return_value=None)
    backend.get_session_info = AsyncMock(return_value=info)
    backend.execute = AsyncMock(return_value=None)
    backend.cancel_session = AsyncMock(return_value=None)
    backend.resolve_approval = MagicMock(return_value=True)
    return backend


def make_config(api_key: str | None = None):
    config = MagicMock()
    config.server = MagicMock()
    config.server.api_key = api_key
    return config


class TestAuthHandshake:
    @pytest.mark.asyncio
    async def test_no_api_key_skips_auth(self):
        """When api_key is None, auth is skipped immediately."""
        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws([])
        backend = make_backend()
        config = make_config(api_key=None)

        conn = ChatConnection(ws, backend, config)
        await conn._auth_handshake()
        ws.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_correct_token_sends_auth_ok(self):
        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws([{"type": "auth", "token": "secret"}])
        backend = make_backend()
        config = make_config(api_key="secret")

        conn = ChatConnection(ws, backend, config)
        await conn._auth_handshake()

        ws.send_json.assert_awaited_once_with({"type": "auth_ok"})
        ws.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_token_closes_4001(self):
        from starlette.websockets import WebSocketDisconnect

        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws([{"type": "auth", "token": "wrong"}])
        backend = make_backend()
        config = make_config(api_key="secret")

        conn = ChatConnection(ws, backend, config)
        with pytest.raises(WebSocketDisconnect):
            await conn._auth_handshake()

        ws.close.assert_awaited_once_with(4001, "Unauthorized")


class TestReceiveLoop:
    @pytest.mark.asyncio
    async def test_create_session_message(self):
        from starlette.websockets import WebSocketDisconnect

        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws(
            [
                {
                    "type": "create_session",
                    "bundle": "foundation",
                    "cwd": "/tmp",
                    "behaviors": [],
                },
            ]
        )
        backend = make_backend("sess-abc")
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        with pytest.raises(WebSocketDisconnect):
            await conn._receive_loop()

        backend.create_session.assert_awaited_once()
        call_kwargs = backend.create_session.call_args.kwargs
        assert call_kwargs.get("working_dir") == "/tmp"

    @pytest.mark.asyncio
    async def test_ping_sends_pong(self):
        from starlette.websockets import WebSocketDisconnect

        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws([{"type": "ping"}])
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        with pytest.raises(WebSocketDisconnect):
            await conn._receive_loop()

        sent = [call.args[0] for call in ws.send_json.await_args_list]
        assert any(m.get("type") == "pong" for m in sent)

    @pytest.mark.asyncio
    async def test_create_session_with_resume_id_calls_backend_resume(self):
        from starlette.websockets import WebSocketDisconnect

        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws(
            [
                {
                    "type": "create_session",
                    "cwd": "/tmp/resume",
                    "resume_session_id": "sess-resume-123",
                },
            ]
        )
        backend = make_backend("sess-resume-123")
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        with pytest.raises(WebSocketDisconnect):
            await conn._receive_loop()

        backend.resume_session.assert_awaited_once_with(
            "sess-resume-123",
            "/tmp/resume",
            surface=ANY,
        )
        call_kwargs = backend.resume_session.await_args.kwargs
        assert call_kwargs["surface"].event_queue is conn.event_queue
        backend.create_session.assert_not_awaited()

        sent = [call.args[0] for call in ws.send_json.await_args_list]
        created = [m for m in sent if m.get("type") == "session_created"]
        assert len(created) == 1
        assert created[0]["session_id"] == "sess-resume-123"


class TestEventFanout:
    @pytest.mark.asyncio
    async def test_events_forwarded_to_websocket(self):
        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws([])
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        await conn.event_queue.put(("orchestrator:complete", {"turn_count": 1}))
        await conn.event_queue.put(_STOP)  # sentinel to stop the loop

        await conn._event_fanout_loop()

        sent = [call.args[0] for call in ws.send_json.await_args_list]
        assert any(m.get("type") == "prompt_complete" for m in sent)

    @pytest.mark.asyncio
    async def test_unknown_events_not_forwarded(self):
        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws([])
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        await conn.event_queue.put(("some:unknown:event", {}))
        await conn.event_queue.put(_STOP)

        await conn._event_fanout_loop()

        # Unknown event produces None from translator — nothing sent
        ws.send_json.assert_not_awaited()


class TestInputValidation:
    """Validate that untrusted WebSocket inputs are sanitized."""

    @pytest.mark.asyncio
    async def test_resume_rejects_path_traversal_session_id(self):
        """A session ID with path traversal characters should be rejected."""
        from starlette.websockets import WebSocketDisconnect

        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws(
            [
                {
                    "type": "create_session",
                    "cwd": "/tmp",
                    "resume_session_id": "../../../etc/passwd",
                },
            ]
        )
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        with pytest.raises(WebSocketDisconnect):
            await conn._receive_loop()

        # Backend should NOT have been called
        backend.resume_session.assert_not_awaited()
        backend.create_session.assert_not_awaited()

        sent = [call.args[0] for call in ws.send_json.await_args_list]
        errors = [m for m in sent if m.get("type") == "error"]
        assert len(errors) == 1
        assert "Invalid session ID" in errors[0]["error"]

    @pytest.mark.asyncio
    async def test_resume_rejects_session_id_with_spaces(self):
        """Session IDs with spaces should be rejected."""
        from starlette.websockets import WebSocketDisconnect

        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws(
            [
                {
                    "type": "create_session",
                    "cwd": "/tmp",
                    "resume_session_id": "bad session id",
                },
            ]
        )
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        with pytest.raises(WebSocketDisconnect):
            await conn._receive_loop()

        backend.resume_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_accepts_valid_session_id(self):
        """Valid session IDs (alphanumeric, hyphens, underscores) should pass."""
        from starlette.websockets import WebSocketDisconnect

        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws(
            [
                {
                    "type": "create_session",
                    "cwd": "/tmp",
                    "resume_session_id": "sess_abc-123_DEF",
                },
            ]
        )
        backend = make_backend("sess_abc-123_DEF")
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        with pytest.raises(WebSocketDisconnect):
            await conn._receive_loop()

        # Valid ID should reach the backend
        backend.resume_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cwd_rejects_null_bytes(self):
        """Working directory with null bytes should be rejected."""
        from starlette.websockets import WebSocketDisconnect

        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws(
            [
                {
                    "type": "create_session",
                    "cwd": "/tmp/\x00evil",
                },
            ]
        )
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        with pytest.raises(WebSocketDisconnect):
            await conn._receive_loop()

        backend.create_session.assert_not_awaited()

        sent = [call.args[0] for call in ws.send_json.await_args_list]
        errors = [m for m in sent if m.get("type") == "error"]
        assert len(errors) == 1
        assert "Invalid working directory" in errors[0]["error"]


class TestEventQueueBounded:
    def test_event_queue_has_maxsize(self):
        """event_queue must be bounded to prevent unbounded memory growth."""
        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws([])
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        assert conn.event_queue.maxsize > 0, "event_queue must have a maxsize"

    def test_event_queue_maxsize_is_10000(self):
        """event_queue maxsize should be 10000."""
        from amplifier_distro.server.apps.chat.connection import (
            _EVENT_QUEUE_MAX_SIZE,
            ChatConnection,
        )

        ws = make_ws([])
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        assert _EVENT_QUEUE_MAX_SIZE == 10000
        assert conn.event_queue.maxsize == _EVENT_QUEUE_MAX_SIZE


class TestSyntheticStreaming:
    @pytest.mark.asyncio
    async def test_synthetic_deltas_sent_for_non_streaming_blocks(self):
        """When content_end arrives with no prior deltas, synthesize chunked deltas."""
        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws([])
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)

        # Simulate runtime shape: content_start then content_end (no deltas)
        await conn.event_queue.put(
            ("content_block:start", {"block_type": "text", "block_index": 2})
        )
        await conn.event_queue.put(
            (
                "content_block:end",
                {
                    "block_index": 2,
                    "block": {"type": "text", "text": "Hello world synthetic"},
                },
            )
        )
        await conn.event_queue.put(_STOP)

        await conn._event_fanout_loop()

        sent = [call.args[0] for call in ws.send_json.await_args_list]
        delta_messages = [m for m in sent if m.get("type") == "content_delta"]
        # Should have multiple delta messages (chunked at 12 chars)
        assert len(delta_messages) > 1
        # Concatenated deltas should reconstruct the text
        full = "".join(m["delta"] for m in delta_messages)
        assert full == "Hello world synthetic"
        # Synthetic chunks should map using block_index (not hard-coded index=0)
        assert all(m["index"] == 2 for m in delta_messages)

    @pytest.mark.asyncio
    async def test_synthetic_deltas_support_object_block_payload(self):
        """Synthetic chunking reads text/index from object-style block payloads."""
        from amplifier_distro.server.apps.chat.connection import ChatConnection

        ws = make_ws([])
        backend = make_backend()
        config = make_config()

        conn = ChatConnection(ws, backend, config)
        block = type("B", (), {"index": 4, "thinking": "Object payload thinking"})()

        await conn.event_queue.put(
            ("content_block:start", {"block_type": "thinking", "block_index": 4})
        )
        await conn.event_queue.put(("content_block:end", {"block": block}))
        await conn.event_queue.put(_STOP)

        await conn._event_fanout_loop()

        sent = [call.args[0] for call in ws.send_json.await_args_list]
        delta_messages = [m for m in sent if m.get("type") == "content_delta"]
        assert len(delta_messages) > 0
        full = "".join(m["delta"] for m in delta_messages)
        assert full == "Object payload thinking"
