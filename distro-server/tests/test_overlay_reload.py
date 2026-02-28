"""Tests: _write_overlay() triggers backend.reload_bundle() when server is running.

These tests verify that overlay writes schedule a live bundle reload through
the services backend — and that the call is safely skipped when services are
not initialized or when the backend doesn't implement reload_bundle().
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from amplifier_distro.overlay import _write_overlay
from amplifier_distro.server.services import (
    init_services,
    reset_services,
)


class TestOverlayWriteTriggersReload:
    """_write_overlay() must schedule backend.reload_bundle() when running."""

    @pytest.mark.anyio
    async def test_write_overlay_schedules_reload_when_services_available(
        self, tmp_path, monkeypatch
    ):
        """Writing the overlay must schedule reload_bundle() on the backend."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "amplifier_distro.overlay.DISTRO_OVERLAY_DIR",
            str(tmp_path / "bundle"),
        )

        mock_backend = AsyncMock()
        mock_backend.reload_bundle = AsyncMock()

        reset_services()
        init_services(backend=mock_backend)

        _write_overlay({"bundle": {"name": "test"}})

        # Yield to the event loop twice so create_task() coroutines can run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        mock_backend.reload_bundle.assert_awaited_once()

    def test_write_overlay_safe_when_no_services_initialized(
        self, tmp_path, monkeypatch
    ):
        """_write_overlay() must not raise when services haven't been initialized."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "amplifier_distro.overlay.DISTRO_OVERLAY_DIR",
            str(tmp_path / "bundle"),
        )

        reset_services()

        # Must NOT raise RuntimeError
        _write_overlay({"bundle": {"name": "test"}})

    @pytest.mark.anyio
    async def test_write_overlay_safe_when_backend_has_no_reload_bundle(
        self, tmp_path, monkeypatch
    ):
        """_write_overlay() must not raise when the backend has no reload_bundle()."""
        from amplifier_distro.server.session_backend import MockBackend

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "amplifier_distro.overlay.DISTRO_OVERLAY_DIR",
            str(tmp_path / "bundle"),
        )

        reset_services()
        init_services(backend=MockBackend())

        # Must NOT raise
        _write_overlay({"bundle": {"name": "test"}})

        # Yield to event loop
        await asyncio.sleep(0)
        await asyncio.sleep(0)
