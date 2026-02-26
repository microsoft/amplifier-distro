"""Server lifecycle management: PID files, process control, daemonization.

Provides utilities for managing the distro server as a background process
with PID file tracking for clean start/stop/restart operations.

All paths are constructed from conventions.py constants — no hardcoded paths.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from amplifier_distro import conventions


def server_dir() -> Path:
    """Return the server directory path, constructed from conventions."""
    return Path(conventions.AMPLIFIER_HOME).expanduser() / conventions.SERVER_DIR


def pid_file_path() -> Path:
    """Return the PID file path, constructed from conventions."""
    return server_dir() / conventions.SERVER_PID_FILE


def write_pid(pid_file: Path, pid: int | None = None) -> None:
    """Write a PID to a file, creating parent directories as needed.

    Args:
        pid_file: Path to write the PID file.
        pid: Process ID to write. Defaults to current process PID.
    """
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid if pid is not None else os.getpid()))


def read_pid(pid_file: Path) -> int | None:
    """Read PID from a file.

    Returns:
        The PID as an integer, or None if the file is missing or invalid.
    """
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def is_running(pid_file: Path) -> bool:
    """Check if the process referenced by a PID file is alive.

    Returns:
        True if the process exists and is running.
    """
    pid = read_pid(pid_file)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # Signal 0 checks existence without killing
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it


def cleanup_pid(pid_file: Path) -> None:
    """Remove a PID file if it exists."""
    with contextlib.suppress(OSError):
        pid_file.unlink(missing_ok=True)


def is_port_in_use(host: str, port: int) -> bool:
    """Check if a port is already in use."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def wait_for_health(
    host: str = "127.0.0.1",
    port: int = conventions.SERVER_DEFAULT_PORT,
    timeout: float = 15.0,
    interval: float = 0.5,
) -> bool:
    """Wait for the server health endpoint to respond after startup.

    Polls http://{host}:{port}/api/health until it returns 200 or timeout.

    Returns:
        True if server became healthy within timeout.
    """
    import urllib.error
    import urllib.request

    url = f"http://{host}:{port}/api/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)  # noqa: S310
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(interval)
    return False


def daemonize(
    host: str = "127.0.0.1",
    port: int = conventions.SERVER_DEFAULT_PORT,
    apps_dir: str | None = None,
    dev: bool = False,
    check_port: bool = True,
) -> int:
    """Spawn the server as a detached background process.

    Starts a new Python process running the server module and writes
    its PID to the conventional PID file location.

    Args:
        host: Bind host address.
        port: Bind port number.
        apps_dir: Optional apps directory path.
        dev: Enable dev mode.
        check_port: If True, raise RuntimeError when the port is busy.

    Returns:
        The PID of the spawned background process.

    Raises:
        RuntimeError: If *check_port* is True and the port is in use.
    """
    if check_port and is_port_in_use(host, port):
        raise RuntimeError(f"Port {port} is already in use")

    cmd = [
        sys.executable,
        "-m",
        "amplifier_distro.server",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if apps_dir:
        cmd.extend(["--apps-dir", apps_dir])
    if dev:
        cmd.append("--dev")

    crash_log = server_dir() / conventions.CRASH_LOG_FILE
    crash_log.parent.mkdir(parents=True, exist_ok=True)
    crash_fh = open(crash_log, "a")  # noqa: SIM115

    process = subprocess.Popen(
        cmd,
        stdout=crash_fh,
        stderr=crash_fh,
        start_new_session=True,
    )
    crash_fh.close()  # Parent doesn't need the fd

    pid_file = pid_file_path()
    write_pid(pid_file, process.pid)
    return process.pid


def stop_process(pid_file: Path, timeout: float = 10.0) -> bool:
    """Stop the process referenced by a PID file.

    Sends SIGTERM and waits up to *timeout* seconds for the process to
    exit.  Falls back to SIGKILL if the process does not exit in time.

    Args:
        pid_file: Path to the PID file.
        timeout: Maximum seconds to wait after SIGTERM.

    Returns:
        True if the process was stopped (or was already gone).
        False if the PID file was missing/unreadable or permission denied.
    """
    pid = read_pid(pid_file)
    if pid is None:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        cleanup_pid(pid_file)
        return True
    except PermissionError:
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            cleanup_pid(pid_file)
            return True

    # Force kill if still alive after timeout
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)

    cleanup_pid(pid_file)
    return True
