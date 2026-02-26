"""Platform service registration: auto-start on boot.

Installs/uninstalls OS-level services that start the amplifier-distro
watchdog (or server directly) on boot.

Supported platforms:
- Linux (including WSL2): systemd user service
- macOS: launchd user agent

All templates are generated in code -- no external template files needed.
All paths are constructed from conventions.py constants.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

from pydantic import BaseModel, Field

from amplifier_distro import conventions

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class ServiceResult(BaseModel):
    """Outcome of a service install/uninstall/status operation."""

    success: bool
    platform: str
    message: str
    details: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def detect_platform() -> str:
    """Detect the current platform for service registration.

    Returns:
        One of: ``'linux'``, ``'macos'``, or ``'unsupported'``.
        WSL2 is detected as ``'linux'`` (systemd works on modern WSL2).
    """
    system = platform.system()
    if system == "Linux":
        return "linux"
    if system == "Darwin":
        return "macos"
    return "unsupported"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_service(include_watchdog: bool = True) -> ServiceResult:
    """Install platform service for auto-start on boot.

    Args:
        include_watchdog: If True (default), the boot service runs the
            watchdog which manages the server. If False, the boot service
            runs the server directly (systemd/launchd handle restarts).

    Returns:
        ServiceResult with success status and details.
    """
    plat = detect_platform()
    if plat == "linux":
        return _install_systemd(include_watchdog)
    if plat == "macos":
        return _install_launchd(include_watchdog)
    return ServiceResult(
        success=False,
        platform=plat,
        message="Unsupported platform for automatic service installation.",
        details=[
            "Supported: Linux (systemd), macOS (launchd).",
            "For Windows, use Task Scheduler to run: amp-distro-server start",
        ],
    )


def uninstall_service() -> ServiceResult:
    """Remove platform service.

    Returns:
        ServiceResult with success status and details.
    """
    plat = detect_platform()
    if plat == "linux":
        return _uninstall_systemd()
    if plat == "macos":
        return _uninstall_launchd()
    return ServiceResult(
        success=False,
        platform=plat,
        message="Unsupported platform.",
    )


def service_status() -> ServiceResult:
    """Check if the platform service is installed and running.

    Returns:
        ServiceResult with status information.
    """
    plat = detect_platform()
    if plat == "linux":
        return _status_systemd()
    if plat == "macos":
        return _status_launchd()
    return ServiceResult(
        success=True,
        platform=plat,
        message="No service support on this platform.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_server_binary() -> str | None:
    """Find the amp-distro-server binary on PATH."""
    return shutil.which("amp-distro-server")


def _find_python_binary() -> str:
    """Return the current Python interpreter path."""
    return sys.executable


def _run_cmd(cmd: list[str], timeout: int = 10) -> tuple[bool, str]:
    """Run a command and return (success, output).

    Args:
        cmd: Command and arguments.
        timeout: Maximum seconds to wait.

    Returns:
        Tuple of (success: bool, combined stdout+stderr: str).
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"Command timed out: {' '.join(cmd)}"


# ===========================================================================
# Linux: systemd (user service)
# ===========================================================================

# Paths:
#   Server:   ~/.config/systemd/user/amplifier-distro.service
#   Watchdog: ~/.config/systemd/user/amplifier-distro-watchdog.service


def _systemd_dir() -> Path:
    """Return the systemd user service directory."""
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_server_unit_path() -> Path:
    """Return path for the server systemd unit file."""
    return _systemd_dir() / f"{conventions.SERVICE_NAME}.service"


def _systemd_watchdog_unit_path() -> Path:
    """Return path for the watchdog systemd unit file."""
    return _systemd_dir() / f"{conventions.SERVICE_NAME}-watchdog.service"


def _generate_systemd_server_unit(server_bin: str) -> str:
    """Generate the systemd unit file for the server.

    Args:
        server_bin: Absolute path to the amp-distro-server binary.

    Returns:
        Complete systemd unit file content as a string.
    """
    path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    port = conventions.SERVER_DEFAULT_PORT
    amplifier_home = Path(conventions.AMPLIFIER_HOME).expanduser()
    return dedent(f"""\
        [Unit]
        Description=Amplifier Distro Server
        After=network.target

        [Service]
        Type=simple
        ExecStart={server_bin} --host 127.0.0.1 --port {port}
        Restart=always
        RestartSec=5
        StartLimitIntervalSec=60
        StartLimitBurst=5
        WorkingDirectory=%h
        Environment=PATH={path_env}
        EnvironmentFile=-{amplifier_home}/.env
        StandardOutput=journal
        StandardError=journal

        [Install]
        WantedBy=default.target
    """)


def _generate_systemd_watchdog_unit(server_bin: str) -> str:
    """Generate the systemd unit file for the watchdog.

    The watchdog unit uses ``Restart=always`` so it is always running.
    It uses ``Wants=`` (not ``BindsTo=``) so the watchdog survives
    server death -- that's its whole purpose: detect failure and restart.

    Args:
        server_bin: Absolute path to the amp-distro-server binary.
            Used to locate the Python interpreter from the same
            virtual environment.

    Returns:
        Complete systemd unit file content as a string.
    """
    python_bin = _find_python_binary()
    path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    port = conventions.SERVER_DEFAULT_PORT
    service_name = conventions.SERVICE_NAME
    amplifier_home = Path(conventions.AMPLIFIER_HOME).expanduser()
    exec_start = (
        f"{python_bin} -m amplifier_distro.server.watchdog"
        f" --host 127.0.0.1 --port {port}"
    )
    return dedent(f"""\
        [Unit]
        Description=Amplifier Distro Watchdog
        After={service_name}.service
        Wants={service_name}.service

        [Service]
        Type=simple
        ExecStart={exec_start}
        Restart=always
        RestartSec=10
        StartLimitIntervalSec=300
        StartLimitBurst=3
        WorkingDirectory=%h
        Environment=PATH={path_env}
        EnvironmentFile=-{amplifier_home}/.env
        StandardOutput=journal
        StandardError=journal

        [Install]
        WantedBy=default.target
    """)


def _install_systemd(include_watchdog: bool) -> ServiceResult:
    """Install systemd user services.

    Steps:
    1. Find amp-distro-server binary via shutil.which.
    2. Create ~/.config/systemd/user/ directory.
    3. Generate and write server unit file.
    4. If include_watchdog: generate and write watchdog unit file.
    5. Run: systemctl --user daemon-reload.
    6. Enable and start server service.
    7. If include_watchdog: enable and start watchdog service.

    Args:
        include_watchdog: Whether to also install the watchdog service.

    Returns:
        ServiceResult with outcome details.
    """
    server_bin = _find_server_binary()
    if server_bin is None:
        return ServiceResult(
            success=False,
            platform="linux",
            message="amp-distro-server not found on PATH.",
            details=["Install amplifier-distro first: uv pip install amplifier-distro"],
        )

    details: list[str] = []

    # Create directory
    systemd_dir = _systemd_dir()
    systemd_dir.mkdir(parents=True, exist_ok=True)

    # Write server unit
    server_unit_path = _systemd_server_unit_path()
    server_unit_path.write_text(_generate_systemd_server_unit(server_bin))
    details.append(f"Wrote {server_unit_path}")

    # Write watchdog unit
    if include_watchdog:
        watchdog_unit_path = _systemd_watchdog_unit_path()
        watchdog_unit_path.write_text(_generate_systemd_watchdog_unit(server_bin))
        details.append(f"Wrote {watchdog_unit_path}")

    # Reload systemd
    ok, output = _run_cmd(["systemctl", "--user", "daemon-reload"])
    if not ok:
        return ServiceResult(
            success=False,
            platform="linux",
            message="systemctl daemon-reload failed.",
            details=[*details, output],
        )
    details.append("Reloaded systemd daemon")

    # Enable and start server
    service_name = conventions.SERVICE_NAME
    ok, output = _run_cmd(
        ["systemctl", "--user", "enable", "--now", f"{service_name}.service"]
    )
    if ok:
        details.append(f"Enabled and started {service_name}.service")
    else:
        details.append(f"Warning: could not enable {service_name}.service: {output}")

    # Enable and start watchdog
    if include_watchdog:
        ok, output = _run_cmd(
            [
                "systemctl",
                "--user",
                "enable",
                "--now",
                f"{service_name}-watchdog.service",
            ]
        )
        if ok:
            details.append(f"Enabled and started {service_name}-watchdog.service")
        else:
            details.append(
                f"Warning: could not enable {service_name}-watchdog.service: {output}"
            )

    # Enable linger for WSL2 (user services start at boot without login)
    ok, output = _run_cmd(["loginctl", "enable-linger", os.environ.get("USER", "")])
    if ok:
        details.append("Enabled linger (services start at boot)")
    else:
        details.append(f"Note: loginctl enable-linger failed: {output}")
        details.append("Services will start on first login instead of boot")

    return ServiceResult(
        success=True,
        platform="linux",
        message="Service installed.",
        details=details,
    )


def _uninstall_systemd() -> ServiceResult:
    """Uninstall systemd user services.

    Steps:
    1. Stop and disable both services (watchdog first, then server).
    2. Remove unit files.
    3. Reload systemd daemon.

    Returns:
        ServiceResult with outcome details.
    """
    details: list[str] = []
    service_name = conventions.SERVICE_NAME

    # Stop and disable watchdog (ignore errors if not installed)
    for unit in [f"{service_name}-watchdog.service", f"{service_name}.service"]:
        _run_cmd(["systemctl", "--user", "stop", unit])
        _run_cmd(["systemctl", "--user", "disable", unit])
        details.append(f"Stopped and disabled {unit}")

    # Remove unit files
    for path in [_systemd_watchdog_unit_path(), _systemd_server_unit_path()]:
        if path.exists():
            path.unlink()
            details.append(f"Removed {path}")

    # Reload
    _run_cmd(["systemctl", "--user", "daemon-reload"])
    details.append("Reloaded systemd daemon")

    return ServiceResult(
        success=True,
        platform="linux",
        message="Service removed.",
        details=details,
    )


def _status_systemd() -> ServiceResult:
    """Check systemd service status.

    Returns:
        ServiceResult with details about installed/running state.
    """
    details: list[str] = []
    service_name = conventions.SERVICE_NAME

    # Check server
    server_unit = _systemd_server_unit_path()
    if server_unit.exists():
        _ok, output = _run_cmd(
            ["systemctl", "--user", "is-active", f"{service_name}.service"]
        )
        state = output.strip()
        details.append(f"Server service: installed ({state})")
    else:
        details.append("Server service: not installed")

    # Check watchdog
    watchdog_unit = _systemd_watchdog_unit_path()
    if watchdog_unit.exists():
        _ok, output = _run_cmd(
            [
                "systemctl",
                "--user",
                "is-active",
                f"{service_name}-watchdog.service",
            ]
        )
        state = output.strip()
        details.append(f"Watchdog service: installed ({state})")
    else:
        details.append("Watchdog service: not installed")

    installed = server_unit.exists() or watchdog_unit.exists()
    return ServiceResult(
        success=True,
        platform="linux",
        message="Installed" if installed else "Not installed",
        details=details,
    )


# ===========================================================================
# macOS: launchd (user agent)
# ===========================================================================

# Paths:
#   Server:   ~/Library/LaunchAgents/com.amplifier.distro.plist
#   Watchdog: ~/Library/LaunchAgents/com.amplifier.distro.watchdog.plist


def _launchd_dir() -> Path:
    """Return the launchd user agents directory."""
    return Path.home() / "Library" / "LaunchAgents"


def _launchd_server_plist_path() -> Path:
    """Return path for the server launchd plist."""
    return _launchd_dir() / f"{conventions.LAUNCHD_LABEL}.plist"


def _launchd_watchdog_plist_path() -> Path:
    """Return path for the watchdog launchd plist."""
    return _launchd_dir() / f"{conventions.LAUNCHD_LABEL}.watchdog.plist"


def _generate_launchd_server_plist(server_bin: str) -> str:
    """Generate a launchd plist for the server.

    The plist uses ``RunAtLoad`` for boot-time start and ``KeepAlive``
    with ``SuccessfulExit=false`` so launchd restarts on crash.

    Args:
        server_bin: Absolute path to the amp-distro-server binary.

    Returns:
        Complete plist XML content as a string.
    """
    label = conventions.LAUNCHD_LABEL
    port = conventions.SERVER_DEFAULT_PORT
    home = str(Path.home())
    srv_dir = str(
        Path(conventions.AMPLIFIER_HOME).expanduser() / conventions.SERVER_DIR
    )
    path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{label}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{server_bin}</string>
                <string>--host</string>
                <string>127.0.0.1</string>
                <string>--port</string>
                <string>{port}</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <dict>
                <key>SuccessfulExit</key>
                <false/>
            </dict>
            <key>WorkingDirectory</key>
            <string>{home}</string>
            <key>StandardOutPath</key>
            <string>{srv_dir}/launchd-stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{srv_dir}/launchd-stderr.log</string>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>{path_env}</string>
            </dict>
        </dict>
        </plist>
    """)


def _generate_launchd_watchdog_plist(python_bin: str) -> str:
    """Generate a launchd plist for the watchdog.

    Uses ``KeepAlive=true`` so the watchdog always restarts if it exits.

    Args:
        python_bin: Absolute path to the Python interpreter.

    Returns:
        Complete plist XML content as a string.
    """
    label = f"{conventions.LAUNCHD_LABEL}.watchdog"
    port = conventions.SERVER_DEFAULT_PORT
    home = str(Path.home())
    srv_dir = str(
        Path(conventions.AMPLIFIER_HOME).expanduser() / conventions.SERVER_DIR
    )
    path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{label}</string>
            <key>ProgramArguments</key>
            <array>
                <string>{python_bin}</string>
                <string>-m</string>
                <string>amplifier_distro.server.watchdog</string>
                <string>--host</string>
                <string>127.0.0.1</string>
                <string>--port</string>
                <string>{port}</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>WorkingDirectory</key>
            <string>{home}</string>
            <key>StandardOutPath</key>
            <string>{srv_dir}/watchdog-launchd-stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{srv_dir}/watchdog-launchd-stderr.log</string>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PATH</key>
                <string>{path_env}</string>
            </dict>
        </dict>
        </plist>
    """)


def _install_launchd(include_watchdog: bool) -> ServiceResult:
    """Install launchd user agents.

    Steps:
    1. Find amp-distro-server binary.
    2. Create ~/Library/LaunchAgents/ if needed.
    3. Generate and write server plist.
    4. Load server plist via launchctl.
    5. If include_watchdog: generate, write, and load watchdog plist.

    Args:
        include_watchdog: Whether to also install the watchdog agent.

    Returns:
        ServiceResult with outcome details.
    """
    server_bin = _find_server_binary()
    if server_bin is None:
        return ServiceResult(
            success=False,
            platform="macos",
            message="amp-distro-server not found on PATH.",
            details=["Install amplifier-distro first: uv pip install amplifier-distro"],
        )

    details: list[str] = []

    # Create directory
    launchd_dir = _launchd_dir()
    launchd_dir.mkdir(parents=True, exist_ok=True)

    # Write and load server plist
    server_plist = _launchd_server_plist_path()
    server_plist.write_text(_generate_launchd_server_plist(server_bin))
    details.append(f"Wrote {server_plist}")

    ok, output = _run_cmd(["launchctl", "load", "-w", str(server_plist)])
    if ok:
        details.append("Loaded server agent")
    else:
        details.append(f"Warning: launchctl load failed: {output}")

    # Write and load watchdog plist
    if include_watchdog:
        python_bin = _find_python_binary()
        watchdog_plist = _launchd_watchdog_plist_path()
        watchdog_plist.write_text(_generate_launchd_watchdog_plist(python_bin))
        details.append(f"Wrote {watchdog_plist}")

        ok, output = _run_cmd(["launchctl", "load", "-w", str(watchdog_plist)])
        if ok:
            details.append("Loaded watchdog agent")
        else:
            details.append(f"Warning: launchctl load failed: {output}")

    return ServiceResult(
        success=True,
        platform="macos",
        message="Service installed.",
        details=details,
    )


def _uninstall_launchd() -> ServiceResult:
    """Uninstall launchd user agents.

    Steps:
    1. Unload both plists via launchctl.
    2. Remove plist files.

    Returns:
        ServiceResult with outcome details.
    """
    details: list[str] = []

    for plist_path in [_launchd_watchdog_plist_path(), _launchd_server_plist_path()]:
        if plist_path.exists():
            _run_cmd(["launchctl", "unload", str(plist_path)])
            plist_path.unlink()
            details.append(f"Unloaded and removed {plist_path}")

    return ServiceResult(
        success=True,
        platform="macos",
        message="Service removed.",
        details=details,
    )


def _status_launchd() -> ServiceResult:
    """Check launchd service status.

    Returns:
        ServiceResult with installed/running details.
    """
    details: list[str] = []
    label = conventions.LAUNCHD_LABEL

    # Check server
    server_plist = _launchd_server_plist_path()
    if server_plist.exists():
        ok, _output = _run_cmd(["launchctl", "list", label])
        if ok:
            details.append("Server agent: installed (loaded)")
        else:
            details.append("Server agent: installed (not loaded)")
    else:
        details.append("Server agent: not installed")

    # Check watchdog
    watchdog_plist = _launchd_watchdog_plist_path()
    if watchdog_plist.exists():
        ok, _output = _run_cmd(["launchctl", "list", f"{label}.watchdog"])
        if ok:
            details.append("Watchdog agent: installed (loaded)")
        else:
            details.append("Watchdog agent: installed (not loaded)")
    else:
        details.append("Watchdog agent: not installed")

    installed = server_plist.exists() or watchdog_plist.exists()
    return ServiceResult(
        success=True,
        platform="macos",
        message="Installed" if installed else "Not installed",
        details=details,
    )
