"""Tests for server daemon lifecycle management and startup utilities.

Tests cover:
1. PID file creation, reading, and cleanup
2. Process liveness checking
3. Daemon start/stop/restart/status CLI commands (mocked subprocess)
4. Systemd service file validation (INI parsing)
5. Key export from keys.yaml
6. Structured logging setup
"""

import configparser
import json
import logging
import logging.handlers
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from amplifier_distro import conventions
from amplifier_distro.server.daemon import (
    cleanup_pid,
    is_running,
    pid_file_path,
    read_pid,
    server_dir,
    write_pid,
)
from amplifier_distro.server.startup import (
    JSONFormatter,
    export_keys,
    keys_file_path,
    log_file_path,
    log_startup_info,
    setup_logging,
)

# ---------------------------------------------------------------------------
# PID file management
# ---------------------------------------------------------------------------


class TestWritePid:
    """Verify write_pid creates a correct PID file."""

    def test_writes_current_pid_by_default(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        write_pid(pid_file)
        assert pid_file.read_text().strip() == str(os.getpid())

    def test_writes_explicit_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        write_pid(pid_file, pid=12345)
        assert pid_file.read_text().strip() == "12345"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "deep" / "nested" / "test.pid"
        write_pid(pid_file, pid=1)
        assert pid_file.exists()


class TestReadPid:
    """Verify read_pid handles all file states correctly."""

    def test_reads_valid_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("42")
        assert read_pid(pid_file) == 42

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert read_pid(tmp_path / "nonexistent.pid") is None

    def test_returns_none_for_invalid_content(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not-a-number")
        assert read_pid(pid_file) is None

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("  999  \n")
        assert read_pid(pid_file) == 999


class TestCleanupPid:
    """Verify cleanup_pid removes files safely."""

    def test_removes_existing_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("1")
        cleanup_pid(pid_file)
        assert not pid_file.exists()

    def test_no_error_on_missing_file(self, tmp_path: Path) -> None:
        # Should not raise
        cleanup_pid(tmp_path / "nonexistent.pid")


class TestIsRunning:
    """Verify process liveness checking."""

    def test_returns_true_for_current_process(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        write_pid(pid_file)
        assert is_running(pid_file) is True

    def test_returns_false_for_dead_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        # Use a PID that almost certainly doesn't exist
        pid_file.write_text("4999999")
        assert is_running(pid_file) is False

    def test_returns_false_for_missing_file(self, tmp_path: Path) -> None:
        assert is_running(tmp_path / "nonexistent.pid") is False


# ---------------------------------------------------------------------------
# Path construction from conventions
# ---------------------------------------------------------------------------


class TestPathConstruction:
    """Verify paths are built from conventions, not hardcoded."""

    def test_server_dir_uses_conventions(self) -> None:
        d = server_dir()
        assert d.name == conventions.SERVER_DIR
        assert d.parent == Path(conventions.AMPLIFIER_HOME).expanduser()

    def test_pid_file_path_uses_conventions(self) -> None:
        p = pid_file_path()
        assert p.name == conventions.SERVER_PID_FILE
        assert p.parent.name == conventions.SERVER_DIR

    def test_log_file_path_uses_conventions(self) -> None:
        p = log_file_path()
        assert p.name == conventions.SERVER_LOG_FILE
        assert p.parent.name == conventions.SERVER_DIR

    def test_keys_file_path_uses_conventions(self) -> None:
        p = keys_file_path()
        assert p.name == conventions.KEYS_FILENAME


# ---------------------------------------------------------------------------
# CLI subcommands (mocked)
# ---------------------------------------------------------------------------


class TestStartCommand:
    """Verify the 'start' subcommand spawns a daemon."""

    @patch("amplifier_distro.server.daemon.subprocess.Popen")
    def test_start_spawns_process(self, mock_popen: MagicMock, tmp_path: Path) -> None:
        mock_process = MagicMock()
        mock_process.pid = 54321
        mock_popen.return_value = mock_process

        pid_file = tmp_path / "test.pid"

        # Create the server dir so crash log can be opened
        srv_dir = tmp_path / "server"
        srv_dir.mkdir()

        with (
            patch(
                "amplifier_distro.server.daemon.pid_file_path",
                return_value=pid_file,
            ),
            patch(
                "amplifier_distro.server.daemon.server_dir",
                return_value=srv_dir,
            ),
            patch(
                "amplifier_distro.server.daemon.is_port_in_use",
                return_value=False,
            ),
            patch(
                "amplifier_distro.server.daemon.wait_for_health",
                return_value=True,
            ),
            patch(
                "amplifier_distro.server.startup.load_env_file",
                return_value=[],
            ),
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["start"])

        assert result.exit_code == 0
        assert "54321" in result.output

    def test_start_rejects_when_already_running(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        # Write current PID so is_running returns True
        write_pid(pid_file)

        with patch(
            "amplifier_distro.server.daemon.pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["start"])

        assert result.exit_code != 0
        assert "already running" in result.output


class TestStopCommand:
    """Verify the 'stop' subcommand sends signals correctly."""

    def test_stop_no_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"

        with patch(
            "amplifier_distro.server.daemon.pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["stop"])

        assert result.exit_code == 0
        assert "No PID file" in result.output


class TestStatusCommand:
    """Verify the 'status' subcommand reports correctly."""

    def test_status_not_running(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"

        with patch(
            "amplifier_distro.server.daemon.pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["status"])

        assert result.exit_code == 0
        assert "not running" in result.output

    def test_status_stale_pid_cleaned(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("4999999")  # Dead PID

        with patch(
            "amplifier_distro.server.daemon.pid_file_path",
            return_value=pid_file,
        ):
            from amplifier_distro.server.cli import serve

            runner = CliRunner()
            result = runner.invoke(serve, ["status"])

        assert "stale PID" in result.output
        assert not pid_file.exists()


# ---------------------------------------------------------------------------
# Systemd service file validation
# ---------------------------------------------------------------------------


class TestSystemdServiceFile:
    """Verify the systemd service file is valid and complete."""

    @pytest.fixture
    def service_path(self, project_root: Path) -> Path:
        return project_root / "scripts" / "amplifier-distro.service"

    def test_service_file_exists(self, service_path: Path) -> None:
        assert service_path.exists()

    def test_service_file_is_valid_ini(self, service_path: Path) -> None:
        parser = configparser.ConfigParser()
        parser.read(str(service_path))
        assert "Unit" in parser
        assert "Service" in parser
        assert "Install" in parser

    def test_service_has_restart_on_failure(self, service_path: Path) -> None:
        parser = configparser.ConfigParser()
        parser.read(str(service_path))
        assert parser["Service"]["Restart"] == "on-failure"

    def test_service_has_after_network(self, service_path: Path) -> None:
        parser = configparser.ConfigParser()
        parser.read(str(service_path))
        assert "network.target" in parser["Unit"]["After"]

    def test_service_has_execstart(self, service_path: Path) -> None:
        parser = configparser.ConfigParser()
        parser.read(str(service_path))
        assert "amp-distro" in parser["Service"]["ExecStart"]
        assert "serve" in parser["Service"]["ExecStart"]


# ---------------------------------------------------------------------------
# Key export logic
# ---------------------------------------------------------------------------


class TestKeyExport:
    """Verify keys.env export into environment variables."""

    def test_exports_keys_from_env_file(self, tmp_path: Path) -> None:
        keys_file = tmp_path / "keys.env"
        keys_file.write_text(
            'TEST_DAEMON_KEY_A="value-a"\nTEST_DAEMON_KEY_B="value-b"\n'
        )
        # Ensure keys are not already set
        os.environ.pop("TEST_DAEMON_KEY_A", None)
        os.environ.pop("TEST_DAEMON_KEY_B", None)

        try:
            exported = export_keys(keys_file)
            assert "TEST_DAEMON_KEY_A" in exported
            assert "TEST_DAEMON_KEY_B" in exported
            assert os.environ["TEST_DAEMON_KEY_A"] == "value-a"
            assert os.environ["TEST_DAEMON_KEY_B"] == "value-b"
        finally:
            os.environ.pop("TEST_DAEMON_KEY_A", None)
            os.environ.pop("TEST_DAEMON_KEY_B", None)

    def test_does_not_override_existing_env(self, tmp_path: Path) -> None:
        keys_file = tmp_path / "keys.env"
        keys_file.write_text('TEST_DAEMON_KEY_C="from-env-file"\n')
        os.environ["TEST_DAEMON_KEY_C"] = "from-env"

        try:
            export_keys(keys_file)
            assert os.environ["TEST_DAEMON_KEY_C"] == "from-env"
        finally:
            os.environ.pop("TEST_DAEMON_KEY_C", None)

    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        assert export_keys(tmp_path / "nonexistent.env") == []

    def test_skips_comments_and_blank_lines(self, tmp_path: Path) -> None:
        keys_file = tmp_path / "keys.env"
        keys_file.write_text("# comment\n\nno_equals_here\n")
        exported = export_keys(keys_file)
        assert exported == []


# ---------------------------------------------------------------------------
# Structured logging setup
# ---------------------------------------------------------------------------


class TestStructuredLogging:
    """Verify structured logging creates both handlers correctly."""

    def test_setup_creates_log_file(self, tmp_path: Path) -> None:
        log_file = tmp_path / "test.log"
        # Clear existing handlers so the idempotency guard doesn't skip
        root = logging.getLogger()
        root.handlers.clear()
        setup_logging(log_file=log_file)
        root = logging.getLogger()
        try:
            # Find the file handler we just added
            file_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.FileHandler)
                and Path(h.baseFilename) == log_file
            ]
            assert len(file_handlers) >= 1
        finally:
            # Clean up handlers to avoid test pollution
            for h in list(root.handlers):
                if (
                    isinstance(h, logging.FileHandler)
                    and Path(h.baseFilename) == log_file
                ):
                    root.removeHandler(h)
                    h.close()
            # Also remove any StreamHandlers we added
            for h in list(root.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(
                    h, logging.FileHandler
                ):
                    root.removeHandler(h)

    def test_json_formatter_produces_valid_json(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["logger"] == "test"
        assert data["message"] == "hello world"
        assert "timestamp" in data

    def test_json_formatter_includes_exception(self) -> None:
        formatter = JSONFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="error occurred",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "exception" in data
        assert "ValueError" in data["exception"]


class TestLogStartupInfo:
    """Verify startup info logging."""

    def test_logs_version_and_port(self) -> None:
        logger = logging.getLogger("test_startup_info")
        logger.handlers.clear()
        handler = logging.handlers.MemoryHandler(capacity=100)
        logger.addHandler(handler)

        log_startup_info(
            host="127.0.0.1",
            port=8400,
            apps=["example", "web-chat"],
            dev_mode=False,
            logger=logger,
        )

        messages = [h.getMessage() for h in handler.buffer]
        assert any("8400" in m for m in messages)
        assert any("example" in m for m in messages)

        logger.removeHandler(handler)
