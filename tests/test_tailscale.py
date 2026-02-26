"""Tests for amplifier_distro.tailscale module."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from amplifier_distro import tailscale

# ---------------------------------------------------------------------------
# get_dns_name
# ---------------------------------------------------------------------------


class TestGetDnsName:
    """Tests for get_dns_name()."""

    def _mock_status(self, *, backend="Running", dns="host.tail1234.ts.net."):
        """Return a mock tailscale status JSON payload."""
        return json.dumps(
            {
                "BackendState": backend,
                "Self": {"DNSName": dns},
            }
        )

    def test_returns_dns_name_when_connected(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=self._mock_status(), stderr=""
        )
        with patch("amplifier_distro.tailscale.subprocess.run", return_value=result):
            assert tailscale.get_dns_name() == "host.tail1234.ts.net"

    def test_strips_trailing_dot(self):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=self._mock_status(dns="box.ts.net."),
            stderr="",
        )
        with patch("amplifier_distro.tailscale.subprocess.run", return_value=result):
            assert tailscale.get_dns_name() == "box.ts.net"

    def test_returns_none_when_not_running(self):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=self._mock_status(backend="Stopped"),
            stderr="",
        )
        with patch("amplifier_distro.tailscale.subprocess.run", return_value=result):
            assert tailscale.get_dns_name() is None

    def test_returns_none_on_nonzero_exit(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="not connected"
        )
        with patch("amplifier_distro.tailscale.subprocess.run", return_value=result):
            assert tailscale.get_dns_name() is None

    def test_returns_none_when_tailscale_not_installed(self):
        with patch(
            "amplifier_distro.tailscale.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            assert tailscale.get_dns_name() is None

    def test_returns_none_on_timeout(self):
        with patch(
            "amplifier_distro.tailscale.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=5),
        ):
            assert tailscale.get_dns_name() is None

    def test_returns_none_on_bad_json(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json", stderr=""
        )
        with patch("amplifier_distro.tailscale.subprocess.run", return_value=result):
            assert tailscale.get_dns_name() is None

    def test_returns_none_when_dns_empty(self):
        result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=self._mock_status(dns=""),
            stderr="",
        )
        with patch("amplifier_distro.tailscale.subprocess.run", return_value=result):
            assert tailscale.get_dns_name() is None


# ---------------------------------------------------------------------------
# start_serve
# ---------------------------------------------------------------------------


class TestStartServe:
    """Tests for start_serve()."""

    def test_returns_url_on_success(self):
        with (
            patch(
                "amplifier_distro.tailscale.get_dns_name",
                return_value="box.ts.net",
            ),
            patch(
                "amplifier_distro.tailscale.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="Serve started", stderr=""
                ),
            ) as mock_run,
        ):
            url = tailscale.start_serve(8400)
            assert url == "https://box.ts.net"
            mock_run.assert_called_once_with(
                ["tailscale", "serve", "--bg", "8400"],
                capture_output=True,
                text=True,
                timeout=30,
            )

    def test_returns_none_when_no_tailscale(self):
        with patch("amplifier_distro.tailscale.get_dns_name", return_value=None):
            assert tailscale.start_serve(8400) is None

    def test_returns_none_on_serve_failure(self):
        with (
            patch(
                "amplifier_distro.tailscale.get_dns_name",
                return_value="box.ts.net",
            ),
            patch(
                "amplifier_distro.tailscale.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="access denied"
                ),
            ),
        ):
            assert tailscale.start_serve(8400) is None

    def test_logs_warning_when_https_not_enabled(self):
        with (
            patch(
                "amplifier_distro.tailscale.get_dns_name",
                return_value="box.ts.net",
            ),
            patch(
                "amplifier_distro.tailscale.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="",
                    stderr="HTTPS not enabled on tailnet",
                ),
            ),
            patch("amplifier_distro.tailscale.logger") as mock_logger,
        ):
            assert tailscale.start_serve(8400) is None
            mock_logger.warning.assert_called_once()
            assert "not enabled" in mock_logger.warning.call_args[0][0].lower()

    def test_returns_none_on_timeout(self):
        with (
            patch(
                "amplifier_distro.tailscale.get_dns_name",
                return_value="box.ts.net",
            ),
            patch(
                "amplifier_distro.tailscale.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=30),
            ),
        ):
            assert tailscale.start_serve(8400) is None


# ---------------------------------------------------------------------------
# stop_serve
# ---------------------------------------------------------------------------


class TestStopServe:
    """Tests for stop_serve()."""

    def test_calls_tailscale_serve_off(self):
        with patch(
            "amplifier_distro.tailscale.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            ),
        ) as mock_run:
            tailscale.stop_serve()
            mock_run.assert_called_once_with(
                ["tailscale", "serve", "off"],
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_silent_when_tailscale_not_installed(self):
        with patch(
            "amplifier_distro.tailscale.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            tailscale.stop_serve()  # should not raise

    def test_silent_on_timeout(self):
        with patch(
            "amplifier_distro.tailscale.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tailscale", timeout=10),
        ):
            tailscale.stop_serve()  # should not raise


# ---------------------------------------------------------------------------
# _setup_tailscale in server CLI
# ---------------------------------------------------------------------------


class TestSetupTailscaleIntegration:
    """Tests for the _setup_tailscale helper in server/cli.py."""

    def test_setup_registers_atexit(self):
        import atexit as _atexit

        with (
            patch(
                "amplifier_distro.tailscale.start_serve",
                return_value="https://box.ts.net",
            ),
            patch.object(_atexit, "register") as mock_register,
        ):
            from amplifier_distro.server.cli import _setup_tailscale

            url = _setup_tailscale(8400)
            assert url == "https://box.ts.net"
            mock_register.assert_called_once()

    def test_setup_returns_none_when_no_tailscale(self):
        with patch("amplifier_distro.tailscale.start_serve", return_value=None):
            from amplifier_distro.server.cli import _setup_tailscale

            assert _setup_tailscale(8400) is None
