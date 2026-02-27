# Service Commands `amp-distro` Fix — Implementation Plan

> **Execution:** Use the `subagent-driven-development` workflow to implement this plan.

**Goal:** Fix `amp-distro service install/uninstall/status` to use `amp-distro serve` (and a hidden `amp-distro watchdog` subcommand) instead of the deprecated `amp-distro-server` binary.

**Architecture:** The `amp-distro` binary becomes the sole entry point for service management, foreground serving, and watchdog supervision. The `amp-distro-server` console script entry point is removed from `pyproject.toml`. Service unit files are updated to reference `amp-distro serve` and `amp-distro watchdog`. The watchdog gains supervisor-aware restart logic so that under systemd/launchd it exits cleanly instead of calling `daemonize()`.

**Tech Stack:** Python 3.11, Click 8, pytest, `unittest.mock`, `click.testing.CliRunner`, setuptools/pyproject.toml.

**Design doc:** `docs/plans/2026-02-27-service-commands-amp-distro-fix-design.md`

---

## ⚠️ Critical context before you start

**All work happens in the worktree, NOT the main repo:**
```
/Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands
```

Every path in this plan is **relative to that worktree root**. When the plan says `distro-server/src/...` that means:
```
/Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server/src/...
```

**Run all commands from inside `distro-server/`:**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
```

**Run tests with uv:**
```bash
uv run pytest tests/test_service.py -v
```

**Commit format:** `type: description` (e.g. `fix:`, `feat:`, `test:`, `chore:`)

---

## What you're changing — overview

| File | Change |
|---|---|
| `distro-server/pyproject.toml` | Remove `amp-distro-server` script, bump version to `0.3.0` |
| `distro-server/src/amplifier_distro/service.py` | Rename `_find_server_binary` → `_find_distro_binary`, update ExecStart strings, `Restart=always` → `Restart=on-failure`, stale unit detection |
| `distro-server/src/amplifier_distro/cli.py` | Add hidden `watchdog` subcommand |
| `distro-server/src/amplifier_distro/server/watchdog.py` | Supervisor detection in `_restart_server()`, catch `RuntimeError` on standalone path |
| `distro-server/scripts/amplifier-distro.service` | Update static `ExecStart` line |
| `distro-server/tests/test_service.py` | Update 8 existing assertions, add `TestFindDistroBinary` and `TestStaleUnitDetection` |
| `distro-server/tests/test_cli.py` | New file — three tests for the hidden `watchdog` subcommand |
| `distro-server/tests/test_watchdog.py` | Add `TestRestartServerSupervisorDetection` with four tests |

---

## Group 1 — pyproject.toml (no TDD needed, just config)

### Task 1: Remove `amp-distro-server` entry point and bump version

**Files:**
- Modify: `distro-server/pyproject.toml`

**Step 1: Read the current file**

Open `distro-server/pyproject.toml`. The relevant section looks like this:
```toml
[project]
name = "amplifier-distro"
version = "0.2.0"
...

[project.scripts]
amp-distro        = "amplifier_distro.cli:main"
amp-distro-server = "amplifier_distro.server.cli:serve"
```

**Step 2: Make both edits**

Change `version = "0.2.0"` to `version = "0.3.0"`.

Remove the `amp-distro-server = "amplifier_distro.server.cli:serve"` line entirely.

The `[project.scripts]` section should end up as:
```toml
[project.scripts]
amp-distro        = "amplifier_distro.cli:main"
```

**Step 3: Verify there's no syntax error**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
python -c "import tomllib; tomllib.loads(open('pyproject.toml').read()); print('OK')"
```
Expected output: `OK`

**Step 4: Commit**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands
git add distro-server/pyproject.toml
git commit -m "chore: remove amp-distro-server entry point, bump version to 0.3.0"
```

---

## Group 2 — service.py (TDD cycle)

### Task 2: Update test_service.py — write all failing tests first

**Files:**
- Modify: `distro-server/tests/test_service.py`

**Step 1: Read the current test file**

Open `distro-server/tests/test_service.py`. You'll find 8 places that need updating plus two new test classes to add.

**Step 2: Add `import sys` to the imports**

The current imports at the top are:
```python
import configparser
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch
```

Add `import sys` so it reads:
```python
import configparser
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch
```

**Step 3: Update `TestSystemdServerUnit` — 3 changes**

**Change 3a** — Update the default `server_bin` argument in `_generate()` (line ~63):
```python
# OLD:
def _generate(
    self,
    server_bin: str = "/usr/local/bin/amp-distro-server",
) -> str:

# NEW:
def _generate(
    self,
    distro_bin: str = "/usr/local/bin/amp-distro",
) -> str:
    from amplifier_distro.service import _generate_systemd_server_unit

    return _generate_systemd_server_unit(distro_bin)
```

**Change 3b** — Update `test_restart_always` to expect `on-failure` (line ~81):
```python
# OLD:
def test_restart_always(self) -> None:
    parser = self._parse(self._generate())
    assert parser["Service"]["Restart"] == "always"

# NEW:
def test_restart_on_failure(self) -> None:
    """Server unit must use Restart=on-failure to allow clean manual stops."""
    parser = self._parse(self._generate())
    assert parser["Service"]["Restart"] == "on-failure"
```

**Change 3c** — Update `test_correct_exec_start` to check for `amp-distro serve` (line ~89):
```python
# OLD:
def test_correct_exec_start(self) -> None:
    content = self._generate("/my/custom/path/amp-distro-server")
    assert "/my/custom/path/amp-distro-server" in content

# NEW:
def test_correct_exec_start(self) -> None:
    """ExecStart uses 'amp-distro serve', not the old standalone binary."""
    content = self._generate("/my/custom/path/amp-distro")
    assert "/my/custom/path/amp-distro" in content
    assert "serve" in content
    assert "amp-distro-server" not in content
```

**Step 4: Update `TestSystemdWatchdogUnit` — 2 changes**

**Change 4a** — Update the default arg in `_generate()` (line ~110):
```python
# OLD:
def _generate(
    self,
    server_bin: str = "/usr/local/bin/amp-distro-server",
) -> str:
    from amplifier_distro.service import _generate_systemd_watchdog_unit

    return _generate_systemd_watchdog_unit(server_bin)

# NEW:
def _generate(
    self,
    distro_bin: str = "/usr/local/bin/amp-distro",
) -> str:
    from amplifier_distro.service import _generate_systemd_watchdog_unit

    return _generate_systemd_watchdog_unit(distro_bin)
```

**Change 4b** — Update `test_runs_watchdog_module` to check for `amp-distro watchdog` (line ~140):
```python
# OLD:
def test_runs_watchdog_module(self) -> None:
    content = self._generate()
    assert "amplifier_distro.server.watchdog" in content

# NEW:
def test_runs_watchdog_subcommand(self) -> None:
    """Watchdog unit ExecStart uses 'amp-distro watchdog', not the python -m form."""
    content = self._generate()
    assert "watchdog" in content
    assert "amplifier_distro.server.watchdog" not in content
    assert "-m" not in content
```

**Step 5: Update `TestLaunchdServerPlist` — 2 changes**

**Change 5a** — Update the default arg in `_generate()` (line ~157):
```python
# OLD:
def _generate(
    self,
    server_bin: str = "/usr/local/bin/amp-distro-server",
) -> str:
    from amplifier_distro.service import _generate_launchd_server_plist

    return _generate_launchd_server_plist(server_bin)

# NEW:
def _generate(
    self,
    distro_bin: str = "/usr/local/bin/amp-distro",
) -> str:
    from amplifier_distro.service import _generate_launchd_server_plist

    return _generate_launchd_server_plist(distro_bin)
```

**Change 5b** — Update `test_correct_program` to check for `amp-distro` and `serve` (line ~177):
```python
# OLD:
def test_correct_program(self) -> None:
    content = self._generate("/my/path/amp-distro-server")
    assert "/my/path/amp-distro-server" in content

# NEW:
def test_correct_program(self) -> None:
    """ProgramArguments contains the distro binary and 'serve' subcommand."""
    content = self._generate("/my/path/amp-distro")
    assert "/my/path/amp-distro" in content
    assert "<string>serve</string>" in content
    assert "amp-distro-server" not in content
```

**Step 6: Update `TestLaunchdWatchdogPlist` — 2 changes**

**Change 6a** — Update `_generate()` signature from `python_bin` to `distro_bin` (line ~197):
```python
# OLD:
def _generate(self, python_bin: str = "/usr/bin/python3") -> str:
    from amplifier_distro.service import (
        _generate_launchd_watchdog_plist,
    )

    return _generate_launchd_watchdog_plist(python_bin)

# NEW:
def _generate(self, distro_bin: str = "/usr/local/bin/amp-distro") -> str:
    from amplifier_distro.service import _generate_launchd_watchdog_plist

    return _generate_launchd_watchdog_plist(distro_bin)
```

**Change 6b** — Update `test_runs_watchdog_module` and `test_correct_python` (lines ~211 and ~220):
```python
# OLD:
def test_runs_watchdog_module(self) -> None:
    content = self._generate()
    assert "amplifier_distro.server.watchdog" in content

def test_correct_python(self) -> None:
    content = self._generate("/my/venv/bin/python3")
    assert "/my/venv/bin/python3" in content

# NEW:
def test_runs_watchdog_subcommand(self) -> None:
    """ProgramArguments uses 'amp-distro watchdog', not python -m."""
    content = self._generate()
    assert "<string>watchdog</string>" in content
    assert "amplifier_distro.server.watchdog" not in content
    assert "<string>-m</string>" not in content

def test_correct_distro_bin(self) -> None:
    """ProgramArguments contains the provided distro binary path."""
    content = self._generate("/my/custom/amp-distro")
    assert "/my/custom/amp-distro" in content
```

**Step 7: Update `TestInstallSystemd` — patch targets change**

Find the two patches that reference `_find_server_binary` in `TestInstallSystemd` (lines ~309, ~335, ~343) and change them to `_find_distro_binary`:

```python
# In test_install_creates_unit_files:
# OLD:
@patch(
    "amplifier_distro.service._find_server_binary",
    return_value="/usr/local/bin/amp-distro-server",
)
# NEW:
@patch(
    "amplifier_distro.service._find_distro_binary",
    return_value="/usr/local/bin/amp-distro",
)

# In test_install_fails_without_binary:
# OLD:
@patch("amplifier_distro.service._find_server_binary", return_value=None)
# NEW:
@patch("amplifier_distro.service._find_distro_binary", return_value=None)

# In test_install_without_watchdog:
# OLD:
@patch(
    "amplifier_distro.service._find_server_binary",
    return_value="/usr/local/bin/amp-distro-server",
)
# NEW:
@patch(
    "amplifier_distro.service._find_distro_binary",
    return_value="/usr/local/bin/amp-distro",
)
```

Also update the `_mock_bin` parameter docstring/comment inside `test_install_fails_without_binary`:
```python
# OLD:
def test_install_fails_without_binary(self, _mock_bin: MagicMock) -> None:
    from amplifier_distro.service import _install_systemd

    result = _install_systemd(include_watchdog=True)
    assert result.success is False
    assert "not found" in result.message

# NEW (message text will change too — assert the key phrase):
def test_install_fails_without_binary(self, _mock_bin: MagicMock) -> None:
    from amplifier_distro.service import _install_systemd

    result = _install_systemd(include_watchdog=True)
    assert result.success is False
    assert "amp-distro" in result.message
```

**Step 8: Add `TestFindDistroBinary` class**

Add this new test class after `TestServiceResult` (at the end of the file):

```python
# ---------------------------------------------------------------------------
# _find_distro_binary()
# ---------------------------------------------------------------------------


class TestFindDistroBinary:
    """Verify _find_distro_binary() resolution order."""

    def test_uses_argv0_when_exists(self, tmp_path: Path) -> None:
        """argv[0] path is returned when it resolves to an existing file."""
        fake_binary = tmp_path / "amp-distro"
        fake_binary.touch()

        from amplifier_distro.service import _find_distro_binary

        with patch.object(sys, "argv", [str(fake_binary)]):
            result = _find_distro_binary()

        assert result == str(fake_binary.resolve())

    def test_falls_back_to_shutil_which(self, tmp_path: Path) -> None:
        """shutil.which is used when argv[0] does not exist on disk."""
        nonexistent = str(tmp_path / "nonexistent-binary")

        from amplifier_distro.service import _find_distro_binary

        with patch.object(sys, "argv", [nonexistent]):
            with patch(
                "amplifier_distro.service.shutil.which",
                return_value="/usr/local/bin/amp-distro",
            ):
                result = _find_distro_binary()

        assert result == "/usr/local/bin/amp-distro"

    def test_returns_none_when_both_fail(self, tmp_path: Path) -> None:
        """Returns None when argv[0] doesn't exist and shutil.which finds nothing."""
        nonexistent = str(tmp_path / "nonexistent-binary")

        from amplifier_distro.service import _find_distro_binary

        with patch.object(sys, "argv", [nonexistent]):
            with patch("amplifier_distro.service.shutil.which", return_value=None):
                result = _find_distro_binary()

        assert result is None
```

**Step 9: Add `TestStaleUnitDetection` class**

Add this class after `TestFindDistroBinary`:

```python
# ---------------------------------------------------------------------------
# Stale unit file detection
# ---------------------------------------------------------------------------


class TestStaleUnitDetection:
    """Verify service_status warns when unit files reference the deprecated binary."""

    def test_status_warns_on_stale_systemd_unit(self, tmp_path: Path) -> None:
        """_status_systemd warns when the server unit references amp-distro-server."""
        unit_file = tmp_path / f"{conventions.SERVICE_NAME}.service"
        unit_file.write_text(
            "[Service]\n"
            "ExecStart=/home/user/.local/bin/amp-distro-server"
            " --host 127.0.0.1 --port 8400\n"
        )

        from amplifier_distro.service import _status_systemd

        with patch(
            "amplifier_distro.service._systemd_server_unit_path",
            return_value=unit_file,
        ):
            with patch(
                "amplifier_distro.service._systemd_watchdog_unit_path",
                return_value=tmp_path / "watchdog.service",
            ):
                with patch(
                    "amplifier_distro.service._run_cmd", return_value=(True, "active")
                ):
                    result = _status_systemd()

        warning = next((d for d in result.details if "deprecated" in d), None)
        assert warning is not None, "Expected a deprecation warning in details"
        assert "amp-distro-server" in warning
        assert "amp-distro service uninstall" in warning

    def test_status_warns_on_stale_launchd_plist(self, tmp_path: Path) -> None:
        """_status_launchd warns when the server plist references amp-distro-server."""
        plist_file = tmp_path / f"{conventions.LAUNCHD_LABEL}.plist"
        plist_file.write_text(
            "<dict><string>/home/user/.local/bin/amp-distro-server</string></dict>\n"
        )

        from amplifier_distro.service import _status_launchd

        with patch(
            "amplifier_distro.service._launchd_server_plist_path",
            return_value=plist_file,
        ):
            with patch(
                "amplifier_distro.service._launchd_watchdog_plist_path",
                return_value=tmp_path / "watchdog.plist",
            ):
                with patch(
                    "amplifier_distro.service._run_cmd", return_value=(True, "0")
                ):
                    result = _status_launchd()

        warning = next((d for d in result.details if "deprecated" in d), None)
        assert warning is not None, "Expected a deprecation warning in details"
        assert "amp-distro-server" in warning
        assert "amp-distro service uninstall" in warning

    def test_no_warning_when_unit_is_current(self, tmp_path: Path) -> None:
        """No deprecation warning when the unit file uses amp-distro serve."""
        unit_file = tmp_path / f"{conventions.SERVICE_NAME}.service"
        unit_file.write_text(
            "[Service]\n"
            "ExecStart=/home/user/.local/bin/amp-distro serve"
            " --host 127.0.0.1 --port 8400\n"
        )

        from amplifier_distro.service import _status_systemd

        with patch(
            "amplifier_distro.service._systemd_server_unit_path",
            return_value=unit_file,
        ):
            with patch(
                "amplifier_distro.service._systemd_watchdog_unit_path",
                return_value=tmp_path / "watchdog.service",
            ):
                with patch(
                    "amplifier_distro.service._run_cmd", return_value=(True, "active")
                ):
                    result = _status_systemd()

        warnings = [d for d in result.details if "deprecated" in d]
        assert len(warnings) == 0, "No deprecation warning expected for current unit"
```

**Step 10: Run ALL service tests — they should ALL fail**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_service.py -v 2>&1 | tail -30
```

Expected: Many failures. You should see errors like:
- `AssertionError: assert 'always' == 'on-failure'`
- `AttributeError: module 'amplifier_distro.service' has no attribute '_find_distro_binary'`
- `AssertionError: amp-distro-server found in content`

If tests are **passing**, something is wrong — re-read the changes above and verify you actually updated the assertions.

---

### Task 3: Implement `_find_distro_binary()` in service.py

**Files:**
- Modify: `distro-server/src/amplifier_distro/service.py`

**Step 1: Read the current file**

Open `distro-server/src/amplifier_distro/service.py`. Find `_find_server_binary()` at around line 135:
```python
def _find_server_binary() -> str | None:
    """Find the amp-distro-server binary on PATH."""
    return shutil.which("amp-distro-server")
```

**Step 2: Replace `_find_server_binary()` with `_find_distro_binary()`**

Replace the entire `_find_server_binary()` function with:
```python
def _find_distro_binary() -> str | None:
    """Find the amp-distro binary, preferring the currently-running binary.

    Resolution order:
    1. ``Path(sys.argv[0]).resolve()`` — the binary currently running this command.
       More reliable than PATH lookup in multi-venv environments.
    2. ``shutil.which("amp-distro")`` — fallback for PATH-based lookup.

    Returns:
        Absolute path string, or None if not found.
    """
    candidate = Path(sys.argv[0]).resolve()
    if candidate.exists():
        return str(candidate)
    return shutil.which("amp-distro")
```

**Step 3: Run the binary-lookup tests to verify they pass**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_service.py::TestFindDistroBinary -v
```
Expected: **3 PASSED**

If they fail, check that `sys` is imported at the top of `service.py` (line 20 — it already is).

---

### Task 4: Update server unit ExecStart and Restart policy

**Files:**
- Modify: `distro-server/src/amplifier_distro/service.py`

**Step 1: Update `_generate_systemd_server_unit()`**

Find `_generate_systemd_server_unit()` (around line 194). The function currently takes `server_bin: str`. Make three changes:

1. Rename the parameter from `server_bin` to `distro_bin`
2. Update the docstring `server_bin` reference to `distro_bin`
3. Change `ExecStart={server_bin} --host...` to `ExecStart={distro_bin} serve --host...`
4. Change `Restart=always` to `Restart=on-failure`

The updated function signature and key lines:
```python
def _generate_systemd_server_unit(distro_bin: str) -> str:
    """Generate the systemd unit file for the server.

    Args:
        distro_bin: Absolute path to the amp-distro binary.

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
        ExecStart={distro_bin} serve --host 127.0.0.1 --port {port}
        Restart=on-failure
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
```

**Step 2: Update `_generate_launchd_server_plist()`**

Find `_generate_launchd_server_plist()` (around line 483). Make two changes:
1. Rename the parameter from `server_bin` to `distro_bin`
2. Add `<string>serve</string>` as the second element in ProgramArguments

The ProgramArguments block should change from:
```xml
<array>
    <string>{server_bin}</string>
    <string>--host</string>
    ...
</array>
```
To:
```xml
<array>
    <string>{distro_bin}</string>
    <string>serve</string>
    <string>--host</string>
    ...
</array>
```

Updated function signature and ProgramArguments in full:
```python
def _generate_launchd_server_plist(distro_bin: str) -> str:
    """Generate a launchd plist for the server.

    The plist uses ``RunAtLoad`` for boot-time start and ``KeepAlive``
    with ``SuccessfulExit=false`` so launchd restarts on crash.

    Args:
        distro_bin: Absolute path to the amp-distro binary.

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
                <string>{distro_bin}</string>
                <string>serve</string>
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
```

**Step 3: Run server unit tests**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_service.py::TestSystemdServerUnit tests/test_service.py::TestLaunchdServerPlist -v
```
Expected: **All PASSED** (7 tests in TestSystemdServerUnit + 7 tests in TestLaunchdServerPlist).

---

### Task 5: Update watchdog unit ExecStart

**Files:**
- Modify: `distro-server/src/amplifier_distro/service.py`

**Step 1: Update `_generate_systemd_watchdog_unit()`**

Find `_generate_systemd_watchdog_unit()` (around line 229). Currently it ignores its `server_bin` parameter and calls `_find_python_binary()` internally to build the ExecStart. After this change:

1. Rename the parameter from `server_bin` to `distro_bin`
2. Remove the `python_bin = _find_python_binary()` call entirely
3. Remove the multi-line `exec_start = ...` assignment
4. Set ExecStart directly using `distro_bin`

The updated function:
```python
def _generate_systemd_watchdog_unit(distro_bin: str) -> str:
    """Generate the systemd unit file for the watchdog.

    The watchdog unit uses ``Restart=always`` so it is always running.
    It uses ``Wants=`` (not ``BindsTo=``) so the watchdog survives
    server death -- that's its whole purpose: detect failure and restart.

    Args:
        distro_bin: Absolute path to the amp-distro binary.

    Returns:
        Complete systemd unit file content as a string.
    """
    path_env = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    port = conventions.SERVER_DEFAULT_PORT
    service_name = conventions.SERVICE_NAME
    amplifier_home = Path(conventions.AMPLIFIER_HOME).expanduser()
    return dedent(f"""\
        [Unit]
        Description=Amplifier Distro Watchdog
        After={service_name}.service
        Wants={service_name}.service

        [Service]
        Type=simple
        ExecStart={distro_bin} watchdog --host 127.0.0.1 --port {port}
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
```

**Step 2: Update `_generate_launchd_watchdog_plist()`**

Find `_generate_launchd_watchdog_plist()` (around line 541). Currently it takes `python_bin: str` and builds ProgramArguments with `python_bin -m amplifier_distro.server.watchdog`. After this change:

1. Rename the parameter from `python_bin` to `distro_bin`
2. Change ProgramArguments from `[python_bin, "-m", "amplifier_distro.server.watchdog", ...]` to `[distro_bin, "watchdog", ...]`

Updated function:
```python
def _generate_launchd_watchdog_plist(distro_bin: str) -> str:
    """Generate a launchd plist for the watchdog.

    Uses ``KeepAlive=true`` so the watchdog always restarts if it exits.

    Args:
        distro_bin: Absolute path to the amp-distro binary.

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
                <string>{distro_bin}</string>
                <string>watchdog</string>
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
```

**Step 3: Update callers — `_install_systemd()` and `_install_launchd()`**

Find `_install_systemd()` (around line 277). It calls `_find_server_binary()` and passes the result as `server_bin`. Change:

```python
# OLD (around line 295):
server_bin = _find_server_binary()
if server_bin is None:
    return ServiceResult(
        success=False,
        platform="linux",
        message="amp-distro-server not found on PATH.",
        details=["Install amplifier-distro first: uv tool install amplifier-distro"]
    )

# NEW:
distro_bin = _find_distro_binary()
if distro_bin is None:
    return ServiceResult(
        success=False,
        platform="linux",
        message="Failed: amp-distro binary not found.",
        details=[
            "Ensure ~/.local/bin is on PATH, or reinstall:",
            "  uv tool install amplifier-distro",
        ],
    )
```

Then update the variable name throughout `_install_systemd()`:
- `server_unit_path.write_text(_generate_systemd_server_unit(server_bin))` → `_generate_systemd_server_unit(distro_bin)`
- `watchdog_unit_path.write_text(_generate_systemd_watchdog_unit(server_bin))` → `_generate_systemd_watchdog_unit(distro_bin)`

Find `_install_launchd()` (around line 597). Make the same changes:

```python
# OLD (around line 613):
server_bin = _find_server_binary()
if server_bin is None:
    return ServiceResult(
        success=False,
        platform="macos",
        message="amp-distro-server not found on PATH.",
        details=["Install amplifier-distro first: uv tool install amplifier-distro"]
    )

# NEW:
distro_bin = _find_distro_binary()
if distro_bin is None:
    return ServiceResult(
        success=False,
        platform="macos",
        message="Failed: amp-distro binary not found.",
        details=[
            "Ensure ~/.local/bin is on PATH, or reinstall:",
            "  uv tool install amplifier-distro",
        ],
    )
```

Then inside `_install_launchd()`, update:
- `server_plist.write_text(_generate_launchd_server_plist(server_bin))` → `_generate_launchd_server_plist(distro_bin)`
- Remove the `python_bin = _find_python_binary()` line (around line 641)
- `watchdog_plist.write_text(_generate_launchd_watchdog_plist(python_bin))` → `_generate_launchd_watchdog_plist(distro_bin)`

Also update `install_service()` (around line 88) to fix the unsupported platform message. Find:
```python
details=[
    "Supported: Linux (systemd), macOS (launchd).",
    "For Windows, use Task Scheduler to run: amp-distro-server start",
],
```
Change to:
```python
details=[
    "Supported: Linux (systemd), macOS (launchd).",
    "For Windows, use Task Scheduler or NSSM to run: amp-distro serve",
    "Windows service support tracked in GitHub issue #21.",
],
```

**Step 4: Remove `_find_python_binary()` if no longer used**

Search the entire `service.py` file for `_find_python_binary`. If no callers remain (they shouldn't after the changes above), remove the function entirely:
```python
# DELETE this entire function:
def _find_python_binary() -> str:
    """Return the current Python interpreter path."""
    return sys.executable
```

**Step 5: Run watchdog unit tests**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_service.py::TestSystemdWatchdogUnit tests/test_service.py::TestLaunchdWatchdogPlist -v
```
Expected: **All PASSED**.

---

### Task 6: Add stale unit file detection to `service_status()`

**Files:**
- Modify: `distro-server/src/amplifier_distro/service.py`

**Step 1: Write the failing tests first (already done in Task 2)**

The `TestStaleUnitDetection` class was written in Task 2. Verify the stale tests are still failing at this point:
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_service.py::TestStaleUnitDetection -v
```
Expected: **3 FAILED** (no warning logic exists yet).

**Step 2: Add stale detection to `_status_systemd()`**

Find `_status_systemd()` (around line 414). After the block that reads the existing server unit file:

```python
# Find this block (around line 424):
server_unit = _systemd_server_unit_path()
if server_unit.exists():
    _ok, output = _run_cmd(
        ["systemctl", "--user", "is-active", f"{service_name}.service"]
    )
    state = output.strip()
    details.append(f"Server service: installed ({state})")
else:
    details.append("Server service: not installed")
```

Add the stale check INSIDE the `if server_unit.exists():` block, after the `details.append(...)` line:
```python
server_unit = _systemd_server_unit_path()
if server_unit.exists():
    _ok, output = _run_cmd(
        ["systemctl", "--user", "is-active", f"{service_name}.service"]
    )
    state = output.strip()
    details.append(f"Server service: installed ({state})")
    # Detect stale unit files from before the amp-distro-server deprecation
    if "amp-distro-server" in server_unit.read_text():
        details.append(
            "Warning: installed service references deprecated 'amp-distro-server'.\n"
            "  Run: amp-distro service uninstall && amp-distro service install"
        )
else:
    details.append("Server service: not installed")
```

**Step 3: Add stale detection to `_status_launchd()`**

Find `_status_launchd()` (around line 686). Do the same for the server plist check:

```python
# Find this block:
server_plist = _launchd_server_plist_path()
if server_plist.exists():
    ok, _output = _run_cmd(["launchctl", "list", label])
    if ok:
        details.append("Server agent: installed (loaded)")
    else:
        details.append("Server agent: installed (not loaded)")
else:
    details.append("Server agent: not installed")
```

Add the stale check after the `details.append(...)` lines inside `if server_plist.exists():`:
```python
server_plist = _launchd_server_plist_path()
if server_plist.exists():
    ok, _output = _run_cmd(["launchctl", "list", label])
    if ok:
        details.append("Server agent: installed (loaded)")
    else:
        details.append("Server agent: installed (not loaded)")
    # Detect stale plists from before the amp-distro-server deprecation
    if "amp-distro-server" in server_plist.read_text():
        details.append(
            "Warning: installed service references deprecated 'amp-distro-server'.\n"
            "  Run: amp-distro service uninstall && amp-distro service install"
        )
else:
    details.append("Server agent: not installed")
```

**Step 4: Run stale detection tests**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_service.py::TestStaleUnitDetection -v
```
Expected: **3 PASSED**

---

### Task 7: Run ALL service tests — all must pass. Commit.

**Step 1: Run the full test_service.py**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_service.py -v
```
Expected: **All PASSED**. If anything fails, fix it before moving on.

**Step 2: Commit**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands
git add distro-server/src/amplifier_distro/service.py distro-server/tests/test_service.py
git commit -m "fix: update service.py to use amp-distro binary instead of amp-distro-server"
```

---

## Group 3 — cli.py (TDD cycle)

### Task 8: Write failing CLI tests in new test_cli.py

**Files:**
- Create: `distro-server/tests/test_cli.py`

**Step 1: Create the new test file with this exact content:**

```python
"""Tests for the amp-distro CLI main command group.

Tests cover:
1. Hidden 'watchdog' subcommand visibility
2. watchdog --help exits cleanly
3. watchdog delegates to run_watchdog_loop with correct args
"""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner


class TestWatchdogSubcommand:
    """Verify the hidden 'watchdog' subcommand behavior."""

    def test_watchdog_subcommand_hidden(self) -> None:
        """'watchdog' must NOT appear in 'amp-distro --help' output."""
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["--help"])

        assert result.exit_code == 0
        assert "watchdog" not in result.output

    def test_watchdog_subcommand_exists(self) -> None:
        """'amp-distro watchdog --help' must succeed with exit code 0."""
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["watchdog", "--help"])

        assert result.exit_code == 0

    @patch("amplifier_distro.server.watchdog.run_watchdog_loop")
    def test_watchdog_delegates_to_run_watchdog_loop(
        self, mock_loop: MagicMock
    ) -> None:
        """'amp-distro watchdog --host X --port Y' calls run_watchdog_loop(host=X, port=Y)."""
        from amplifier_distro.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["watchdog", "--host", "0.0.0.0", "--port", "9000"])

        assert result.exit_code == 0
        mock_loop.assert_called_once_with(host="0.0.0.0", port=9000)
```

**Step 2: Run the new tests — they should all fail**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_cli.py -v
```
Expected: **3 FAILED** — the `watchdog` subcommand doesn't exist yet.

---

### Task 9: Add the hidden `watchdog` subcommand to cli.py

**Files:**
- Modify: `distro-server/src/amplifier_distro/cli.py`

**Step 1: Read the current file**

Open `distro-server/src/amplifier_distro/cli.py`. The file ends at line 196 with the `service_cmd_status` function.

**Step 2: Add the watchdog subcommand at the end of the file**

After the last line of `service_cmd_status` (the `click.echo(f"  {detail}")` line), add:

```python

# -- Watchdog (hidden: for service supervision only) ---------------------


@main.command("watchdog", hidden=True)
@click.option("--host", default="127.0.0.1", help="Bind host")
@click.option(
    "--port",
    default=conventions.SERVER_DEFAULT_PORT,
    type=int,
    help="Bind port",
)
def watchdog_cmd(host: str, port: int) -> None:
    """Run the health watchdog (for service supervision — not user-facing)."""
    from .server.watchdog import run_watchdog_loop

    run_watchdog_loop(host=host, port=port)
```

**Step 3: Run the CLI tests — all should pass**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_cli.py -v
```
Expected: **3 PASSED**

**Step 4: Commit**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands
git add distro-server/src/amplifier_distro/cli.py distro-server/tests/test_cli.py
git commit -m "feat: add hidden watchdog subcommand to amp-distro CLI"
```

---

## Group 4 — watchdog.py (TDD cycle)

### Task 10: Write failing supervisor detection tests

**Files:**
- Modify: `distro-server/tests/test_watchdog.py`

**Step 1: Add missing imports at the top of test_watchdog.py**

The current imports at the top of `test_watchdog.py`:
```python
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch
```

Add `import os` and `import pytest`:
```python
import os
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
```

**Step 2: Add `TestRestartServerSupervisorDetection` class at the end of the file**

After the last existing test class (`TestWatchdogCli`), add this new class:

```python
# ---------------------------------------------------------------------------
# _restart_server() supervisor detection
# ---------------------------------------------------------------------------


class TestRestartServerSupervisorDetection:
    """Verify _restart_server() uses supervisor-aware restart under systemd/launchd."""

    @patch("amplifier_distro.server.watchdog.daemonize")
    @patch("amplifier_distro.server.watchdog.stop_process")
    def test_restart_under_systemd_exits_not_daemonize(
        self,
        mock_stop: MagicMock,
        mock_daemonize: MagicMock,
    ) -> None:
        """Under systemd (INVOCATION_ID set): stops server and exits 1, never daemonizes."""
        from amplifier_distro.server.watchdog import _restart_server

        with patch.dict(os.environ, {"INVOCATION_ID": "abc123"}, clear=True):
            with pytest.raises(SystemExit) as exc_info:
                _restart_server("127.0.0.1", 8400, None, False)

        assert exc_info.value.code == 1
        mock_stop.assert_called_once()
        mock_daemonize.assert_not_called()

    @patch("amplifier_distro.server.watchdog.daemonize")
    @patch("amplifier_distro.server.watchdog.stop_process")
    def test_restart_under_launchd_exits_not_daemonize(
        self,
        mock_stop: MagicMock,
        mock_daemonize: MagicMock,
    ) -> None:
        """Under launchd (LAUNCHD_JOB_NAME set): stops server and exits 1, never daemonizes."""
        from amplifier_distro.server.watchdog import _restart_server

        with patch.dict(
            os.environ, {"LAUNCHD_JOB_NAME": "com.amplifier.distro"}, clear=True
        ):
            with pytest.raises(SystemExit) as exc_info:
                _restart_server("127.0.0.1", 8400, None, False)

        assert exc_info.value.code == 1
        mock_stop.assert_called_once()
        mock_daemonize.assert_not_called()

    @patch("amplifier_distro.server.watchdog.daemonize")
    @patch("amplifier_distro.server.watchdog.is_running", return_value=False)
    def test_restart_standalone_calls_daemonize(
        self,
        _mock_is_running: MagicMock,
        mock_daemonize: MagicMock,
    ) -> None:
        """Standalone (no supervisor env vars): calls daemonize() normally."""
        mock_daemonize.return_value = 12345

        from amplifier_distro.server.watchdog import _restart_server

        with patch.dict(os.environ, {}, clear=True):
            _restart_server("127.0.0.1", 8400, None, False)

        mock_daemonize.assert_called_once_with(
            host="127.0.0.1", port=8400, apps_dir=None, dev=False
        )

    @patch("amplifier_distro.server.watchdog.logger")
    @patch("amplifier_distro.server.watchdog.daemonize")
    @patch("amplifier_distro.server.watchdog.is_running", return_value=False)
    def test_restart_port_busy_logs_warning_and_returns(
        self,
        _mock_is_running: MagicMock,
        mock_daemonize: MagicMock,
        mock_logger: MagicMock,
    ) -> None:
        """When daemonize() raises RuntimeError: logs warning and returns (no re-raise)."""
        mock_daemonize.side_effect = RuntimeError("Address already in use")

        from amplifier_distro.server.watchdog import _restart_server

        with patch.dict(os.environ, {}, clear=True):
            # Must NOT raise — the function should catch RuntimeError and return
            _restart_server("127.0.0.1", 8400, None, False)

        mock_logger.warning.assert_called_once()
```

**Step 3: Run the new tests — they should all fail**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_watchdog.py::TestRestartServerSupervisorDetection -v
```
Expected: **4 FAILED**. If tests PASS already, something is wrong — the implementation shouldn't exist yet.

---

### Task 11: Implement supervisor detection and RuntimeError catch in `_restart_server()`

**Files:**
- Modify: `distro-server/src/amplifier_distro/server/watchdog.py`

**Step 1: Read the current `_restart_server()` function**

Open `distro-server/src/amplifier_distro/server/watchdog.py`. Find `_restart_server()` at around line 227:

```python
def _restart_server(
    host: str,
    port: int,
    apps_dir: str | None,
    dev: bool,
) -> None:
    ...
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
```

**Step 2: Replace the function body with the new implementation**

Replace the entire body of `_restart_server()` with:

```python
def _restart_server(
    host: str,
    port: int,
    apps_dir: str | None,
    dev: bool,
) -> None:
    """Stop the server (if running) and start a fresh instance.

    **Supervisor-managed path (systemd/launchd):** If ``INVOCATION_ID``
    (systemd) or ``LAUNCHD_JOB_NAME`` (launchd) is set in the environment,
    we stop the server and exit with code 1. The supervisor sees the
    non-zero exit and restarts both ``amp-distro serve`` and this watchdog
    cleanly, avoiding double-restart races and orphan processes.

    **Standalone path:** Stop the server, pause for port release, then
    spawn a fresh instance via ``daemonize()``. If the port is still busy,
    log a warning and return -- the watchdog loop will retry on the next
    health check interval.

    Args:
        host: Server bind host.
        port: Server bind port.
        apps_dir: Optional apps directory.
        dev: Dev mode flag.
    """
    server_pid = pid_file_path()

    # If running under a service manager, stop the server and exit with error.
    # The supervisor (systemd Restart=on-failure / launchd KeepAlive) will
    # restart amp-distro serve cleanly -- no risk of double-restart.
    if os.environ.get("INVOCATION_ID") or os.environ.get("LAUNCHD_JOB_NAME"):
        logger.info(
            "Running under service manager — stopping server and exiting "
            "to trigger supervised restart"
        )
        stop_process(server_pid)
        sys.exit(1)

    # Standalone path: stop the server and restart directly.
    if is_running(server_pid):
        logger.info("Stopping existing server...")
        stop_process(server_pid)
        # Brief pause for port release
        time.sleep(2)

    try:
        pid = daemonize(host=host, port=port, apps_dir=apps_dir, dev=dev)
        logger.info("Server restarted (PID %d)", pid)
    except RuntimeError as e:
        logger.warning(
            "Port still busy after server stop — will retry next cycle: %s", e
        )
        return
```

**Step 3: Verify `os` is imported**

The file already imports `os` at line 14. No change needed.

**Step 4: Run the supervisor detection tests**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_watchdog.py::TestRestartServerSupervisorDetection -v
```
Expected: **4 PASSED**

**Step 5: Run ALL watchdog tests to check for regressions**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/test_watchdog.py -v
```
Expected: **All PASSED**

**Step 6: Commit**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands
git add distro-server/src/amplifier_distro/server/watchdog.py distro-server/tests/test_watchdog.py
git commit -m "fix: add supervisor detection and RuntimeError handling to _restart_server()"
```

---

## Group 5 — Static template + final verification

### Task 12: Update the static service template

**Files:**
- Modify: `distro-server/scripts/amplifier-distro.service`

**Step 1: Read the current file**

Open `distro-server/scripts/amplifier-distro.service`. Line 7 currently reads:
```
ExecStart=amp-distro-server --host 127.0.0.1 --port 8400
```

**Step 2: Update ExecStart**

Change line 7 to:
```
ExecStart=amp-distro serve --host 127.0.0.1 --port 8400
```

The full file after the change:
```ini
[Unit]
Description=Amplifier Distro Server
After=network.target

[Service]
Type=simple
ExecStart=amp-distro serve --host 127.0.0.1 --port 8400
Restart=on-failure
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=60
WorkingDirectory=%h
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
```

Note: This static file already has `Restart=on-failure` (unlike the programmatically generated unit which had `Restart=always`). Only the `ExecStart` line changes.

---

### Task 13: Run the full test suite — everything must pass

**Step 1: Run all tests**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
uv run pytest tests/ -v 2>&1 | tail -50
```

Expected: All tests pass. If anything fails:

1. Read the failure message carefully
2. Check which file and function is failing
3. Fix the minimal issue — do NOT add new code that wasn't part of the design

**Common fixes you might need:**

- `AttributeError: module has no attribute '_find_distro_binary'` — did you rename but miss a call site?
- `AttributeError: module has no attribute '_find_python_binary'` — is there still a call to the removed function somewhere?
- Any test in `TestInstallSystemd` failing with "amp-distro-server not found" — you may have missed updating `_install_systemd()` or `_install_launchd()` to call `_find_distro_binary()`.
- `test_watchdog_delegates_to_run_watchdog_loop` failing with wrong args — check the `watchdog_cmd` in cli.py passes `host=` and `port=` as keyword args.

---

### Task 14: Final commit

**Step 1: Commit the static template change**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands
git add distro-server/scripts/amplifier-distro.service
git commit -m "fix: update static service template to use amp-distro serve"
```

**Step 2: Verify git log looks right**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands
git log --oneline -6
```

Expected output (newest first):
```
<hash> fix: update static service template to use amp-distro serve
<hash> fix: add supervisor detection and RuntimeError handling to _restart_server()
<hash> feat: add hidden watchdog subcommand to amp-distro CLI
<hash> fix: update service.py to use amp-distro binary instead of amp-distro-server
<hash> chore: remove amp-distro-server entry point, bump version to 0.3.0
<hash> docs: add design for fixing amp-distro service commands routing  ← pre-existing
```

**Step 3: Final sanity check**

Verify there are no remaining references to `amp-distro-server` in the source (test fixtures and design docs are OK):
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-service-commands/distro-server
grep -r "amp-distro-server" src/ scripts/
```
Expected: **No output** (zero matches).

---

## Summary of all changes

| Group | Files | Key change |
|---|---|---|
| 1 | `pyproject.toml` | Remove `amp-distro-server` script, version → `0.3.0` |
| 2 | `service.py`, `tests/test_service.py` | `_find_distro_binary()`, ExecStart strings, `Restart=on-failure`, stale detection |
| 3 | `cli.py`, `tests/test_cli.py` | Hidden `watchdog` subcommand |
| 4 | `watchdog.py`, `tests/test_watchdog.py` | Supervisor detection + `RuntimeError` catch in `_restart_server()` |
| 5 | `scripts/amplifier-distro.service` | Update static template |

**Total new/modified test assertions:** ~20 updated + ~14 new
**Total new production code lines:** ~50
