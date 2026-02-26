"""Tailscale remote-access integration for amplifier-distro.

Auto-detects Tailscale and sets up HTTPS reverse proxy via ``tailscale serve``.
No configuration needed -- if Tailscale is connected, it just works.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess

logger = logging.getLogger(__name__)


def get_dns_name() -> str | None:
    """Get the MagicDNS name if Tailscale is connected.

    Returns e.g. ``"win-dlpodl2cijb.tail79ce67.ts.net"`` or ``None``.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        if data.get("BackendState") != "Running":
            return None

        dns = data.get("Self", {}).get("DNSName", "").rstrip(".")
        return dns or None

    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def start_serve(port: int) -> str | None:
    """Start ``tailscale serve`` to proxy HTTPS -> localhost:port.

    Returns the HTTPS URL on success, or ``None`` on any failure.
    Failures are logged but never raise -- the server starts regardless.
    """
    dns_name = get_dns_name()
    if dns_name is None:
        return None

    try:
        result = subprocess.run(
            ["tailscale", "serve", "--bg", str(port)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "not enabled" in stderr.lower() or "enable" in stderr.lower():
                logger.warning(
                    "Tailscale Serve not enabled on tailnet. "
                    "Enable HTTPS in Tailscale admin: "
                    "https://login.tailscale.com/admin/dns"
                )
            else:
                logger.warning("tailscale serve failed: %s", stderr)
            return None

        url = f"https://{dns_name}"
        logger.info("Tailscale HTTPS active: %s -> localhost:%d", url, port)
        return url

    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.debug("tailscale serve unavailable: %s", exc)
        return None


def stop_serve() -> None:
    """Tear down ``tailscale serve``. Idempotent -- safe if not serving."""
    with contextlib.suppress(FileNotFoundError, subprocess.TimeoutExpired):
        subprocess.run(
            ["tailscale", "serve", "off"],
            capture_output=True,
            text=True,
            timeout=10,
        )
