# fix/await-cancel Implementation Plan

> **For execution:** Use `/execute-plan` mode or the subagent-driven-development recipe.

**Goal:** Fix two async correctness bugs in `session_backend.py` — missing `await` on `request_cancel` and missing CWD safety guard in `_reconnect`.

**Architecture:** Two targeted fixes inside `_SessionHandle.cancel()` and `FoundationBackend._reconnect()`. No new files, no new classes, no API changes. One new `import os` line added at the top of `session_backend.py` for the CWD guard.

**Tech Stack:** Python 3.11+, asyncio, pytest-asyncio (`asyncio_mode = "auto"`), uv

---

## Before You Touch Anything

### Orientation (read once, reference always)

```
Repo root:   /Users/samule/repo/amplifier-distro-msft
Worktree:    .worktrees/fix-await-cancel
Work from:   /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-await-cancel
```

**Switch to the worktree first:**

```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-await-cancel
```

All commands in this plan assume this working directory unless stated otherwise.

**The two files you will touch:**

| File | What changes |
|------|-------------|
| `distro-server/tests/test_session_backend.py` | Add 2 new test methods to existing test classes |
| `distro-server/src/amplifier_distro/server/session_backend.py` | Add `import os`, fix `cancel()`, fix `_reconnect()` |

**Test command (run from the worktree root):**

```bash
cd distro-server && uv run pytest tests/test_session_backend.py -v
```

**Full suite baseline (945 tests, all green — run once to confirm before starting):**

```bash
cd distro-server && uv run pytest tests/ -v
```

### Critical test-file conventions — read before writing any code

Copying these wrong is the #1 way to waste time:

1. **No `@pytest.mark.asyncio`** — `pyproject.toml` sets `asyncio_mode = "auto"`. Every `async def test_*` runs automatically. Do not add the decorator.

2. **Product-code imports are deferred inside each test method**, not at module level. Pattern:
   ```python
   async def test_something(self):
       from amplifier_distro.server.session_backend import _SessionHandle
       # ... test body
   ```

3. **Module-level imports** (already at the top of the test file, do not re-add):
   ```python
   import asyncio
   import sys
   import unittest.mock
   from pathlib import Path
   from unittest.mock import AsyncMock, MagicMock, patch
   import pytest
   ```

4. **Add new tests to existing classes.** Do not create new classes. The two classes that receive new tests:
   - `TestSessionHandleCancel` (around line 557) — receives Bug 1 test
   - `TestFoundationBackendReconnect` (around line 471) — receives Bug 2 test

5. **`bridge_backend` fixture** is defined in the same file (around line 28). Tests that use it receive it as a parameter.

6. **Async tests in this file use `self` and the `bridge_backend` fixture** when they need one, e.g.:
   ```python
   async def test_foo(self, bridge_backend):
   ```
   Tests that build their own objects (like `TestSessionHandleCancel`) do not use `bridge_backend`.

---

## Bug 1 — `request_cancel` Not Awaited

**Root cause:** `_SessionHandle.cancel()` line 89 calls `request_cancel(level)` without `await`. When `request_cancel` is a coroutine function (as `ModuleCoordinator.request_cancel` is in production), this creates a coroutine object and immediately discards it, producing `RuntimeWarning: coroutine '...' was never awaited` on every server shutdown.

**Fix:** Wrap the call in an `asyncio.iscoroutinefunction` guard so async implementations are awaited and sync implementations are called normally.

---

### Task 1: Write the failing test for Bug 1

**File to edit:** `distro-server/tests/test_session_backend.py`

**Where to add it:** Inside the `TestSessionHandleCancel` class, after the last existing method (`test_cancel_no_coordinator_does_not_raise`, which ends around line 596). Add the new method at the end of that class.

**Exact code to add** (paste this as the new last method of `TestSessionHandleCancel`):

```python
    async def test_cancel_awaits_coroutine_request_cancel(self):
        """cancel() must await request_cancel when it is a coroutine function.

        The coordinator's request_cancel is async in production. The old code
        called request_cancel(level) without await, silently discarding the
        coroutine. This test uses AsyncMock to prove the coroutine is awaited.
        """
        from amplifier_distro.server.session_backend import _SessionHandle

        mock_session = MagicMock()
        mock_session.coordinator = MagicMock()
        mock_session.coordinator.request_cancel = AsyncMock()  # async — must be awaited

        handle = _SessionHandle(
            session_id="s-await-001",
            project_id="p-await-001",
            working_dir=Path("/tmp"),
            session=mock_session,
        )
        await handle.cancel("graceful")

        # assert_awaited_once_with checks .await_count, not just .call_count.
        # With the bug: call_count=1, await_count=0 → this assertion FAILS.
        # With the fix: call_count=1, await_count=1 → PASSES.
        mock_session.coordinator.request_cancel.assert_awaited_once_with("graceful")
```

**Indentation note:** Class methods in this file use 4-space indentation. The `async def test_...` line is indented 4 spaces (one level inside the class).

---

### Task 2: Verify Task 1's test fails

Run only the new test:

```bash
cd distro-server && uv run pytest tests/test_session_backend.py::TestSessionHandleCancel::test_cancel_awaits_coroutine_request_cancel -v
```

**Expected output (FAIL):**

```
FAILED tests/test_session_backend.py::TestSessionHandleCancel::test_cancel_awaits_coroutine_request_cancel
AssertionError: expected await not found.
Expected: mock('graceful')
Actual: not awaited.
```

The exact wording may vary slightly, but the test **must be FAILED** before you proceed. If it passes, the test is wrong — stop and re-read Task 1.

---

### Task 3: Implement the await fix in `_SessionHandle.cancel()`

**File to edit:** `distro-server/src/amplifier_distro/server/session_backend.py`

**Find this exact block** (lines 86–93 in the current file):

```python
        request_cancel = getattr(coordinator, "request_cancel", None)
        if request_cancel is not None:
            try:
                request_cancel(level)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Error requesting cancel (level=%s)", level, exc_info=True
                )
```

**Replace it with this** (the only change is adding the `iscoroutinefunction` check around `request_cancel(level)`):

```python
        request_cancel = getattr(coordinator, "request_cancel", None)
        if request_cancel is not None:
            try:
                if asyncio.iscoroutinefunction(request_cancel):
                    await request_cancel(level)
                else:
                    request_cancel(level)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Error requesting cancel (level=%s)", level, exc_info=True
                )
```

**What changed:** Two lines replaced `request_cancel(level)` with an `if/else` on `asyncio.iscoroutinefunction`. The `asyncio` import already exists at the top of the file (line 14) — **do not add it again**.

**Nothing else changes.** Do not touch the method signature, the `if self.session is None` guard, or the `coordinator` guard above this block.

---

### Task 4: Verify the Bug 1 test now passes

Run all three `TestSessionHandleCancel` tests together (new test + the two existing ones):

```bash
cd distro-server && uv run pytest tests/test_session_backend.py::TestSessionHandleCancel -v
```

**Expected output (all 3 PASS):**

```
PASSED tests/test_session_backend.py::TestSessionHandleCancel::test_cancel_calls_coordinator_request_cancel
PASSED tests/test_session_backend.py::TestSessionHandleCancel::test_cancel_no_session_does_not_raise
PASSED tests/test_session_backend.py::TestSessionHandleCancel::test_cancel_no_coordinator_does_not_raise
PASSED tests/test_session_backend.py::TestSessionHandleCancel::test_cancel_awaits_coroutine_request_cancel
```

**All four must be green.** If `test_cancel_calls_coordinator_request_cancel` (the sync-mock test) is now failing, your edit broke the sync code path — the `iscoroutinefunction` guard for a plain `MagicMock()` should return `False` and fall through to `else: request_cancel(level)`.

---

### Task 5: Commit Bug 1 fix

```bash
cd distro-server && uv run pytest tests/ -v --tb=no -q
```

Confirm the count is still 945+ passed, 0 failed. Then commit:

```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-await-cancel
git add distro-server/src/amplifier_distro/server/session_backend.py \
        distro-server/tests/test_session_backend.py
git commit -m "fix: await request_cancel coroutine in _SessionHandle.cancel()

request_cancel is async on ModuleCoordinator. Calling it without await
silently discarded the coroutine, producing RuntimeWarning on every
server shutdown.

Add asyncio.iscoroutinefunction guard so async implementations are
awaited and sync implementations (e.g. tests, mocks) are called
normally. Covers both paths with a new AsyncMock-based test."
```

---

## Bug 2 — FileNotFoundError on Reconnect When CWD Deleted

**Root cause:** When a server process's working directory is deleted while it's running (common in temp-dir or container environments), `BundleRegistry.__init__` calls `Path.cwd()` → `os.getcwd()` which raises `FileNotFoundError`. This propagates up through `_reconnect()` which catches it as a generic `Exception` and re-raises as `ValueError("Unknown session: ...")`, making every reconnect attempt fail until the server restarts.

**Fix:** Add a CWD safety guard in `_reconnect()` immediately before the `_load_bundle()` call. If `os.getcwd()` raises `FileNotFoundError`, silently `chdir` to home and proceed.

---

### Task 6: Write the failing test for Bug 2

**File to edit:** `distro-server/tests/test_session_backend.py`

**Where to add it:** Inside the `TestFoundationBackendReconnect` class (starts around line 471), after the last existing method (`test_find_transcript_reads_jsonl`, which ends around line 551). Add the new method at the end of that class.

**Exact code to add:**

```python
    async def test_reconnect_chdir_home_if_cwd_deleted(self, bridge_backend):
        """_reconnect() must chdir to ~ and continue if os.getcwd() raises.

        When the server process's CWD has been deleted, BundleRegistry calls
        os.getcwd() and raises FileNotFoundError. The fix adds a guard before
        _load_bundle() that catches this and chdirs to home.
        """
        import os
        import sys

        mock_session = MagicMock()
        mock_session.session_id = "sess-cwd-001"
        mock_session.coordinator = MagicMock()
        mock_context = MagicMock()
        mock_context.get_messages = AsyncMock(return_value=[])
        mock_context.set_messages = AsyncMock()
        mock_session.coordinator.get = MagicMock(return_value=mock_context)

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        bridge_backend._load_bundle = AsyncMock(return_value=mock_prepared)
        bridge_backend._find_transcript = MagicMock(
            return_value=[{"role": "user", "content": "hello"}]
        )

        # Mock amplifier_foundation.session so the test works without a real install
        mock_af_session = MagicMock()
        mock_af_session.find_orphaned_tool_calls.return_value = []

        home_dir = os.path.expanduser("~")

        with (
            patch.dict(sys.modules, {"amplifier_foundation.session": mock_af_session}),
            patch("os.getcwd", side_effect=FileNotFoundError("No such file or directory")),
            patch("os.chdir") as mock_chdir,
        ):
            from amplifier_distro.server.session_backend import FoundationBackend

            handle = await FoundationBackend._reconnect(
                bridge_backend, "sess-cwd-001"
            )

        # Guard must have called chdir(home) to recover
        mock_chdir.assert_called_once_with(home_dir)
        # Reconnect must have succeeded and returned a valid handle
        assert handle.session_id == "sess-cwd-001"

        # Cleanup background worker task
        if "sess-cwd-001" in bridge_backend._worker_tasks:
            bridge_backend._worker_tasks["sess-cwd-001"].cancel()
```

**Important notes about this test:**

- `import os` and `import sys` are **local to the test method** (deferred import pattern used in this file).
- `patch("os.getcwd", ...)` patches `os.getcwd` globally for the duration of the `with` block. The test is designed so only the CWD guard calls `os.getcwd()` — `_load_bundle` is mocked and the `Path(...).expanduser()` calls used in the reconnect body do not call `os.getcwd()`.
- `patch("os.chdir")` intercepts the actual `os.chdir` call without changing the process's real working directory.
- The `with (...)` multi-context-manager syntax requires Python 3.10+ — the project targets Python 3.11+ so this is fine.

---

### Task 7: Verify Task 6's test fails

```bash
cd distro-server && uv run pytest tests/test_session_backend.py::TestFoundationBackendReconnect::test_reconnect_chdir_home_if_cwd_deleted -v
```

**Expected output (FAIL):**

```
FAILED tests/test_session_backend.py::TestFoundationBackendReconnect::test_reconnect_chdir_home_if_cwd_deleted
AssertionError: Expected call: chdir('<your home dir>')
Not called
```

The assertion `mock_chdir.assert_called_once_with(home_dir)` fails because the current code never calls `os.chdir`. The test **must be FAILED** before you proceed. If it passes, the guard already exists or the test is wrong — stop and investigate.

---

### Task 8: Implement the CWD guard in `_reconnect()`

This task has two sub-steps: add the `import os` line, then add the guard.

#### Sub-step 8a: Add `import os` to `session_backend.py`

**File:** `distro-server/src/amplifier_distro/server/session_backend.py`

**Find this block** at the top of the file (lines 14–19):

```python
import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
```

**Replace it with** (adding `import os` in alphabetical order between `json` and `logging`):

```python
import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
```

**Only one line is added.** Alphabetical order within the stdlib block is a project convention — `os` goes between `logging` and `from dataclasses`.

#### Sub-step 8b: Add the CWD guard in `_reconnect()`

**In the same file**, find this exact block inside `_reconnect()` (around lines 704–706):

```python
            # 3. Create a fresh session with the same bundle
            wd = Path(working_dir).expanduser()
            prepared = await self._load_bundle()
```

**Replace it with:**

```python
            # 3. Create a fresh session with the same bundle
            wd = Path(working_dir).expanduser()
            # CWD safety guard: BundleRegistry.__init__ calls os.getcwd() which
            # raises FileNotFoundError if the process CWD was deleted (e.g. in
            # container or temp-dir environments). Silently recover by moving to ~.
            try:
                os.getcwd()
            except FileNotFoundError:
                os.chdir(os.path.expanduser("~"))
            prepared = await self._load_bundle()
```

**What changed:** Four lines added between `wd = ...` and `prepared = ...`. Nothing else changes. The guard is intentionally minimal — it only moves the process home when necessary and then lets the rest of `_reconnect()` proceed normally.

---

### Task 9: Verify the Bug 2 test now passes

Run all tests in `TestFoundationBackendReconnect`:

```bash
cd distro-server && uv run pytest tests/test_session_backend.py::TestFoundationBackendReconnect -v
```

**Expected output (all PASS):**

```
PASSED tests/test_session_backend.py::TestFoundationBackendReconnect::test_reconnect_raises_for_ended_session
PASSED tests/test_session_backend.py::TestFoundationBackendReconnect::test_reconnect_raises_when_no_transcript
PASSED tests/test_session_backend.py::TestFoundationBackendReconnect::test_resume_session_delegates_to_reconnect
PASSED tests/test_session_backend.py::TestFoundationBackendReconnect::test_resume_session_skips_if_already_cached
PASSED tests/test_session_backend.py::TestFoundationBackendReconnect::test_find_transcript_reads_jsonl
PASSED tests/test_session_backend.py::TestFoundationBackendReconnect::test_reconnect_chdir_home_if_cwd_deleted
```

**All six must be green.** If any pre-existing test in this class now fails, your edit to `_reconnect()` broke something — check that you edited the correct block and did not accidentally modify surrounding code.

---

### Task 10: Final commit

Run the full suite one last time to confirm baseline:

```bash
cd distro-server && uv run pytest tests/ -v --tb=no -q
```

**Expected:** 947 passed (945 original + 2 new), 0 failed.

Then commit from the worktree root:

```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-await-cancel
git add distro-server/src/amplifier_distro/server/session_backend.py \
        distro-server/tests/test_session_backend.py
git commit -m "fix: add CWD safety guard in _reconnect() before _load_bundle()

BundleRegistry.__init__ calls os.getcwd() which raises FileNotFoundError
when the server process's working directory has been deleted (common in
container and temp-dir environments). This caused _reconnect() to fail
with ValueError('Unknown session: ...'), making every reconnect attempt
silently fail until server restart.

Add a try/except guard before _load_bundle() that catches
FileNotFoundError from os.getcwd() and chdirs to ~ before proceeding.
Covered by a new test that patches os.getcwd to raise and verifies
os.chdir is called with the home directory."
```

---

## Appendix A: What Each Test Proves (and Why It Fails Before the Fix)

### Bug 1 test: `test_cancel_awaits_coroutine_request_cancel`

**Why it fails before the fix:**

`unittest.mock.AsyncMock` records two separate counters: `.call_count` (how many times the mock was called) and `.await_count` (how many times the returned coroutine was awaited). The current code does `request_cancel(level)` which increments `call_count` to 1 but leaves `await_count` at 0. The assertion `assert_awaited_once_with("graceful")` checks `await_count == 1` → fails.

**Why it passes after the fix:**

`asyncio.iscoroutinefunction(AsyncMock())` returns `True`. The code takes the `if` branch and does `await request_cancel(level)`, which increments both `call_count` and `await_count` to 1. The assertion passes.

**Why the existing sync test still passes after the fix:**

`asyncio.iscoroutinefunction(MagicMock())` returns `False`. The code takes the `else` branch and does `request_cancel(level)` (sync call). The existing test asserts `.assert_called_once_with("graceful")` (call count, not await count) — still passes.

### Bug 2 test: `test_reconnect_chdir_home_if_cwd_deleted`

**Why it fails before the fix:**

The current `_reconnect()` never calls `os.getcwd()` or `os.chdir()`. Patching `os.getcwd` to raise doesn't affect anything because nobody calls it. `mock_chdir.assert_called_once_with(home_dir)` fails because `os.chdir` was never invoked.

**Why it passes after the fix:**

The guard `try: os.getcwd() except FileNotFoundError: os.chdir(...)` is hit before `_load_bundle()`. `os.getcwd()` raises (patched). `os.chdir(os.path.expanduser("~"))` is called. `_load_bundle()` proceeds (mocked to succeed). `mock_chdir.assert_called_once_with(home_dir)` passes.

---

## Appendix B: Common Mistakes and How to Avoid Them

| Mistake | Symptom | Fix |
|---------|---------|-----|
| Added `@pytest.mark.asyncio` to a test | `PytestUnraisableExceptionWarning` or double-run | Remove the decorator — `asyncio_mode = "auto"` handles it |
| Imported `_SessionHandle` at module level | `ImportError` on module load in CI | Move the import inside the test method |
| Used `assert_called_once_with` instead of `assert_awaited_once_with` for Bug 1 test | Test passes even without the fix | Use `assert_awaited_once_with` — it checks the await counter |
| Added `import os` inside `_reconnect()` instead of at the top of the file | Works but violates project style (ruff will flag it) | Put `import os` in the stdlib block at the top of the file |
| Edited the wrong `wd = ...` line (there's one in `create_session` too) | Wrong method gets the guard | Confirm you're editing the line inside `_reconnect()`, which is a method of `FoundationBackend`, not `_SessionHandle` |
| Forgot to cancel the worker task in the Bug 2 test | Stray task warning from `_cancel_stray_tasks` autouse fixture | The `if "sess-cwd-001" in bridge_backend._worker_tasks: cancel()` block at the end handles this |
