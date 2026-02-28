"""Tests for the doctor command: diagnostics, auto-fix, and CLI integration.

Covers:
- DiagnosticCheck and DoctorReport Pydantic models
- Each individual diagnostic check (mocked filesystem state)
- Auto-fix actions (verify files created, permissions set)
- CLI command output format (human-readable and JSON)
- --fix flag triggers fixes

Adapted from amplifier-distro-ramparte commit 414a8ef7 for distro-server:
- Settings live in DISTRO_HOME/settings.yaml (not AMPLIFIER_HOME/distro.yaml)
- Keys are in KEY=VALUE env format (not YAML)
- load_settings() replaces load_config()
"""

import json
import platform
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from amplifier_distro import conventions
from amplifier_distro.cli import main
from amplifier_distro.doctor import (
    CheckStatus,
    DiagnosticCheck,
    DoctorReport,
    _check_gh_authenticated,
    _check_git_configured,
    _read_keys_env,
    run_diagnostics,
    run_fixes,
)

# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestDiagnosticCheckModel:
    """Verify the DiagnosticCheck Pydantic model."""

    def test_minimal_construction(self):
        check = DiagnosticCheck(name="test", status=CheckStatus.ok, message="all good")
        assert check.name == "test"
        assert check.status == CheckStatus.ok
        assert check.message == "all good"
        assert check.fix_available is False
        assert check.fix_description == ""

    def test_with_fix_info(self):
        check = DiagnosticCheck(
            name="broken",
            status=CheckStatus.error,
            message="something wrong",
            fix_available=True,
            fix_description="run this to fix",
        )
        assert check.fix_available is True
        assert check.fix_description == "run this to fix"

    def test_serializes_to_dict(self):
        check = DiagnosticCheck(name="test", status=CheckStatus.warning, message="meh")
        data = check.model_dump()
        assert data["name"] == "test"
        assert data["status"] == "warning"
        assert data["message"] == "meh"

    def test_status_values(self):
        """CheckStatus must have exactly ok, warning, error."""
        assert set(CheckStatus) == {
            CheckStatus.ok,
            CheckStatus.warning,
            CheckStatus.error,
        }


class TestDoctorReportModel:
    """Verify the DoctorReport Pydantic model."""

    def test_empty_report_summary(self):
        report = DoctorReport()
        assert report.summary == {"ok": 0, "warning": 0, "error": 0}

    def test_summary_counts(self):
        report = DoctorReport(
            checks=[
                DiagnosticCheck(name="a", status=CheckStatus.ok, message=""),
                DiagnosticCheck(name="b", status=CheckStatus.ok, message=""),
                DiagnosticCheck(name="c", status=CheckStatus.warning, message=""),
                DiagnosticCheck(name="d", status=CheckStatus.error, message=""),
            ]
        )
        assert report.summary == {"ok": 2, "warning": 1, "error": 1}

    def test_checks_default_to_empty(self):
        report = DoctorReport()
        assert report.checks == []


# ---------------------------------------------------------------------------
# Keys env parser tests
# ---------------------------------------------------------------------------


class TestReadKeysEnv:
    """Test the KEY=VALUE env file parser."""

    def test_parses_key_value_pairs(self, tmp_path):
        keys = tmp_path / "keys.env"
        keys.write_text("FOO=bar\nBAZ=qux\n")
        result = _read_keys_env(keys)
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_skips_comments_and_blank_lines(self, tmp_path):
        keys = tmp_path / "keys.env"
        keys.write_text("# a comment\n\nFOO=bar\n  # indented comment\n")
        result = _read_keys_env(keys)
        assert result == {"FOO": "bar"}

    def test_value_with_equals_sign(self, tmp_path):
        """Values containing '=' should not be split further."""
        keys = tmp_path / "keys.env"
        keys.write_text("TOKEN=abc=def\n")
        result = _read_keys_env(keys)
        assert result == {"TOKEN": "abc=def"}

    def test_missing_file_returns_empty(self, tmp_path):
        result = _read_keys_env(tmp_path / "nonexistent.env")
        assert result == {}


# ---------------------------------------------------------------------------
# Individual diagnostic check tests
# ---------------------------------------------------------------------------


class TestCheckConfigExists:
    """Test the config file diagnostic check.

    The config file lives at DISTRO_HOME/settings.yaml (not AMPLIFIER_HOME).
    We pass distro_home=tmp_path so we can place the file there.
    """

    def test_config_present_and_valid(self, tmp_path):
        cfg = tmp_path / conventions.DISTRO_SETTINGS_FILENAME
        cfg.write_text(yaml.dump({"workspace_root": "~/dev"}))
        report = _run_with_home(tmp_path)
        config_check = _find_check(report, "Config file")
        assert config_check.status == CheckStatus.ok

    def test_config_missing(self, tmp_path):
        report = _run_with_home(tmp_path)
        config_check = _find_check(report, "Config file")
        assert config_check.status == CheckStatus.error

    def test_config_empty(self, tmp_path):
        cfg = tmp_path / conventions.DISTRO_SETTINGS_FILENAME
        cfg.write_text("")
        report = _run_with_home(tmp_path)
        config_check = _find_check(report, "Config file")
        assert config_check.status == CheckStatus.warning


class TestCheckMemoryDir:
    """Test the memory directory diagnostic check."""

    def test_memory_dir_exists_and_writable(self, tmp_path):
        mem = tmp_path / conventions.MEMORY_DIR
        mem.mkdir(parents=True)
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Memory directory")
        assert check.status == CheckStatus.ok

    def test_memory_dir_missing(self, tmp_path):
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Memory directory")
        assert check.status == CheckStatus.warning
        assert check.fix_available is True


class TestCheckKeysPermissions:
    """Test the keys.env permission diagnostic check."""

    def test_no_keys_file(self, tmp_path):
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Keys permissions")
        assert check.status == CheckStatus.ok
        assert "not present" in check.message

    @pytest.mark.skipif(platform.system() == "Windows", reason="Unix permissions only")
    def test_keys_correct_permissions(self, tmp_path):
        keys = tmp_path / conventions.KEYS_FILENAME
        keys.write_text("ANTHROPIC_API_KEY=sk-123")
        keys.chmod(0o600)
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Keys permissions")
        assert check.status == CheckStatus.ok

    @pytest.mark.skipif(platform.system() == "Windows", reason="Unix permissions only")
    def test_keys_wrong_permissions(self, tmp_path):
        keys = tmp_path / conventions.KEYS_FILENAME
        keys.write_text("ANTHROPIC_API_KEY=sk-123")
        keys.chmod(0o644)
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Keys permissions")
        assert check.status == CheckStatus.warning
        assert check.fix_available is True


class TestCheckServerStatus:
    """Test the server PID file diagnostic check."""

    def test_no_pid_file(self, tmp_path):
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Server status")
        assert check.status == CheckStatus.ok

    def test_stale_pid_file(self, tmp_path):
        server_dir = tmp_path / conventions.SERVER_DIR
        server_dir.mkdir()
        pid_file = server_dir / conventions.SERVER_PID_FILE
        pid_file.write_text("999999999")  # Non-existent PID
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Server status")
        assert check.status == CheckStatus.warning
        assert check.fix_available is True


class TestCheckBundleCache:
    """Test the bundle cache directory diagnostic check."""

    def test_cache_exists(self, tmp_path):
        cache = tmp_path / conventions.CACHE_DIR
        cache.mkdir()
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Bundle cache")
        assert check.status == CheckStatus.ok

    def test_cache_missing(self, tmp_path):
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Bundle cache")
        assert check.status == CheckStatus.warning
        assert check.fix_available is True


class TestCheckGitConfigured:
    """Test the git config diagnostic check (unit-testing the check function)."""

    @patch("amplifier_distro.doctor.subprocess.run")
    def test_git_configured(self, mock_run):
        mock_run.side_effect = [
            MagicMock(stdout="Test User\n", returncode=0),
            MagicMock(stdout="test@example.com\n", returncode=0),
        ]
        check = _check_git_configured()
        assert check.status == CheckStatus.ok

    @patch("amplifier_distro.doctor.subprocess.run")
    def test_git_missing_name(self, mock_run):
        mock_run.side_effect = [
            MagicMock(stdout="", returncode=1),
            MagicMock(stdout="test@example.com\n", returncode=0),
        ]
        check = _check_git_configured()
        assert check.status == CheckStatus.warning

    @patch("amplifier_distro.doctor.subprocess.run", side_effect=FileNotFoundError)
    def test_git_not_installed(self, _mock):
        check = _check_git_configured()
        assert check.status == CheckStatus.error


class TestCheckGhAuthenticated:
    """Test the GitHub CLI auth check (unit-testing the check function)."""

    @patch("amplifier_distro.doctor.subprocess.run")
    def test_gh_authenticated(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        check = _check_gh_authenticated()
        assert check.status == CheckStatus.ok

    @patch("amplifier_distro.doctor.subprocess.run")
    def test_gh_not_authenticated(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        check = _check_gh_authenticated()
        assert check.status == CheckStatus.warning

    @patch("amplifier_distro.doctor.subprocess.run", side_effect=FileNotFoundError)
    def test_gh_not_installed(self, _mock):
        check = _check_gh_authenticated()
        assert check.status == CheckStatus.warning


class TestCheckSlackConfigured:
    """Test the Slack bridge diagnostic check.

    Keys are in KEY=VALUE env format, not YAML.
    """

    def test_slack_token_in_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Slack bridge")
        assert check.status == CheckStatus.ok

    def test_slack_token_in_keys_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        keys = tmp_path / conventions.KEYS_FILENAME
        keys.write_text("SLACK_BOT_TOKEN=xoxb-real\n")
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Slack bridge")
        assert check.status == CheckStatus.ok

    def test_slack_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Slack bridge")
        assert check.status == CheckStatus.warning


class TestCheckVoiceConfigured:
    """Test the voice config diagnostic check.

    Keys are in KEY=VALUE env format, not YAML.
    """

    def test_openai_key_in_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test123")
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Voice config")
        assert check.status == CheckStatus.ok

    def test_openai_key_in_keys_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        keys = tmp_path / conventions.KEYS_FILENAME
        keys.write_text("OPENAI_API_KEY=sk-real\n")
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Voice config")
        assert check.status == CheckStatus.ok

    def test_voice_not_configured(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        report = _run_with_home(tmp_path)
        check = _find_check(report, "Voice config")
        assert check.status == CheckStatus.warning


# ---------------------------------------------------------------------------
# Auto-fix tests
# ---------------------------------------------------------------------------


class TestRunFixes:
    """Test auto-fix capabilities."""

    def test_fix_creates_memory_dir(self, tmp_path):
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Memory directory",
                    status=CheckStatus.warning,
                    message="missing",
                    fix_available=True,
                    fix_description="create it",
                )
            ]
        )
        fixes = run_fixes(tmp_path, report)
        assert len(fixes) == 1
        assert "Created directory" in fixes[0]
        assert (tmp_path / conventions.MEMORY_DIR).is_dir()

    def test_fix_creates_cache_dir(self, tmp_path):
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Bundle cache",
                    status=CheckStatus.warning,
                    message="missing",
                    fix_available=True,
                    fix_description="create it",
                )
            ]
        )
        fixes = run_fixes(tmp_path, report)
        assert len(fixes) == 1
        assert (tmp_path / conventions.CACHE_DIR).is_dir()

    def test_fix_creates_server_dir(self, tmp_path):
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Server directory",
                    status=CheckStatus.warning,
                    message="missing",
                    fix_available=True,
                    fix_description="create it",
                )
            ]
        )
        fixes = run_fixes(tmp_path, report)
        assert len(fixes) == 1
        assert (tmp_path / conventions.SERVER_DIR).is_dir()

    @pytest.mark.skipif(platform.system() == "Windows", reason="Unix permissions only")
    def test_fix_keys_permissions(self, tmp_path):
        keys = tmp_path / conventions.KEYS_FILENAME
        keys.write_text("secret=value")
        keys.chmod(0o644)
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Keys permissions",
                    status=CheckStatus.warning,
                    message="bad perms",
                    fix_available=True,
                    fix_description="chmod 600",
                )
            ]
        )
        fixes = run_fixes(tmp_path, report)
        assert len(fixes) == 1
        assert "permissions" in fixes[0].lower()
        assert keys.stat().st_mode & 0o777 == 0o600

    def test_fix_removes_stale_pid(self, tmp_path):
        server_dir = tmp_path / conventions.SERVER_DIR
        server_dir.mkdir()
        pid_file = server_dir / conventions.SERVER_PID_FILE
        pid_file.write_text("99999")
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Server status",
                    status=CheckStatus.warning,
                    message="stale",
                    fix_available=True,
                    fix_description="remove stale PID",
                )
            ]
        )
        fixes = run_fixes(tmp_path, report)
        assert len(fixes) == 1
        assert not pid_file.exists()

    def test_no_fixes_for_ok_checks(self, tmp_path):
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Memory directory",
                    status=CheckStatus.ok,
                    message="fine",
                    fix_available=True,
                    fix_description="should not run",
                )
            ]
        )
        fixes = run_fixes(tmp_path, report)
        assert fixes == []

    def test_no_fixes_for_unfixable_checks(self, tmp_path):
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Config file",
                    status=CheckStatus.error,
                    message="missing",
                    fix_available=False,
                )
            ]
        )
        fixes = run_fixes(tmp_path, report)
        assert fixes == []


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestDoctorCLI:
    """Test the CLI integration of the doctor command."""

    def test_doctor_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "diagnose" in result.output.lower() or "doctor" in result.output.lower()

    def test_doctor_shows_checks(self):
        """Doctor command shows check results in human-readable form."""
        runner = CliRunner()
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Config file", status=CheckStatus.ok, message="found"
                ),
                DiagnosticCheck(
                    name="Memory directory",
                    status=CheckStatus.warning,
                    message="missing",
                    fix_available=True,
                    fix_description="create it",
                ),
            ]
        )
        with patch("amplifier_distro.doctor.run_diagnostics", return_value=report):
            result = runner.invoke(main, ["doctor"])
            assert result.exit_code == 0
            assert "Config file" in result.output
            assert "Memory directory" in result.output
            assert "create it" in result.output

    def test_doctor_json_output(self):
        """--json flag produces valid JSON with expected structure."""
        runner = CliRunner()
        report = DoctorReport(
            checks=[
                DiagnosticCheck(name="test", status=CheckStatus.ok, message="good"),
            ]
        )
        with patch("amplifier_distro.doctor.run_diagnostics", return_value=report):
            result = runner.invoke(main, ["doctor", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "checks" in data
            assert "summary" in data
            assert "fixes_applied" in data
            assert data["checks"][0]["name"] == "test"
            assert data["summary"]["ok"] == 1

    def test_doctor_fix_flag(self):
        """--fix flag triggers run_fixes and re-runs diagnostics."""
        runner = CliRunner()
        warning_report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Memory directory",
                    status=CheckStatus.warning,
                    message="missing",
                    fix_available=True,
                    fix_description="create it",
                ),
            ]
        )
        ok_report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Memory directory",
                    status=CheckStatus.ok,
                    message="created",
                ),
            ]
        )
        with (
            patch(
                "amplifier_distro.doctor.run_diagnostics",
                side_effect=[warning_report, ok_report],
            ),
            patch(
                "amplifier_distro.doctor.run_fixes",
                return_value=["Created directory: /tmp/memory"],
            ) as mock_fixes,
        ):
            result = runner.invoke(main, ["doctor", "--fix"])
            assert result.exit_code == 0
            mock_fixes.assert_called_once()
            assert "Fixes applied" in result.output

    def test_doctor_exits_nonzero_on_errors(self):
        """Doctor exits non-zero when error-severity checks remain."""
        runner = CliRunner()
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Config file",
                    status=CheckStatus.error,
                    message="missing",
                ),
            ]
        )
        with patch("amplifier_distro.doctor.run_diagnostics", return_value=report):
            result = runner.invoke(main, ["doctor"])
            assert result.exit_code != 0

    def test_doctor_exits_zero_on_warnings_only(self):
        """Doctor exits zero when only warnings remain (no errors)."""
        runner = CliRunner()
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Slack bridge",
                    status=CheckStatus.warning,
                    message="not configured",
                ),
            ]
        )
        with patch("amplifier_distro.doctor.run_diagnostics", return_value=report):
            result = runner.invoke(main, ["doctor"])
            assert result.exit_code == 0

    def test_doctor_json_with_fix(self):
        """--json --fix together produce JSON with fixes_applied populated."""
        runner = CliRunner()
        report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Bundle cache",
                    status=CheckStatus.warning,
                    message="missing",
                    fix_available=True,
                    fix_description="create it",
                ),
            ]
        )
        ok_report = DoctorReport(
            checks=[
                DiagnosticCheck(
                    name="Bundle cache",
                    status=CheckStatus.ok,
                    message="exists",
                ),
            ]
        )
        with (
            patch(
                "amplifier_distro.doctor.run_diagnostics",
                side_effect=[report, ok_report],
            ),
            patch(
                "amplifier_distro.doctor.run_fixes",
                return_value=["Created directory: /tmp/cache"],
            ),
        ):
            result = runner.invoke(main, ["doctor", "--json", "--fix"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data["fixes_applied"]) == 1


# ---------------------------------------------------------------------------
# Full diagnostics integration test
# ---------------------------------------------------------------------------


class TestRunDiagnostics:
    """Test that run_diagnostics returns the expected set of checks."""

    @patch("amplifier_distro.doctor.subprocess.run", side_effect=FileNotFoundError)
    @patch("amplifier_distro.doctor.shutil.which", return_value=None)
    def test_all_checks_present(self, _mock_which, _mock_run, tmp_path):
        """run_diagnostics must return all 13 named checks."""
        report = _run_with_home(tmp_path)
        names = [c.name for c in report.checks]
        expected = [
            "Config file",
            "Identity",
            "Workspace",
            "Amplifier CLI",
            "Memory directory",
            "Keys permissions",
            "Bundle cache",
            "Server directory",
            "Server status",
            "Git config",
            "GitHub CLI",
            "Slack bridge",
            "Voice config",
        ]
        for name in expected:
            assert name in names, f"Missing check: {name}"
        assert len(report.checks) == 13


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_with_home(home: Path) -> DoctorReport:
    """Run diagnostics with mocked external commands against a temp home.

    Uses home as both amplifier_home and distro_home for simplicity.
    """
    with (
        patch(
            "amplifier_distro.doctor.subprocess.run",
            side_effect=FileNotFoundError,
        ),
        patch("amplifier_distro.doctor.shutil.which", return_value=None),
        patch("amplifier_distro.doctor.load_settings"),
    ):
        return run_diagnostics(home, distro_home=home)


def _find_check(report: DoctorReport, name: str) -> DiagnosticCheck:
    """Find a check by name, raising if not found."""
    for c in report.checks:
        if c.name == name:
            return c
    raise AssertionError(
        f"Check {name!r} not found in report: {[c.name for c in report.checks]}"
    )
