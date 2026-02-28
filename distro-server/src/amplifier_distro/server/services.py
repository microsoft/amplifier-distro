"""Server-level shared services.

The server creates ONE set of services at startup and shares them
with all apps. This replaces the pattern where each app created
its own backend independently.

Usage:
    # At server startup (in cli.py):
    from amplifier_distro.server.services import init_services
    services = init_services(dev_mode=True)

    # In app route handlers:
    from amplifier_distro.server.services import get_services
    services = get_services()
    info = await services.backend.create_session(...)

    # In tests:
    from amplifier_distro.server.services import init_services, reset_services
    services = init_services(backend=my_mock_backend)
    # ... run tests ...
    reset_services()
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from amplifier_distro.server.session_backend import (
    MockBackend,
    SessionBackend,
)

logger = logging.getLogger(__name__)

# Module-level singleton
_instance: ServerServices | None = None
_instance_lock = threading.Lock()


@dataclass
class ServerServices:
    """Shared services available to all server apps.

    This is the single point of truth for server-wide resources.
    Apps should NEVER create their own backend instances - they
    use the one provided here.

    Attributes:
        backend: Session backend (MockBackend or BridgeBackend)
        dev_mode: Whether the server is in dev/simulator mode
    """

    backend: SessionBackend
    dev_mode: bool = False
    # Extensible: add more shared services here as needed
    # (discovery, config, notification bus, etc.)
    _extras: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        """Access extra services by key."""
        return self._extras[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """Set extra services by key."""
        self._extras[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """Get extra service with default."""
        return self._extras.get(key, default)


def init_services(
    *,
    dev_mode: bool = False,
    backend: SessionBackend | None = None,
) -> ServerServices:
    """Initialize server services. Called once at server startup.

    Args:
        dev_mode: Use MockBackend (True) or BridgeBackend (False).
        backend: Override the backend (for testing).

    Returns:
        The initialized ServerServices instance.
    """
    global _instance

    if backend is None:
        if dev_mode:
            backend = MockBackend()
            logger.info("Server services: using MockBackend (dev mode)")
        else:
            # Late import - FoundationBackend requires amplifier-foundation
            from amplifier_distro.server.session_backend import FoundationBackend

            backend = FoundationBackend()
            logger.info("Server services: using FoundationBackend (production)")

    with _instance_lock:
        _instance = ServerServices(backend=backend, dev_mode=dev_mode)
        return _instance


def get_services() -> ServerServices:
    """Get the shared services instance.

    Raises RuntimeError if services haven't been initialized.
    """
    with _instance_lock:
        if _instance is None:
            raise RuntimeError(
                "Server services not initialized. Call init_services() first."
            )
        return _instance


def reset_services() -> None:
    """Reset services (for testing). Not for production use."""
    global _instance
    with _instance_lock:
        _instance = None


async def start_services() -> None:
    """Pre-warm the backend bundle at server startup.
    Called during FastAPI startup (wired in app.py via add_event_handler).
    Safe to call even if services haven't been initialized or if the backend
    doesn't implement startup() (e.g., MockBackend in dev mode).
    """
    with _instance_lock:
        instance = _instance

    if instance is None:
        return

    backend = instance.backend
    if hasattr(backend, "startup"):
        await backend.startup()


async def stop_services() -> None:
    """Gracefully stop shared services.

    Called during server shutdown.  Safe to call even if services were
    never initialized or if the backend doesn't implement stop().
    """
    with _instance_lock:
        instance = _instance

    if instance is None:
        return

    backend = instance.backend
    if hasattr(backend, "stop"):
        await backend.stop()
