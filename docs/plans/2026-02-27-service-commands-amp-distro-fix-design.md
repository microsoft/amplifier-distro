# Service Commands `amp-distro` Fix Design

## Goal

Fix `amp-distro service install/uninstall/status` to use the `amp-distro` CLI (specifically `amp-distro serve`) instead of the deprecated `amp-distro-server` binary.

## Background

The service commands currently fail because they look for `amp-distro-server`, which should not exist. The `amp-distro-server` binary was added as a console script entry point in commit `1ceb6d8` but is now deprecated. The fix routes everything through the single `amp-distro` binary using a single-binary pattern.

## Approach

**Option B — Route everything through `amp-distro`.**

`amp-distro` is the sole entry point for service management, foreground serving, and watchdog supervision. `amp-distro-server` is deprecated and its console script entry point should not exist. This eliminates the broken dependency and simplifies the binary surface area.

## Architecture

Five files change to implement the fix.

## Components

### 1. `pyproject.toml`

- Revert the `amp-distro-server` console script entry point (added in commit `1ceb6d8`).
- Bump version `0.2.0 → 0.3.0` — removing a public binary is a breaking change per semver.

### 2. `service.py`

- Rename `_find_server_binary()` → `_find_distro_binary()`.
- Resolution order: `Path(sys.argv[0]).resolve()` first (the binary currently running the command), then `shutil.which("amp-distro")` as fallback.
- Server unit `ExecStart` → `amp-distro serve --host {host} --port {port}`.
- Watchdog unit `ExecStart` → `amp-distro watchdog --host {host} --port {port}`.
- Change `Restart=always` → `Restart=on-failure` in the generated server unit (prevents auto-restart fighting manual stops).

### 3. `cli.py`

- Add `amp-distro watchdog` as a **hidden** subcommand (`hidden=True` in Click — does not appear in `--help`).
- Accepts `--host` and `--port`.
- Delegates to `run_watchdog_loop()` synchronously (blocking foreground call).
- This is infrastructure for the service unit only — not a user-facing feature.

### 4. `watchdog.py`

Two changes in `_restart_server()`:

**Supervisor detection:** Check `os.environ.get("INVOCATION_ID")` (systemd) or `os.environ.get("LAUNCHD_JOB_NAME")` (launchd). If under a supervisor → call `stop_process(server_pid)` then `sys.exit(1)`. The supervisor sees the exit and restarts `amp-distro serve` cleanly — no double-restart race, no orphan processes.

**Standalone path:** Wrap `daemonize()` in `try/except RuntimeError` → `logger.warning("Port still busy — will retry next cycle")` + `return`. Preserves `first_failure_time` so `max_restarts` counting stays accurate.

### 5. `scripts/amplifier-distro.service`

- Update static template: `ExecStart=amp-distro serve --host 127.0.0.1 --port 8400`.

## Data Flow

```
amp-distro service install
  └─► _find_distro_binary()
        ├─ try: Path(sys.argv[0]).resolve()   # binary running right now
        └─ fallback: shutil.which("amp-distro")
  └─► writes unit file: ExecStart=amp-distro serve --host ... --port ...
  └─► writes watchdog unit: ExecStart=amp-distro watchdog --host ... --port ...

systemd/launchd starts service
  └─► amp-distro serve         (server unit)
  └─► amp-distro watchdog      (watchdog unit, hidden subcommand)
        └─► run_watchdog_loop()
              └─► health check fails
                    ├─ INVOCATION_ID set (systemd):   stop_process() + sys.exit(1)
                    ├─ LAUNCHD_JOB_NAME set (launchd): stop_process() + sys.exit(1)
                    └─ standalone: daemonize() with RuntimeError catch → retry next cycle
```

## Error Handling

**Binary not found.** `_find_distro_binary()` tries `Path(sys.argv[0]).resolve()` first, then `shutil.which("amp-distro")`. If both fail:

```
Failed: amp-distro binary not found.
  Ensure ~/.local/bin is on PATH, or reinstall: uv tool install amplifier-distro
```

**Stale unit files (migration path).** Users with existing service installs have unit files referencing `amp-distro-server`. `service status` detects this — if the existing unit file's `ExecStart` references `amp-distro-server`, print:

```
Warning: installed service references deprecated 'amp-distro-server'.
  Run: amp-distro service uninstall && amp-distro service install
```

**Watchdog port-busy restart (standalone only).** When `daemonize()` raises `RuntimeError` (port still bound after server stop): catch it, log `logger.warning`, return. Watchdog continues its loop and retries next health check interval. `first_failure_time` is not reset so `max_restarts` stays accurate. Under a service manager this path is never reached — the supervisor handles restart.

## Platform Coverage

| Platform           | Status                                                                                                    |
|--------------------|-----------------------------------------------------------------------------------------------------------|
| Linux (systemd)    | Supported                                                                                                 |
| macOS (launchd)    | Supported                                                                                                 |
| WSL2 (systemd enabled) | Supported                                                                                             |
| Windows            | Unsupported — `service install` prints "unsupported platform". `amp-distro serve` works fine in foreground. Windows service support tracked in GitHub issue #21. |
| FreeBSD/other Unix | Unsupported (unchanged from current)                                                                      |

The supervisor detection env vars (`INVOCATION_ID`, `LAUNCHD_JOB_NAME`) are Linux/macOS-specific. On the standalone path, `daemonize()` uses `os.fork()` (Unix-only). Windows users manage the process themselves or use Task Scheduler/NSSM.

## Testing Strategy

**Update existing `test_service.py`** (5 test classes need assertion updates):

- Server unit `ExecStart` → `amp-distro serve`
- Watchdog unit `ExecStart` → `amp-distro watchdog`
- Binary lookup: mock `sys.argv[0]` and `shutil.which` to verify `_find_distro_binary()` precedence
- `Restart=on-failure` in generated server unit
- Stale unit file detection warning in `service_status()`

**New tests for `amp-distro watchdog` subcommand** (using Click `CliRunner`):

- `watchdog --help` exits 0
- `watchdog --host 0.0.0.0 --port 9000` calls `run_watchdog_loop(host="0.0.0.0", port=9000)` (mocked)

**New tests for watchdog supervisor detection (`test_watchdog.py`)**:

- `INVOCATION_ID` set → `_restart_server()` calls `stop_process()` + `sys.exit(1)`, does NOT call `daemonize()`
- `LAUNCHD_JOB_NAME` set → same behavior
- Neither set (standalone) → calls `daemonize()`, `RuntimeError` caught and logged as warning

## Open Questions

None — design is fully validated.

## Related Issues

- **GitHub #19**: `amp-distro service` commands broken + distro-server deprecation decision needed
- **GitHub #21**: feat: Windows service support for `amp-distro service install`
