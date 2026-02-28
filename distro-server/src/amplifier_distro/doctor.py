"""Doctor command: diagnose and auto-fix common Amplifier distro problems.

Runs a comprehensive suite of diagnostic checks against the local
Amplifier installation and optionally applies automatic fixes for
common issues (missing directories, wrong permissions, stale PID files).

All paths are constructed from conventions.py constants -- no hardcoded paths.

Adapted from amplifier-distro-ramparte for distro-server:
- Config lives at DISTRO_HOME/settings.yaml (not AMPLIFIER_HOME/distro.yaml)
- Keys are in KEY=VALUE env format at AMPLIFIER_HOME/keys.env (not YAML)
- Identity/workspace loaded via distro_settings.load()
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field
import yaml

from . import conventions
from .distro_settings import load as load_settings
from .server.daemon import is_running, read_pid


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CheckStatus(StrEnum):
    """Possible outcomes for a single diagnostic check."""

    ok = "ok"
    warning = "warning"
    error = "error"


class DiagnosticCheck(BaseModel):
    """Result of a single diagnostic check."""

    name: str
    status: CheckStatus
    message: str
    fix_available: bool = False
    fix_description: str = ""


class DoctorReport(BaseModel):
    """Aggregate report from all diagnostic checks."""

    checks: list[DiagnosticCheck] = Field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        """Count of checks by status."""
        return {
            "ok": sum(1 for c in self.checks if c.status == CheckStatus.ok),
            "warning": sum(1 for c in self.checks if c.status == CheckStatus.warning),
            "error": sum(1 for c in self.checks if c.status == CheckStatus.error),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_unix() -> bool:
    """Return True on Linux/macOS (platforms with POSIX permissions)."""
    return platform.system() in ("Linux", "Darwin")


def _read_keys_env(keys_path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE env file, skipping blank lines and # comments.

    Returns a dict of key -> value.  Values containing '=' are handled
    correctly (only the first '=' is used as the separator).
    """
    result: dict[str, str] = {}
    try:
        for line in keys_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_config_exists(distro_home: Path) -> DiagnosticCheck:
    """Check that distro settings.yaml exists and is valid YAML."""
    cfg_path = distro_home / conventions.DISTRO_SETTINGS_FILENAME
    if not cfg_path.exists():
        return DiagnosticCheck(
            name="Config file",
            status=CheckStatus.error,
            message=f"settings.yaml not found at {cfg_path}",
            fix_available=False,
            fix_description="Run 'amp-distro init' to create it",
        )
    try:
        data = yaml.safe_load(cfg_path.read_text())
        if data is None:
            return DiagnosticCheck(
                name="Config file",
                status=CheckStatus.warning,
                message="settings.yaml is empty",
            )
        return DiagnosticCheck(
            name="Config file",
            status=CheckStatus.ok,
            message=f"Found at {cfg_path}",
        )
    except yaml.YAMLError as exc:
        return DiagnosticCheck(
            name="Config file",
            status=CheckStatus.error,
            message=f"Invalid YAML: {exc}",
        )


def _check_identity() -> DiagnosticCheck:
    """Check that a GitHub identity is configured in distro settings."""
    settings = load_settings()
    if settings.identity.github_handle:
        return DiagnosticCheck(
            name="Identity",
            status=CheckStatus.ok,
            message=f"@{settings.identity.github_handle}",
        )
    return DiagnosticCheck(
        name="Identity",
        status=CheckStatus.error,
        message="GitHub handle not set. Run 'amp-distro init'",
    )


def _check_workspace() -> DiagnosticCheck:
    """Check that the configured workspace root directory exists."""
    settings = load_settings()
    ws = Path(settings.workspace_root).expanduser()
    if ws.is_dir():
        return DiagnosticCheck(
            name="Workspace",
            status=CheckStatus.ok,
            message=str(ws),
        )
    return DiagnosticCheck(
        name="Workspace",
        status=CheckStatus.error,
        message=f"{ws} does not exist",
    )


def _check_amplifier_installed() -> DiagnosticCheck:
    """Check that the amplifier CLI binary is on PATH."""
    if shutil.which("amplifier"):
        return DiagnosticCheck(
            name="Amplifier CLI",
            status=CheckStatus.ok,
            message="Installed",
        )
    return DiagnosticCheck(
        name="Amplifier CLI",
        status=CheckStatus.error,
        message="Not found on PATH",
    )


def _check_memory_dir(home: Path) -> DiagnosticCheck:
    """Check that the memory directory exists and is writable."""
    memory_dir = home / conventions.MEMORY_DIR
    if not memory_dir.exists():
        return DiagnosticCheck(
            name="Memory directory",
            status=CheckStatus.warning,
            message=f"{memory_dir} does not exist",
            fix_available=True,
            fix_description=f"Create {memory_dir}",
        )
    if not memory_dir.is_dir():
        return DiagnosticCheck(
            name="Memory directory",
            status=CheckStatus.error,
            message=f"{memory_dir} exists but is not a directory",
        )
    if not os.access(memory_dir, os.W_OK):
        return DiagnosticCheck(
            name="Memory directory",
            status=CheckStatus.error,
            message=f"{memory_dir} is not writable",
            fix_available=True,
            fix_description=f"Fix permissions on {memory_dir}",
        )
    return DiagnosticCheck(
        name="Memory directory",
        status=CheckStatus.ok,
        message=str(memory_dir),
    )


def _check_keys_permissions(home: Path) -> DiagnosticCheck:
    """Check that keys.env has mode 600 (owner-only) on Unix."""
    keys_path = home / conventions.KEYS_FILENAME
    if not keys_path.exists():
        return DiagnosticCheck(
            name="Keys permissions",
            status=CheckStatus.ok,
            message="keys.env not present (nothing to check)",
        )
    if not _is_unix():
        return DiagnosticCheck(
            name="Keys permissions",
            status=CheckStatus.ok,
            message="Permission check skipped (Windows)",
        )
    mode = keys_path.stat().st_mode & 0o777
    if mode == 0o600:
        return DiagnosticCheck(
            name="Keys permissions",
            status=CheckStatus.ok,
            message="keys.env has correct permissions (600)",
        )
    return DiagnosticCheck(
        name="Keys permissions",
        status=CheckStatus.warning,
        message=f"keys.env has mode {oct(mode)}, should be 0o600",
        fix_available=True,
        fix_description="Set keys.env permissions to 600",
    )


def _check_bundle_cache(home: Path) -> DiagnosticCheck:
    """Check that the bundle cache directory exists."""
    cache_dir = home / conventions.CACHE_DIR
    if not cache_dir.exists():
        return DiagnosticCheck(
            name="Bundle cache",
            status=CheckStatus.warning,
            message=f"{cache_dir} does not exist",
            fix_available=True,
            fix_description=f"Create {cache_dir}",
        )
    return DiagnosticCheck(
        name="Bundle cache",
        status=CheckStatus.ok,
        message=str(cache_dir),
    )


def _check_server_dir(home: Path) -> DiagnosticCheck:
    """Check that the server directory exists."""
    server_dir = home / conventions.SERVER_DIR
    if not server_dir.exists():
        return DiagnosticCheck(
            name="Server directory",
            status=CheckStatus.warning,
            message=f"{server_dir} does not exist",
            fix_available=True,
            fix_description=f"Create {server_dir}",
        )
    return DiagnosticCheck(
        name="Server directory",
        status=CheckStatus.ok,
        message=str(server_dir),
    )


def _check_server_running(home: Path) -> DiagnosticCheck:
    """Check whether the distro server is running via its PID file."""
    server_dir = home / conventions.SERVER_DIR
    pid_path = server_dir / conventions.SERVER_PID_FILE
    if not pid_path.exists():
        return DiagnosticCheck(
            name="Server status",
            status=CheckStatus.ok,
            message="No PID file (server not expected to be running)",
        )
    pid = read_pid(pid_path)
    if pid is None:
        return DiagnosticCheck(
            name="Server status",
            status=CheckStatus.warning,
            message="PID file exists but is unreadable",
            fix_available=True,
            fix_description="Remove stale PID file",
        )
    if is_running(pid_path):
        return DiagnosticCheck(
            name="Server status",
            status=CheckStatus.ok,
            message=f"Server running (PID {pid})",
        )
    return DiagnosticCheck(
        name="Server status",
        status=CheckStatus.warning,
        message=f"Stale PID file (process {pid} not running)",
        fix_available=True,
        fix_description="Remove stale PID file",
    )


def _check_git_configured() -> DiagnosticCheck:
    """Check that git user.name and user.email are configured."""
    try:
        name_result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        email_result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        name = name_result.stdout.strip()
        email = email_result.stdout.strip()
        if name and email:
            return DiagnosticCheck(
                name="Git config",
                status=CheckStatus.ok,
                message=f"{name} <{email}>",
            )
        missing = []
        if not name:
            missing.append("user.name")
        if not email:
            missing.append("user.email")
        return DiagnosticCheck(
            name="Git config",
            status=CheckStatus.warning,
            message=f"Missing: {', '.join(missing)}",
        )
    except FileNotFoundError:
        return DiagnosticCheck(
            name="Git config",
            status=CheckStatus.error,
            message="git not installed",
        )
    except subprocess.TimeoutExpired:
        return DiagnosticCheck(
            name="Git config",
            status=CheckStatus.warning,
            message="Timed out checking git config",
        )


def _check_gh_authenticated() -> DiagnosticCheck:
    """Check that the GitHub CLI is authenticated."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return DiagnosticCheck(
                name="GitHub CLI",
                status=CheckStatus.ok,
                message="Authenticated",
            )
        return DiagnosticCheck(
            name="GitHub CLI",
            status=CheckStatus.warning,
            message="Not authenticated. Run 'gh auth login'",
        )
    except FileNotFoundError:
        return DiagnosticCheck(
            name="GitHub CLI",
            status=CheckStatus.warning,
            message="gh CLI not installed",
        )
    except subprocess.TimeoutExpired:
        return DiagnosticCheck(
            name="GitHub CLI",
            status=CheckStatus.warning,
            message="Timed out checking gh auth",
        )


def _check_slack_configured(home: Path) -> DiagnosticCheck:
    """Check that the Slack bridge has a bot token available.

    Checks environment first, then keys.env (KEY=VALUE format).
    """
    if os.environ.get("SLACK_BOT_TOKEN"):
        return DiagnosticCheck(
            name="Slack bridge",
            status=CheckStatus.ok,
            message="SLACK_BOT_TOKEN set in environment",
        )
    keys_path = home / conventions.KEYS_FILENAME
    if keys_path.exists():
        keys = _read_keys_env(keys_path)
        if keys.get("SLACK_BOT_TOKEN"):
            return DiagnosticCheck(
                name="Slack bridge",
                status=CheckStatus.ok,
                message="SLACK_BOT_TOKEN found in keys.env",
            )
    return DiagnosticCheck(
        name="Slack bridge",
        status=CheckStatus.warning,
        message="SLACK_BOT_TOKEN not found in env or keys.env",
    )


def _check_voice_configured(home: Path) -> DiagnosticCheck:
    """Check that voice has an OpenAI API key available.

    Checks environment first, then keys.env (KEY=VALUE format).
    """
    if os.environ.get("OPENAI_API_KEY"):
        return DiagnosticCheck(
            name="Voice config",
            status=CheckStatus.ok,
            message="OPENAI_API_KEY set in environment",
        )
    keys_path = home / conventions.KEYS_FILENAME
    if keys_path.exists():
        keys = _read_keys_env(keys_path)
        if keys.get("OPENAI_API_KEY"):
            return DiagnosticCheck(
                name="Voice config",
                status=CheckStatus.ok,
                message="OPENAI_API_KEY found in keys.env",
            )
    return DiagnosticCheck(
        name="Voice config",
        status=CheckStatus.warning,
        message="OPENAI_API_KEY not found in env or keys.env",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_diagnostics(
    amplifier_home: Path,
    distro_home: Path | None = None,
) -> DoctorReport:
    """Run the full diagnostic suite and return a report.

    Args:
        amplifier_home: Resolved path to the amplifier home directory
            (typically ``~/.amplifier`` expanded).
        distro_home: Resolved path to the distro home directory
            (typically ``~/.amplifier-distro`` expanded).
            Defaults to ``Path(conventions.DISTRO_HOME).expanduser()``.

    Returns:
        A :class:`DoctorReport` containing all check results.
    """
    if distro_home is None:
        distro_home = Path(conventions.DISTRO_HOME).expanduser()

    report = DoctorReport()

    # Pre-flight style checks
    report.checks.append(_check_config_exists(distro_home))
    report.checks.append(_check_identity())
    report.checks.append(_check_workspace())
    report.checks.append(_check_amplifier_installed())

    # Filesystem checks
    report.checks.append(_check_memory_dir(amplifier_home))
    report.checks.append(_check_keys_permissions(amplifier_home))
    report.checks.append(_check_bundle_cache(amplifier_home))
    report.checks.append(_check_server_dir(amplifier_home))

    # Server check
    report.checks.append(_check_server_running(amplifier_home))

    # External tool checks
    report.checks.append(_check_git_configured())
    report.checks.append(_check_gh_authenticated())

    # Integration checks
    report.checks.append(_check_slack_configured(amplifier_home))
    report.checks.append(_check_voice_configured(amplifier_home))

    return report


def run_fixes(amplifier_home: Path, report: DoctorReport) -> list[str]:
    """Apply automatic fixes for fixable issues found in a report.

    Only attempts fixes for checks where ``fix_available`` is True.

    Args:
        amplifier_home: Resolved path to the amplifier home directory.
        report: A previously-generated :class:`DoctorReport`.

    Returns:
        A list of human-readable descriptions of fixes that were applied.
    """
    fixed: list[str] = []

    for check in report.checks:
        if check.status == CheckStatus.ok or not check.fix_available:
            continue

        if check.name == "Memory directory":
            memory_dir = amplifier_home / conventions.MEMORY_DIR
            memory_dir.mkdir(parents=True, exist_ok=True)
            fixed.append(f"Created directory: {memory_dir}")

        elif check.name == "Keys permissions":
            keys_path = amplifier_home / conventions.KEYS_FILENAME
            if keys_path.exists() and _is_unix():
                keys_path.chmod(0o600)
                fixed.append("Set keys.env permissions to 600")

        elif check.name == "Bundle cache":
            cache_dir = amplifier_home / conventions.CACHE_DIR
            cache_dir.mkdir(parents=True, exist_ok=True)
            fixed.append(f"Created directory: {cache_dir}")

        elif check.name == "Server directory":
            server_dir = amplifier_home / conventions.SERVER_DIR
            server_dir.mkdir(parents=True, exist_ok=True)
            fixed.append(f"Created directory: {server_dir}")

        elif check.name == "Server status":
            # Clear stale PID file
            pid_path = (
                amplifier_home / conventions.SERVER_DIR / conventions.SERVER_PID_FILE
            )
            if pid_path.exists():
                pid_path.unlink(missing_ok=True)
                fixed.append("Removed stale PID file")

    return fixed
