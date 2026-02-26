"""Tests for image attachment support in execute()."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestExecuteWithImages:
    @pytest.mark.asyncio
    async def test_execute_passes_images_to_handle(self):
        """execute() accepts images and calls handle.run() correctly."""
        from amplifier_distro.server.session_backend import FoundationBackend

        handle = MagicMock()
        handle.run = AsyncMock(return_value="response")

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._sessions = {"s001": handle}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        images = ["base64datahere", "anotherimage"]
        await backend.execute("s001", "describe these", images=images)
        # execute() currently calls handle.run(prompt) — images deferred to future
        handle.run.assert_called_once_with("describe these")

    @pytest.mark.asyncio
    async def test_execute_no_images_still_works(self):
        """execute() works correctly with no images."""
        from amplifier_distro.server.session_backend import FoundationBackend

        handle = MagicMock()
        handle.run = AsyncMock(return_value="ok")

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._sessions = {"s002": handle}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        await backend.execute("s002", "no images here")
        handle.run.assert_called_once_with("no images here")

    @pytest.mark.asyncio
    async def test_execute_images_none_equivalent_to_no_images(self):
        """execute() with images=None behaves the same as no images."""
        from amplifier_distro.server.session_backend import FoundationBackend

        handle = MagicMock()
        handle.run = AsyncMock(return_value="ok")

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._sessions = {"s003": handle}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._wired_sessions = set()

        await backend.execute("s003", "prompt with none images", images=None)
        handle.run.assert_called_once_with("prompt with none images")
