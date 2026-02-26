"""Server watchdog: health monitoring and automatic restart.

A lightweight standalone process that monitors the distro server by polling
its health endpoint. If the server is unresponsive for a sustained period
(default 5 minutes), the watchdog restarts it.

The watchdog is separate from the server process -- if the server crashes,
the watchdog survives. All paths are from conventions.py.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

from amplifier_distro import conventions
from amplifier_distro.server.daemon import (
    cleanup_pid,
    daemonize,
    is_running,
    pid_file_path,
    server_dir,
    stop_process,
    write_pid,
)

logger = logging.getLogger(__name__)

# Module-level shutdown flag, set by signal handlers
_shutdown = False


# ---------------------------------------------------------------------------
# Path helpers (mirror daemon.py pattern exactly)
# ---------------------------------------------------------------------------


def watchdog_pid_file_path() -> Path:
    """Return the watchdog PID file path, constructed from conventions."""
    return server_dir() / conventions.WATCHDOG_PID_FILE


def watchdog_log_file_path() -> Path:
    """Return the watchdog log file path, constructed from conventions."""
    return server_dir() / conventions.WATCHDOG_LOG_FILE


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def check_health(url: str, timeout: float = 5.0) -> bool:
    """Check if the server health endpoint responds with HTTP 200.

    Uses stdlib urllib to avoid adding dependencies to the watchdog.

    Args:
        url: Full URL to the health endpoint
             (e.g., ``http://127.0.0.1:8400/api/health``).
        timeout: Request timeout in seconds.

    Returns:
        True if the endpoint returns HTTP 200.
    """
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)  # noqa: S310
        return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Watchdog loop
# ---------------------------------------------------------------------------


def _setup_watchdog_logging() -> None:
    """Configure logging for the watchdog process.

    Logs to both the watchdog log file and stderr. Uses a simple format
    (not JSON) since the watchdog is a lightweight monitor.
    """
    log_file = watchdog_log_file_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        str(log_file), maxBytes=5 * 1024 * 1024, backupCount=2
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)


def _signal_handler(signum: int, frame: object) -> None:
    """Handle SIGTERM/SIGINT by setting the shutdown flag."""
    global _shutdown
    _shutdown = True
    logger.info("Received signal %d, shutting down...", signum)


def run_watchdog_loop(
    *,
    host: str = "127.0.0.1",
    port: int = conventions.SERVER_DEFAULT_PORT,
    check_interval: int = 30,
    restart_after: int = 300,
    max_restarts: int = 5,
    apps_dir: str | None = None,
    dev: bool = False,
) -> None:
    """Run the watchdog loop in the foreground (blocking).

    Monitors the server health endpoint and restarts the server if it has
    been continuously unresponsive for ``restart_after`` seconds.

    Handles SIGTERM/SIGINT for clean shutdown. Writes its own PID file.

    Args:
        host: Server bind host (for health URL and restart).
        port: Server bind port.
        check_interval: Seconds between health checks.
        restart_after: Seconds of sustained downtime before restart.
        max_restarts: Maximum restarts per watchdog session (0 = unlimited).
        apps_dir: Optional server apps directory.
        dev: Server dev mode flag.
    """
    global _shutdown
    _shutdown = False

    # Register signal handlers
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Write our own PID file
    wd_pid_file = watchdog_pid_file_path()
    write_pid(wd_pid_file)

    health_url = f"http://{host}:{port}/api/health"
    logger.info(
        "Watchdog started (PID %d), monitoring %s every %ds",
        os.getpid(),
        health_url,
        check_interval,
    )
    logger.info(
        "Will restart server after %ds of sustained downtime (max_restarts=%d)",
        restart_after,
        max_restarts,
    )

    first_failure_time: float | None = None
    restart_count = 0

    try:
        while not _shutdown:
            time.sleep(check_interval)
            if _shutdown:
                break

            healthy = check_health(health_url)

            if healthy:
                if first_failure_time is not None:
                    elapsed = time.monotonic() - first_failure_time
                    logger.info("Server recovered after %ds of downtime", int(elapsed))
                    first_failure_time = None
                continue

            # Server is unhealthy
            if first_failure_time is None:
                first_failure_time = time.monotonic()
                logger.warning("Server health check failed, monitoring...")
                continue

            elapsed = time.monotonic() - first_failure_time
            if elapsed < restart_after:
                logger.warning(
                    "Server unhealthy for %ds (threshold: %ds)",
                    int(elapsed),
                    restart_after,
                )
                continue

            # Threshold exceeded -- restart
            if max_restarts > 0 and restart_count >= max_restarts:
                logger.error(
                    "Max restarts (%d) reached, watchdog giving up",
                    max_restarts,
                )
                break

            logger.warning(
                "Server down for %ds (>= %ds threshold), restarting... (restart %d/%s)",
                int(elapsed),
                restart_after,
                restart_count + 1,
                str(max_restarts) if max_restarts > 0 else "unlimited",
            )
            _restart_server(host, port, apps_dir, dev)
            restart_count += 1
            first_failure_time = None  # Reset after restart attempt
    finally:
        cleanup_pid(wd_pid_file)
        logger.info("Watchdog stopped (restarts performed: %d)", restart_count)


def _restart_server(
    host: str,
    port: int,
    apps_dir: str | None,
    dev: bool,
) -> None:
    """Stop the server (if running) and start a fresh instance.

    Uses daemon.stop_process for graceful shutdown (SIGTERM then SIGKILL),
    then daemon.daemonize to spawn a new process.

    Args:
        host: Server bind host.
        port: Server bind port.
        apps_dir: Optional apps directory.
        dev: Dev mode flag.
    """
    server_pid = pid_file_path()

    # Stop existing server if running
    if is_running(server_pid):
        logger.info("Stopping existing server...")
        stop_process(server_pid)
        # Brief pause for port release
        time.sleep(2)

    # Start fresh server
    pid = daemonize(host=host, port=port, apps_dir=apps_dir, dev=dev)
    logger.info("Server restarted (PID %d)", pid)


# ---------------------------------------------------------------------------
# Watchdog process management (called from CLI)
# ---------------------------------------------------------------------------


def start_watchdog(
    *,
    host: str = "127.0.0.1",
    port: int = conventions.SERVER_DEFAULT_PORT,
    check_interval: int = 30,
    restart_after: int = 300,
    max_restarts: int = 5,
    apps_dir: str | None = None,
    dev: bool = False,
) -> int:
    """Spawn the watchdog as a detached background process.

    Follows the same pattern as daemon.daemonize(): uses subprocess.Popen
    with start_new_session=True, writes PID file.

    Args:
        host: Server host to monitor.
        port: Server port to monitor.
        check_interval: Seconds between health checks.
        restart_after: Seconds of sustained downtime before restart.
        max_restarts: Max restarts per session (0 = unlimited).
        apps_dir: Optional server apps directory.
        dev: Server dev mode flag.

    Returns:
        The PID of the spawned watchdog process.
    """
    cmd = [
        sys.executable,
        "-m",
        "amplifier_distro.server.watchdog",
        "--host",
        host,
        "--port",
        str(port),
        "--check-interval",
        str(check_interval),
        "--restart-after",
        str(restart_after),
        "--max-restarts",
        str(max_restarts),
    ]
    if apps_dir:
        cmd.extend(["--apps-dir", apps_dir])
    if dev:
        cmd.append("--dev")

    crash_log = server_dir() / conventions.WATCHDOG_CRASH_LOG_FILE
    crash_log.parent.mkdir(parents=True, exist_ok=True)
    crash_fh = open(crash_log, "a")  # noqa: SIM115

    process = subprocess.Popen(
        cmd,
        stdout=crash_fh,
        stderr=crash_fh,
        start_new_session=True,
    )
    crash_fh.close()  # Parent doesn't need the fd

    wd_pid_file = watchdog_pid_file_path()
    write_pid(wd_pid_file, process.pid)
    return process.pid


def stop_watchdog() -> bool:
    """Stop the running watchdog process.

    Returns:
        True if the watchdog was stopped (or was already gone).
        False if the PID file was missing/unreadable.
    """
    return stop_process(watchdog_pid_file_path())


def is_watchdog_running() -> bool:
    """Check if the watchdog process is alive."""
    return is_running(watchdog_pid_file_path())


# ---------------------------------------------------------------------------
# Module entry point (for background spawning via python -m)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Amplifier Distro Server Watchdog")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=conventions.SERVER_DEFAULT_PORT)
    parser.add_argument("--check-interval", type=int, default=30)
    parser.add_argument("--restart-after", type=int, default=300)
    parser.add_argument("--max-restarts", type=int, default=5)
    parser.add_argument("--apps-dir", default=None)
    parser.add_argument("--dev", action="store_true")
    args = parser.parse_args()

    _setup_watchdog_logging()
    run_watchdog_loop(
        host=args.host,
        port=args.port,
        check_interval=args.check_interval,
        restart_after=args.restart_after,
        max_restarts=args.max_restarts,
        apps_dir=args.apps_dir,
        dev=args.dev,
    )
