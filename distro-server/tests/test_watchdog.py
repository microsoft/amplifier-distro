"""Tests for server watchdog: health monitoring and automatic restart.

Tests cover:
1. Health check function (mocked HTTP)
2. Watchdog path construction from conventions
3. Watchdog loop logic (mocked time and daemon calls)
4. Watchdog start/stop process management
5. Watchdog CLI subcommands (mocked)
"""

import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from amplifier_distro import conventions
from amplifier_distro.server.daemon import write_pid
from amplifier_distro.server.watchdog import (
    check_health,
    is_watchdog_running,
    start_watchdog,
    stop_watchdog,
    watchdog_log_file_path,
    watchdog_pid_file_path,
)

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestCheckHealth:
    """Verify HTTP health check function."""

    @patch("amplifier_distro.server.watchdog.urllib.request.urlopen")
    def test_returns_true_for_200(self, mock_urlopen: MagicMock) -> None:
        """Health check succeeds when endpoint returns 200."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value = mock_resp
        assert check_health("http://127.0.0.1:8400/api/health") is True

    @patch("amplifier_distro.server.watchdog.urllib.request.urlopen")
    def test_returns_false_for_non_200(self, mock_urlopen: MagicMock) -> None:
        """Health check fails for non-200 status codes."""
        mock_resp = MagicMock()
        mock_resp.status = 503
        mock_urlopen.return_value = mock_resp
        assert check_health("http://127.0.0.1:8400/api/health") is False

    @patch("amplifier_distro.server.watchdog.urllib.request.urlopen")
    def test_returns_false_on_connection_error(self, mock_urlopen: MagicMock) -> None:
        """Health check fails when server is unreachable."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        assert check_health("http://127.0.0.1:8400/api/health") is False

    @patch("amplifier_distro.server.watchdog.urllib.request.urlopen")
    def test_returns_false_on_timeout(self, mock_urlopen: MagicMock) -> None:
        """Health check fails on socket timeout."""
        mock_urlopen.side_effect = OSError("timed out")
        assert check_health("http://127.0.0.1:8400/api/health") is False

    def test_returns_false_for_invalid_url(self) -> None:
        """Health check fails gracefully for malformed URL."""
        assert check_health("not-a-url") is False


# ---------------------------------------------------------------------------
# Path construction from conventions
# ---------------------------------------------------------------------------


class TestWatchdogPaths:
    """Verify watchdog paths are built from conventions, not hardcoded."""

    def test_watchdog_pid_file_uses_conventions(self) -> None:
        p = watchdog_pid_file_path()
        assert p.name == conventions.WATCHDOG_PID_FILE
        assert p.parent.name == conventions.SERVER_DIR

    def test_watchdog_log_file_uses_conventions(self) -> None:
        p = watchdog_log_file_path()
        assert p.name == conventions.WATCHDOG_LOG_FILE
        assert p.parent.name == conventions.SERVER_DIR

    def test_watchdog_pid_is_sibling_of_server_pid(self) -> None:
        """Watchdog PID file lives in the same directory as the server PID file."""
        from amplifier_distro.server.daemon import pid_file_path

        assert watchdog_pid_file_path().parent == pid_file_path().parent


# ---------------------------------------------------------------------------
# Watchdog loop logic
# ---------------------------------------------------------------------------


class TestWatchdogLoop:
    """Verify watchdog monitoring and restart logic.

    All tests mock time.monotonic, time.sleep, and daemon functions to
    exercise the loop logic without real delays or processes.
    """

    @patch("amplifier_distro.server.watchdog.cleanup_pid")
    @patch("amplifier_distro.server.watchdog.write_pid")
    @patch("amplifier_distro.server.watchdog._restart_server")
    @patch("amplifier_distro.server.watchdog.check_health")
    @patch("amplifier_distro.server.watchdog.time.monotonic")
    @patch("amplifier_distro.server.watchdog.time.sleep")
    def test_restarts_after_threshold(
        self,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
        mock_health: MagicMock,
        mock_restart: MagicMock,
        mock_write_pid: MagicMock,
        mock_cleanup: MagicMock,
    ) -> None:
        """Server is restarted after restart_after seconds of sustained downtime."""
        # All health checks fail
        mock_health.return_value = False

        # Simulate time: first_failure at t=100, then check at t=200
        # (100s > 15s threshold)
        # monotonic calls: first_failure_time=100, elapsed check=200, reset (None),
        # then max_restarts=1 reached on second cycle
        mock_monotonic.side_effect = [100, 200, 300, 400]
        mock_sleep.return_value = None

        from amplifier_distro.server.watchdog import run_watchdog_loop

        run_watchdog_loop(
            check_interval=1,
            restart_after=15,
            max_restarts=1,
        )

        mock_restart.assert_called_once()

    @patch("amplifier_distro.server.watchdog.cleanup_pid")
    @patch("amplifier_distro.server.watchdog.write_pid")
    @patch("amplifier_distro.server.watchdog._restart_server")
    @patch("amplifier_distro.server.watchdog.check_health")
    @patch("amplifier_distro.server.watchdog.time.monotonic")
    @patch("amplifier_distro.server.watchdog.time.sleep")
    def test_resets_on_recovery(
        self,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
        mock_health: MagicMock,
        mock_restart: MagicMock,
        mock_write_pid: MagicMock,
        mock_cleanup: MagicMock,
    ) -> None:
        """Failure timer resets when server recovers before threshold."""
        # fail, fail, success, fail, fail -- then KeyboardInterrupt to exit
        mock_health.side_effect = [
            False,
            False,
            True,
            False,
            False,
            KeyboardInterrupt(),
        ]
        # monotonic calls:
        # 1. first fail sets first_failure_time = 100
        # 2. second fail: elapsed = 110 - 100 = 10 (<300)
        # 3. success: recovery elapsed = 120 - 100 = 20, resets timer
        # 4. new fail sets first_failure_time = 200
        # 5. second fail: elapsed = 210 - 200 = 10 (<300)
        mock_monotonic.side_effect = [100, 110, 120, 200, 210]
        mock_sleep.return_value = None

        from amplifier_distro.server.watchdog import run_watchdog_loop

        try:
            run_watchdog_loop(check_interval=1, restart_after=300, max_restarts=5)
        except KeyboardInterrupt:
            pass

        mock_restart.assert_not_called()

    @patch("amplifier_distro.server.watchdog.cleanup_pid")
    @patch("amplifier_distro.server.watchdog.write_pid")
    @patch("amplifier_distro.server.watchdog._restart_server")
    @patch("amplifier_distro.server.watchdog.check_health")
    @patch("amplifier_distro.server.watchdog.time.monotonic")
    @patch("amplifier_distro.server.watchdog.time.sleep")
    def test_stops_after_max_restarts(
        self,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
        mock_health: MagicMock,
        mock_restart: MagicMock,
        mock_write_pid: MagicMock,
        mock_cleanup: MagicMock,
    ) -> None:
        """Watchdog exits when max_restarts is exhausted."""
        mock_health.return_value = False
        mock_sleep.return_value = None
        # Each pair: first_failure_time, then elapsed check exceeds threshold
        mock_monotonic.side_effect = [0, 100, 200, 300, 400, 500, 600, 700]

        from amplifier_distro.server.watchdog import run_watchdog_loop

        run_watchdog_loop(check_interval=1, restart_after=1, max_restarts=3)

        assert mock_restart.call_count == 3

    @patch("amplifier_distro.server.watchdog.cleanup_pid")
    @patch("amplifier_distro.server.watchdog.write_pid")
    @patch("amplifier_distro.server.watchdog._restart_server")
    @patch("amplifier_distro.server.watchdog.check_health")
    @patch("amplifier_distro.server.watchdog.time.monotonic")
    @patch("amplifier_distro.server.watchdog.time.sleep")
    def test_healthy_server_no_restarts(
        self,
        mock_sleep: MagicMock,
        mock_monotonic: MagicMock,
        mock_health: MagicMock,
        mock_restart: MagicMock,
        mock_write_pid: MagicMock,
        mock_cleanup: MagicMock,
    ) -> None:
        """A consistently healthy server is never restarted."""
        # 3 healthy checks then KeyboardInterrupt
        mock_health.side_effect = [True, True, True, KeyboardInterrupt()]
        mock_sleep.return_value = None

        from amplifier_distro.server.watchdog import run_watchdog_loop

        try:
            run_watchdog_loop(check_interval=1, restart_after=300, max_restarts=5)
        except KeyboardInterrupt:
            pass

        mock_restart.assert_not_called()


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


class TestStartWatchdog:
    """Verify watchdog background process spawning."""

    @patch("amplifier_distro.server.watchdog.subprocess.Popen")
    def test_spawns_background_process(
        self, mock_popen: MagicMock, tmp_path: Path
    ) -> None:
        """start_watchdog returns the PID and writes a PID file."""
        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_popen.return_value = mock_process
        pid_file = tmp_path / "watchdog.pid"

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=pid_file,
        ):
            pid = start_watchdog()

        assert pid == 99999
        assert pid_file.exists()
        assert pid_file.read_text().strip() == "99999"
        # Verify Popen was called with start_new_session
        call_kwargs = mock_popen.call_args
        assert call_kwargs.kwargs.get("start_new_session") is True

    @patch("amplifier_distro.server.watchdog.subprocess.Popen")
    def test_passes_all_options(self, mock_popen: MagicMock, tmp_path: Path) -> None:
        """start_watchdog passes all options through to the command."""
        mock_process = MagicMock()
        mock_process.pid = 11111
        mock_popen.return_value = mock_process

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=tmp_path / "watchdog.pid",
        ):
            start_watchdog(
                host="0.0.0.0",
                port=9000,
                check_interval=60,
                restart_after=600,
                max_restarts=10,
                apps_dir="/tmp/apps",
                dev=True,
            )

        cmd = mock_popen.call_args[0][0]
        assert "--host" in cmd
        assert "0.0.0.0" in cmd
        assert "--port" in cmd
        assert "9000" in cmd
        assert "--check-interval" in cmd
        assert "60" in cmd
        assert "--restart-after" in cmd
        assert "600" in cmd
        assert "--max-restarts" in cmd
        assert "10" in cmd
        assert "--apps-dir" in cmd
        assert "/tmp/apps" in cmd
        assert "--dev" in cmd


class TestStopWatchdog:
    """Verify watchdog stopping."""

    def test_returns_false_for_no_pid_file(self, tmp_path: Path) -> None:
        """stop_watchdog returns False when no PID file exists."""
        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=tmp_path / "nonexistent.pid",
        ):
            assert stop_watchdog() is False


class TestIsWatchdogRunning:
    """Verify watchdog liveness checking."""

    def test_returns_true_for_live_process(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "watchdog.pid"
        write_pid(pid_file)  # Write current process PID

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=pid_file,
        ):
            assert is_watchdog_running() is True

    def test_returns_false_for_no_pid_file(self, tmp_path: Path) -> None:
        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=tmp_path / "nonexistent.pid",
        ):
            assert is_watchdog_running() is False

    def test_returns_false_for_dead_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "watchdog.pid"
        pid_file.write_text("4999999")

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=pid_file,
        ):
            assert is_watchdog_running() is False


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


class TestWatchdogCli:
    """Verify watchdog CLI subcommands via CliRunner."""

    def test_watchdog_status_not_running(self, tmp_path: Path) -> None:
        """'watchdog status' reports when no watchdog is running."""
        pid_file = tmp_path / "watchdog.pid"

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["watchdog", "status"])

        assert result.exit_code == 0
        assert "not running" in result.output

    def test_watchdog_status_stale_pid_cleaned(self, tmp_path: Path) -> None:
        """'watchdog status' cleans up stale PID file."""
        pid_file = tmp_path / "watchdog.pid"
        pid_file.write_text("4999999")  # Dead PID

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["watchdog", "status"])

        assert "stale PID" in result.output
        assert not pid_file.exists()

    @patch("amplifier_distro.server.watchdog.subprocess.Popen")
    def test_watchdog_start(self, mock_popen: MagicMock, tmp_path: Path) -> None:
        """'watchdog start' spawns the watchdog and reports PID."""
        mock_process = MagicMock()
        mock_process.pid = 88888
        mock_popen.return_value = mock_process
        pid_file = tmp_path / "watchdog.pid"

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["watchdog", "start"])

        assert result.exit_code == 0
        assert "88888" in result.output
        assert "Monitoring" in result.output

    def test_watchdog_start_rejects_when_running(self, tmp_path: Path) -> None:
        """'watchdog start' fails if watchdog is already running."""
        pid_file = tmp_path / "watchdog.pid"
        write_pid(pid_file)  # Current process = "running"

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["watchdog", "start"])

        assert result.exit_code != 0
        assert "already running" in result.output

    def test_watchdog_stop_no_pid(self, tmp_path: Path) -> None:
        """'watchdog stop' reports when no PID file exists."""
        pid_file = tmp_path / "watchdog.pid"

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["watchdog", "stop"])

        assert result.exit_code == 0
        assert "No watchdog PID file" in result.output

    def test_watchdog_default_shows_status(self, tmp_path: Path) -> None:
        """'watchdog' without subcommand shows status."""
        pid_file = tmp_path / "watchdog.pid"

        with patch(
            "amplifier_distro.server.watchdog.watchdog_pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["watchdog"])

        assert result.exit_code == 0
        assert "not running" in result.output
