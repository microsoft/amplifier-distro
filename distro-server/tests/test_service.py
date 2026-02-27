"""Tests for platform service registration.

Tests cover:
1. Platform detection
2. Systemd unit file generation and INI validation
3. Launchd plist generation and XML validation
4. Install/uninstall dispatch (mocked subprocess)
5. Service status checking
6. Service CLI subcommands (mocked)
"""

import configparser
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from amplifier_distro import conventions
from amplifier_distro.service import (
    ServiceResult,
    detect_platform,
    install_service,
    service_status,
    uninstall_service,
)

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


class TestDetectPlatform:
    """Verify platform detection returns correct platform strings."""

    @patch("amplifier_distro.service.platform.system", return_value="Linux")
    def test_detects_linux(self, _mock: MagicMock) -> None:
        assert detect_platform() == "linux"

    @patch("amplifier_distro.service.platform.system", return_value="Darwin")
    def test_detects_macos(self, _mock: MagicMock) -> None:
        assert detect_platform() == "macos"

    @patch("amplifier_distro.service.platform.system", return_value="Windows")
    def test_windows_returns_unsupported(self, _mock: MagicMock) -> None:
        assert detect_platform() == "unsupported"

    @patch("amplifier_distro.service.platform.system", return_value="FreeBSD")
    def test_unknown_returns_unsupported(self, _mock: MagicMock) -> None:
        assert detect_platform() == "unsupported"


# ---------------------------------------------------------------------------
# Systemd unit generation
# ---------------------------------------------------------------------------


class TestSystemdServerUnit:
    """Verify systemd server unit file generation."""

    def _generate(
        self,
        distro_bin: str = "/usr/local/bin/amp-distro",
    ) -> str:
        from amplifier_distro.service import _generate_systemd_server_unit

        return _generate_systemd_server_unit(distro_bin)

    def _parse(self, content: str) -> configparser.ConfigParser:
        parser = configparser.ConfigParser()
        parser.read_string(content)
        return parser

    def test_valid_ini(self) -> None:
        """Generated unit is valid INI with all required sections."""
        parser = self._parse(self._generate())
        assert "Unit" in parser
        assert "Service" in parser
        assert "Install" in parser

    def test_restart_on_failure(self) -> None:
        parser = self._parse(self._generate())
        assert parser["Service"]["Restart"] == "on-failure"

    def test_after_network(self) -> None:
        parser = self._parse(self._generate())
        assert "network.target" in parser["Unit"]["After"]

    def test_correct_exec_start(self) -> None:
        content = self._generate("/my/custom/path/amp-distro")
        assert "/my/custom/path/amp-distro" in content
        assert "serve" in content
        assert "amp-distro-server" not in content

    def test_has_environment_path(self) -> None:
        content = self._generate()
        assert "Environment" in content
        assert "PATH=" in content

    def test_default_port(self) -> None:
        content = self._generate()
        assert str(conventions.SERVER_DEFAULT_PORT) in content

    def test_wanted_by_default_target(self) -> None:
        parser = self._parse(self._generate())
        assert parser["Install"]["WantedBy"] == "default.target"


class TestSystemdWatchdogUnit:
    """Verify systemd watchdog unit file generation."""

    def _generate(
        self,
        distro_bin: str = "/usr/local/bin/amp-distro",
    ) -> str:
        from amplifier_distro.service import _generate_systemd_watchdog_unit

        return _generate_systemd_watchdog_unit(distro_bin)

    def _parse(self, content: str) -> configparser.ConfigParser:
        parser = configparser.ConfigParser()
        parser.read_string(content)
        return parser

    def test_valid_ini(self) -> None:
        parser = self._parse(self._generate())
        assert "Unit" in parser
        assert "Service" in parser
        assert "Install" in parser

    def test_restart_always(self) -> None:
        """Watchdog service must always restart -- it should never stay dead."""
        parser = self._parse(self._generate())
        assert parser["Service"]["Restart"] == "always"

    def test_depends_on_server(self) -> None:
        """Watchdog unit must declare After and Wants on the server unit."""
        parser = self._parse(self._generate())
        assert conventions.SERVICE_NAME in parser["Unit"]["After"]
        assert conventions.SERVICE_NAME in parser["Unit"]["Wants"]

    def test_runs_watchdog_subcommand(self) -> None:
        content = self._generate()
        assert "watchdog" in content
        assert "amplifier_distro.server.watchdog" not in content
        assert "-m" not in content

    def test_has_environment_path(self) -> None:
        content = self._generate()
        assert "PATH=" in content


# ---------------------------------------------------------------------------
# Launchd plist generation
# ---------------------------------------------------------------------------


class TestLaunchdServerPlist:
    """Verify launchd server plist generation."""

    def _generate(
        self,
        distro_bin: str = "/usr/local/bin/amp-distro",
    ) -> str:
        from amplifier_distro.service import _generate_launchd_server_plist

        return _generate_launchd_server_plist(distro_bin)

    def test_valid_xml(self) -> None:
        """Generated plist must parse as valid XML."""
        ET.fromstring(self._generate())

    def test_correct_label(self) -> None:
        content = self._generate()
        assert conventions.LAUNCHD_LABEL in content

    def test_run_at_load(self) -> None:
        content = self._generate()
        assert "RunAtLoad" in content

    def test_correct_program(self) -> None:
        content = self._generate("/my/path/amp-distro")
        assert "/my/path/amp-distro" in content
        assert "<string>serve</string>" in content
        assert "amp-distro-server" not in content

    def test_keep_alive(self) -> None:
        content = self._generate()
        assert "KeepAlive" in content

    def test_default_port(self) -> None:
        content = self._generate()
        assert str(conventions.SERVER_DEFAULT_PORT) in content

    def test_has_environment_path(self) -> None:
        content = self._generate()
        assert "PATH" in content


class TestLaunchdWatchdogPlist:
    """Verify launchd watchdog plist generation."""

    def _generate(self, distro_bin: str = "/usr/local/bin/amp-distro") -> str:
        from amplifier_distro.service import (
            _generate_launchd_watchdog_plist,
        )

        return _generate_launchd_watchdog_plist(distro_bin)

    def test_valid_xml(self) -> None:
        ET.fromstring(self._generate())

    def test_watchdog_label(self) -> None:
        content = self._generate()
        assert f"{conventions.LAUNCHD_LABEL}.watchdog" in content

    def test_runs_watchdog_subcommand(self) -> None:
        content = self._generate()
        assert "<string>watchdog</string>" in content
        assert "amplifier_distro.server.watchdog" not in content
        assert "<string>-m</string>" not in content

    def test_keep_alive_true(self) -> None:
        """Watchdog agent must use KeepAlive=true (always running)."""
        content = self._generate()
        assert "KeepAlive" in content

    def test_correct_distro_bin(self) -> None:
        content = self._generate("/my/custom/amp-distro")
        assert "/my/custom/amp-distro" in content


# ---------------------------------------------------------------------------
# Install/uninstall dispatch
# ---------------------------------------------------------------------------


class TestInstallDispatch:
    """Verify install_service dispatches to the correct platform handler."""

    @patch(
        "amplifier_distro.service.detect_platform",
        return_value="unsupported",
    )
    def test_unsupported_platform_returns_failure(self, _mock: MagicMock) -> None:
        result = install_service()
        assert result.success is False
        assert "Unsupported" in result.message

    @patch("amplifier_distro.service.detect_platform", return_value="linux")
    @patch("amplifier_distro.service._install_systemd")
    def test_linux_delegates_to_systemd(
        self, mock_install: MagicMock, _mock_plat: MagicMock
    ) -> None:
        mock_install.return_value = ServiceResult(
            success=True, platform="linux", message="OK"
        )
        install_service(include_watchdog=True)
        mock_install.assert_called_once_with(True)

    @patch("amplifier_distro.service.detect_platform", return_value="macos")
    @patch("amplifier_distro.service._install_launchd")
    def test_macos_delegates_to_launchd(
        self, mock_install: MagicMock, _mock_plat: MagicMock
    ) -> None:
        mock_install.return_value = ServiceResult(
            success=True, platform="macos", message="OK"
        )
        install_service(include_watchdog=False)
        mock_install.assert_called_once_with(False)


class TestUninstallDispatch:
    """Verify uninstall_service dispatches correctly."""

    @patch(
        "amplifier_distro.service.detect_platform",
        return_value="unsupported",
    )
    def test_unsupported_returns_failure(self, _mock: MagicMock) -> None:
        result = uninstall_service()
        assert result.success is False

    @patch("amplifier_distro.service.detect_platform", return_value="linux")
    @patch("amplifier_distro.service._uninstall_systemd")
    def test_linux_delegates_to_systemd(
        self, mock_uninstall: MagicMock, _mock_plat: MagicMock
    ) -> None:
        mock_uninstall.return_value = ServiceResult(
            success=True, platform="linux", message="Removed"
        )
        uninstall_service()
        mock_uninstall.assert_called_once()


class TestServiceStatus:
    """Verify service_status dispatches and returns."""

    @patch(
        "amplifier_distro.service.detect_platform",
        return_value="unsupported",
    )
    def test_unsupported_returns_info(self, _mock: MagicMock) -> None:
        result = service_status()
        assert result.success is True
        assert result.platform == "unsupported"


# ---------------------------------------------------------------------------
# Systemd install (mocked filesystem + subprocess)
# ---------------------------------------------------------------------------


class TestInstallSystemd:
    """Verify _install_systemd with mocked shutil.which and subprocess."""

    @patch("amplifier_distro.service._run_cmd", return_value=(True, ""))
    @patch(
        "amplifier_distro.service._find_distro_binary",
        return_value="/usr/local/bin/amp-distro",
    )
    def test_install_creates_unit_files(
        self,
        _mock_bin: MagicMock,
        _mock_cmd: MagicMock,
        tmp_path: Path,
    ) -> None:
        from amplifier_distro.service import _install_systemd

        with patch(
            "amplifier_distro.service._systemd_dir",
            return_value=tmp_path,
        ):
            result = _install_systemd(include_watchdog=True)

        assert result.success is True
        # Check files were created
        server_file = tmp_path / f"{conventions.SERVICE_NAME}.service"
        watchdog_file = tmp_path / f"{conventions.SERVICE_NAME}-watchdog.service"
        assert server_file.exists()
        assert watchdog_file.exists()

    @patch("amplifier_distro.service._find_distro_binary", return_value=None)
    def test_install_fails_without_binary(self, _mock_bin: MagicMock) -> None:
        from amplifier_distro.service import _install_systemd

        result = _install_systemd(include_watchdog=True)
        assert result.success is False
        assert "amp-distro" in result.message

    @patch("amplifier_distro.service._run_cmd", return_value=(True, ""))
    @patch(
        "amplifier_distro.service._find_distro_binary",
        return_value="/usr/local/bin/amp-distro",
    )
    def test_install_without_watchdog(
        self,
        _mock_bin: MagicMock,
        _mock_cmd: MagicMock,
        tmp_path: Path,
    ) -> None:
        from amplifier_distro.service import _install_systemd

        with patch(
            "amplifier_distro.service._systemd_dir",
            return_value=tmp_path,
        ):
            result = _install_systemd(include_watchdog=False)

        assert result.success is True
        watchdog_file = tmp_path / f"{conventions.SERVICE_NAME}-watchdog.service"
        assert not watchdog_file.exists()


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


class TestServiceCli:
    """Verify service CLI subcommands via CliRunner."""

    @patch("amplifier_distro.service.install_service")
    def test_install_success(self, mock_install: MagicMock) -> None:
        mock_install.return_value = ServiceResult(
            success=True,
            platform="linux",
            message="Installed",
            details=["Server enabled", "Watchdog enabled"],
        )
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])

        assert result.exit_code == 0
        assert "installed" in result.output.lower()

    @patch("amplifier_distro.service.install_service")
    def test_install_failure(self, mock_install: MagicMock) -> None:
        mock_install.return_value = ServiceResult(
            success=False,
            platform="unsupported",
            message="Unsupported platform.",
        )
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])

        assert result.exit_code != 0

    @patch("amplifier_distro.service.install_service")
    def test_install_no_watchdog_flag(self, mock_install: MagicMock) -> None:
        mock_install.return_value = ServiceResult(
            success=True, platform="linux", message="OK"
        )
        from amplifier_distro.cli import main

        runner = CliRunner()
        runner.invoke(main, ["service", "install", "--no-watchdog"])

        mock_install.assert_called_once_with(include_watchdog=False)

    @patch("amplifier_distro.service.uninstall_service")
    def test_uninstall_success(self, mock_uninstall: MagicMock) -> None:
        mock_uninstall.return_value = ServiceResult(
            success=True, platform="linux", message="Removed"
        )
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["service", "uninstall"])

        assert result.exit_code == 0

    @patch("amplifier_distro.service.service_status")
    def test_status(self, mock_status: MagicMock) -> None:
        mock_status.return_value = ServiceResult(
            success=True,
            platform="linux",
            message="Installed",
            details=["Server: active", "Watchdog: active"],
        )
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["service", "status"])

        assert result.exit_code == 0
        assert "linux" in result.output.lower()
        assert "Server: active" in result.output


# ---------------------------------------------------------------------------
# ServiceResult model
# ---------------------------------------------------------------------------


class TestServiceResult:
    """Verify the ServiceResult Pydantic model."""

    def test_defaults(self) -> None:
        result = ServiceResult(success=True, platform="linux", message="OK")
        assert result.details == []

    def test_with_details(self) -> None:
        result = ServiceResult(
            success=True,
            platform="macos",
            message="Done",
            details=["step 1", "step 2"],
        )
        assert len(result.details) == 2


# ---------------------------------------------------------------------------
# Find distro binary
# ---------------------------------------------------------------------------


class TestFindDistroBinary:
    """Verify _find_distro_binary resolution logic."""

    def test_uses_argv0_when_exists(self, tmp_path: Path) -> None:
        from amplifier_distro.service import _find_distro_binary

        fake_binary = tmp_path / "amp-distro"
        fake_binary.touch()
        fake_binary.chmod(0o755)

        with patch.object(sys, "argv", [str(fake_binary)]):
            result = _find_distro_binary()

        assert result == str(fake_binary.resolve())

    def test_falls_back_to_shutil_which(self, tmp_path: Path) -> None:
        from amplifier_distro.service import _find_distro_binary

        nonexistent = str(tmp_path / "does-not-exist")

        with (
            patch.object(sys, "argv", [nonexistent]),
            patch.object(shutil, "which", return_value="/usr/local/bin/amp-distro"),
        ):
            result = _find_distro_binary()

        assert result == "/usr/local/bin/amp-distro"

    def test_returns_none_when_both_fail(self, tmp_path: Path) -> None:
        from amplifier_distro.service import _find_distro_binary

        nonexistent = str(tmp_path / "does-not-exist")

        with (
            patch.object(sys, "argv", [nonexistent]),
            patch.object(shutil, "which", return_value=None),
        ):
            result = _find_distro_binary()

        assert result is None


# ---------------------------------------------------------------------------
# Stale unit detection
# ---------------------------------------------------------------------------


class TestStaleUnitDetection:
    """Verify _status_systemd and _status_launchd warn on stale unit files."""

    def test_status_warns_on_stale_systemd_unit(self, tmp_path: Path) -> None:
        from amplifier_distro.service import _status_systemd

        unit_file = tmp_path / f"{conventions.SERVICE_NAME}.service"
        unit_file.write_text("[Service]\nExecStart=/usr/local/bin/amp-distro-server\n")

        with (
            patch(
                "amplifier_distro.service._systemd_server_unit_path",
                return_value=unit_file,
            ),
            patch(
                "amplifier_distro.service._run_cmd",
                return_value=(True, "active"),
            ),
        ):
            result = _status_systemd()

        deprecated_details = [
            d
            for d in result.details
            if "deprecated" in d
            and "amp-distro-server" in d
            and "amp-distro service uninstall" in d
        ]
        assert len(deprecated_details) >= 1

    def test_status_warns_on_stale_launchd_plist(self, tmp_path: Path) -> None:
        from amplifier_distro.service import _status_launchd

        plist_file = tmp_path / f"{conventions.LAUNCHD_LABEL}.plist"
        plist_file.write_text(
            '<?xml version="1.0"?>'
            "<plist><string>/usr/local/bin/amp-distro-server</string></plist>"
        )

        with (
            patch(
                "amplifier_distro.service._launchd_server_plist_path",
                return_value=plist_file,
            ),
            patch(
                "amplifier_distro.service._run_cmd",
                return_value=(True, "active"),
            ),
        ):
            result = _status_launchd()

        deprecated_details = [
            d
            for d in result.details
            if "deprecated" in d
            and "amp-distro-server" in d
            and "amp-distro service uninstall" in d
        ]
        assert len(deprecated_details) >= 1

    def test_no_warning_when_unit_is_current(self, tmp_path: Path) -> None:
        from amplifier_distro.service import _status_systemd

        unit_file = tmp_path / f"{conventions.SERVICE_NAME}.service"
        unit_file.write_text("[Service]\nExecStart=/usr/local/bin/amp-distro serve\n")

        with (
            patch(
                "amplifier_distro.service._systemd_server_unit_path",
                return_value=unit_file,
            ),
            patch(
                "amplifier_distro.service._run_cmd",
                return_value=(True, "active"),
            ),
        ):
            result = _status_systemd()

        deprecated_details = [d for d in result.details if "deprecated" in d]
        assert len(deprecated_details) == 0
