"""Shared test fixtures for amplifier-distro acceptance tests."""

import asyncio
import contextlib
from pathlib import Path

import httpx
import pytest

from amplifier_distro.server.services import init_services, reset_services


@pytest.fixture
def project_root():
    """Return the project root directory."""
    return Path(__file__).parent.parent


@pytest.fixture
def src_root(project_root):
    """Return the src/ directory."""
    return project_root / "src"


@pytest.fixture
async def async_webchat_client():
    """Async httpx client wired directly to the web-chat ASGI app."""
    import amplifier_distro.server.apps.web_chat as wc
    from amplifier_distro.server.app import DistroServer
    from amplifier_distro.server.apps.web_chat import manifest

    wc._active_session_id = None
    wc._session_lock = asyncio.Lock()
    if hasattr(wc, "_message_in_flight"):
        wc._message_in_flight = False

    reset_services()
    init_services(dev_mode=True)

    server = DistroServer()
    server.register_app(manifest)

    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    reset_services()


@pytest.fixture(autouse=True)
async def _cancel_stray_tasks():
    """Cancel any tasks that leaked from a test."""
    yield
    await asyncio.sleep(0)
    current = asyncio.current_task()
    for task in asyncio.all_tasks():
        if task is not current and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
