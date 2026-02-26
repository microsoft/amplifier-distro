"""Tests for server-side slash command handlers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def make_connection(session_id: str = "test-sess"):
    from amplifier_distro.server.apps.chat.connection import ChatConnection

    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.accept = AsyncMock()
    backend = MagicMock()
    backend.create_session = AsyncMock(
        return_value=MagicMock(session_id="new-sess", working_dir="/new")
    )
    backend.cancel_session = AsyncMock(return_value=None)
    backend.end_session = AsyncMock(return_value=None)
    config = MagicMock()
    config.server.api_key = None
    conn = ChatConnection(ws, backend, config)
    conn._session_id = session_id
    return conn, ws, backend


class TestCommandDispatch:
    @pytest.mark.asyncio
    async def test_status_command_returns_session_id(self):
        """status command returns current session_id and status."""
        conn, _ws, _backend = make_connection("sess-001")
        result = await conn._dispatch_command("status", [])
        assert result["session_id"] == "sess-001"
        assert "status" in result

    @pytest.mark.asyncio
    async def test_status_command_no_session(self):
        """status command with no session returns no_session status."""
        conn, _ws, _backend = make_connection()
        conn._session_id = None
        result = await conn._dispatch_command("status", [])
        assert result["session_id"] is None
        assert result["status"] == "no_session"

    @pytest.mark.asyncio
    async def test_bundle_command_creates_new_session(self):
        """bundle command creates a new session with the specified bundle."""
        conn, _ws, backend = make_connection()
        result = await conn._dispatch_command("bundle", ["my-bundle"])
        backend.create_session.assert_awaited_once()
        assert "session_id" in result

    @pytest.mark.asyncio
    async def test_bundle_command_passes_bundle_name(self):
        """bundle command passes the bundle name to create_session."""
        conn, _ws, backend = make_connection()
        await conn._dispatch_command("bundle", ["foundation"])
        call_kwargs = backend.create_session.call_args.kwargs
        assert call_kwargs.get("bundle_name") == "foundation"

    @pytest.mark.asyncio
    async def test_cwd_command_creates_new_session(self):
        """cwd command creates a new session with the specified working directory."""
        conn, _ws, backend = make_connection()
        result = await conn._dispatch_command("cwd", ["/new/path"])
        backend.create_session.assert_awaited_once()
        assert "cwd" in result

    @pytest.mark.asyncio
    async def test_cwd_command_passes_working_dir(self):
        """cwd command passes the new cwd to create_session."""
        conn, _ws, backend = make_connection()
        await conn._dispatch_command("cwd", ["/home/user/projects"])
        call_kwargs = backend.create_session.call_args.kwargs
        assert call_kwargs.get("working_dir") == "/home/user/projects"

    @pytest.mark.asyncio
    async def test_unknown_command_returns_error(self):
        """Unknown commands return an error dict with 'error' key."""
        conn, _ws, _backend = make_connection()
        result = await conn._dispatch_command("nonexistent", [])
        assert "error" in result

    @pytest.mark.asyncio
    async def test_bundle_command_no_args_returns_error(self):
        """bundle command with no args falls to unknown command path."""
        conn, _ws, _backend = make_connection()
        result = await conn._dispatch_command("bundle", [])
        # bundle without args doesn't match the 'bundle' if args case
        assert "error" in result

    @pytest.mark.asyncio
    async def test_cwd_command_no_args_returns_error(self):
        """cwd command with no args falls to unknown command path."""
        conn, _ws, _backend = make_connection()
        result = await conn._dispatch_command("cwd", [])
        assert "error" in result
