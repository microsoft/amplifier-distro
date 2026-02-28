# fix/bundle-prewarm Implementation Plan

> **For execution:** Use `/execute-plan` mode or the subagent-driven-development recipe.

**Goal:** Eliminate "Session creation failed" by pre-warming the bundle at server startup and supporting live reload when the overlay changes.

**Architecture:** Add `startup()`/`reload_bundle()` to `FoundationBackend` with a `_prepared_bundle` cache field, wire to FastAPI startup via `start_services()`, notify active sessions via `on_bundle_reload` callback, and trigger reload from `overlay.py` after every overlay write.

**Tech Stack:** Python 3.11+, asyncio, FastAPI, pytest, uv

**Depends on:** `fix/approval-display` (for `SessionSurface.on_bundle_reload` — this plan uses `Any` where `SessionSurface` is referenced so it merges cleanly before that branch lands)

**Baseline:** 945 tests passing before any changes.

---

## File Map

| File | What Changes |
|------|-------------|
| `distro-server/src/amplifier_distro/server/session_backend.py` | Add `_prepared_bundle`/`_bundle_version` fields, cache check in `_load_bundle()`, `startup()`, `reload_bundle()`, `_compute_bundle_version()`, new fields on `_SessionHandle`, populate `bundle_version` in `create_session()` |
| `distro-server/src/amplifier_distro/server/services.py` | Add `start_services()` function |
| `distro-server/src/amplifier_distro/server/app.py` | Wire `start_services` to FastAPI startup event |
| `distro-server/src/amplifier_distro/overlay.py` | Call `backend.reload_bundle()` after `_write_overlay()` |
| `distro-server/tests/test_session_backend.py` | New test classes for cache, startup, reload, version, handle fields |
| `distro-server/tests/test_services.py` | New test class for `start_services()` |
| `distro-server/tests/test_overlay_reload.py` | New file: test overlay write triggers reload |

---

## Critical Setup Notes

**Worktree:** All work happens in `.worktrees/fix-bundle-prewarm`. Every command in this plan assumes you are in that worktree.

**Test command (from the distro-server directory):**
```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-bundle-prewarm/distro-server
uv run pytest tests/ -v
```

**The `bridge_backend` fixture** in `test_session_backend.py` bypasses `FoundationBackend.__init__` and manually sets all instance attributes. **Whenever you add a new field to `__init__`, you must also add it to the fixture.** If you forget, tests that use `bridge_backend` will blow up with `AttributeError` on the new field. The fixture lives at the top of `test_session_backend.py` starting at line 28.

---

## Task 1: Write Failing Test — `_load_bundle()` Returns Cache When `_prepared_bundle` Is Set

**File:** `distro-server/tests/test_session_backend.py`

**Step 1: Append this new test class to the end of `test_session_backend.py`**

```python
# ── Bundle Pre-warm Cache ──────────────────────────────────────────────────


class TestFoundationBackendBundleCache:
    """_load_bundle() returns _prepared_bundle immediately when the cache is set."""

    async def test_load_bundle_returns_cached_bundle_without_hitting_foundation(
        self, bridge_backend
    ):
        """If _prepared_bundle is set, _load_bundle() returns it without
        importing or calling amplifier_foundation.load_bundle.

        This is the core cache contract: once pre-warmed, no network/disk
        I/O happens on subsequent calls.
        """
        mock_bundle = MagicMock()
        bridge_backend._prepared_bundle = mock_bundle

        from amplifier_distro.server.session_backend import FoundationBackend

        result = await FoundationBackend._load_bundle(bridge_backend)

        assert result is mock_bundle

    async def test_load_bundle_cache_miss_when_prepared_bundle_is_none(
        self, bridge_backend
    ):
        """When _prepared_bundle is None, _load_bundle() falls through to
        the real load path (which will fail without foundation — that's fine,
        we just need AttributeError/ImportError, not the cached value).
        """
        bridge_backend._prepared_bundle = None

        from amplifier_distro.server.session_backend import FoundationBackend

        # Should NOT return None — it must attempt a real load and fail
        # (ImportError or similar) because foundation isn't mocked here.
        with pytest.raises(Exception):  # noqa: B017
            await FoundationBackend._load_bundle(bridge_backend)
```

> **Why two tests?** The first is the golden path (cache hit returns the mock). The second guards against an implementation bug where `_load_bundle` always returns `None` or the cached value even when the cache is explicitly `None`.

**Step 2: Run the tests to confirm they FAIL**

```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-bundle-prewarm/distro-server
uv run pytest tests/test_session_backend.py::TestFoundationBackendBundleCache -v
```

Expected output:
```
FAILED tests/test_session_backend.py::TestFoundationBackendBundleCache::test_load_bundle_returns_cached_bundle_without_hitting_foundation
  AttributeError: 'FoundationBackend' object has no attribute '_prepared_bundle'
FAILED tests/test_session_backend.py::TestFoundationBackendBundleCache::test_load_bundle_cache_miss_when_prepared_bundle_is_none
  AttributeError: 'FoundationBackend' object has no attribute '_prepared_bundle'
```

Both fail with `AttributeError` because neither the field nor the cache check exist yet. This confirms the tests are meaningful.

---

## Task 2: Implement `_prepared_bundle` Cache — Update Fixture, `__init__`, and `_load_bundle()`; Verify Pass; Commit

**Files:**
- Modify: `distro-server/src/amplifier_distro/server/session_backend.py`
- Modify: `distro-server/tests/test_session_backend.py` (the `bridge_backend` fixture)

### Step 1: Add fields to `FoundationBackend.__init__`

Find the `__init__` method (line 308). After the last existing attribute assignment (`self._wired_sessions: set[str] = set()`), add two new lines:

```python
    def __init__(self, bundle_name: str = AMPLIFIER_START_URI) -> None:
        self._bundle_name = bundle_name
        self._sessions: dict[str, _SessionHandle] = {}
        self._reconnect_locks: dict[str, asyncio.Lock] = {}
        # Per-session FIFO queues for serializing handle.run() calls
        self._session_queues: dict[str, asyncio.Queue] = {}
        # Worker tasks draining each session queue
        self._worker_tasks: dict[str, asyncio.Task] = {}
        # Tombstone: sessions that were intentionally ended (blocks reconnect)
        self._ended_sessions: set[str] = set()
        self._approval_systems: dict[str, Any] = {}
        # Guard: sessions whose hooks have already been wired (prevents
        # double-registration on page refresh / resume)
        self._wired_sessions: set[str] = set()
        # Pre-warmed bundle cache: set by startup() so create_session() is instant
        self._prepared_bundle: Any | None = None
        # Version string (overlay bundle.yaml mtime) for staleness detection
        self._bundle_version: str = ""
```

### Step 2: Update `_load_bundle()` to check the cache first

Find `_load_bundle()` (line 323). Add the cache check at the very top of the method body, before the `from amplifier_foundation import load_bundle` line:

```python
    async def _load_bundle(self, bundle_name: str | None = None) -> Any:
        """Load and prepare a bundle via foundation.

        If a local overlay bundle exists (created by the install wizard),
        loads it by path.  The overlay includes the maintained distro bundle and any
        user-selected features, so everything composes automatically.
        Falls back to loading the bundle by name if no overlay exists.

        If _prepared_bundle is already set (pre-warmed by startup()), returns
        the cached value immediately without any I/O.
        """
        if self._prepared_bundle is not None:
            return self._prepared_bundle

        from amplifier_foundation import load_bundle

        from amplifier_distro.overlay import overlay_dir, overlay_exists

        if overlay_exists():
            bundle = await load_bundle(str(overlay_dir()))
        else:
            name = bundle_name or self._bundle_name
            bundle = await load_bundle(name)
        return await bundle.prepare()
```

### Step 3: Update the `bridge_backend` fixture in `test_session_backend.py`

Find the `bridge_backend` fixture (line 28). Add the two new fields **after** `backend._wired_sessions = set()`:

```python
@pytest.fixture
def bridge_backend():
    """FoundationBackend with mocked LocalBridge."""
    target = "amplifier_distro.server.session_backend.FoundationBackend.__init__"
    with patch(target) as mock_init:
        mock_init.return_value = None  # suppress real __init__

        from amplifier_distro.server.session_backend import FoundationBackend

        backend = FoundationBackend.__new__(FoundationBackend)
        backend._bundle_name = "test-bundle"
        backend._sessions = {}
        backend._reconnect_locks = {}
        backend._session_queues = {}
        backend._worker_tasks = {}
        backend._ended_sessions = set()
        backend._approval_systems = {}
        backend._wired_sessions = set()
        # Pre-warm cache fields (new in fix/bundle-prewarm)
        backend._prepared_bundle = None
        backend._bundle_version = ""
        return backend
```

### Step 4: Run the tests to confirm they PASS

```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-bundle-prewarm/distro-server
uv run pytest tests/test_session_backend.py::TestFoundationBackendBundleCache -v
```

Expected output:
```
PASSED tests/test_session_backend.py::TestFoundationBackendBundleCache::test_load_bundle_returns_cached_bundle_without_hitting_foundation
PASSED tests/test_session_backend.py::TestFoundationBackendBundleCache::test_load_bundle_cache_miss_when_prepared_bundle_is_none
```

### Step 5: Run the full suite to confirm no regressions

```bash
uv run pytest tests/ -v
```

Expected: all 945+ tests pass.

### Step 6: Commit

```bash
git add distro-server/src/amplifier_distro/server/session_backend.py \
        distro-server/tests/test_session_backend.py
git commit -m "feat: add _prepared_bundle cache field and cache check to _load_bundle()"
```

---

## Task 3: Write Failing Test — `startup()` Pre-Warms the Bundle

**File:** `distro-server/tests/test_session_backend.py`

**Step 1: Append this new test class after `TestFoundationBackendBundleCache`**

```python
# ── Bundle startup() ──────────────────────────────────────────────────────


class TestFoundationBackendStartup:
    """startup() pre-warms the bundle so first create_session() is instant."""

    async def test_startup_sets_prepared_bundle(self, bridge_backend):
        """After startup(), _prepared_bundle is non-None."""
        mock_bundle = MagicMock()
        bridge_backend._load_bundle = AsyncMock(return_value=mock_bundle)
        bridge_backend._compute_bundle_version = MagicMock(return_value="")

        from amplifier_distro.server.session_backend import FoundationBackend

        await FoundationBackend.startup(bridge_backend)

        assert bridge_backend._prepared_bundle is mock_bundle

    async def test_startup_calls_load_bundle_once(self, bridge_backend):
        """startup() calls _load_bundle() exactly once (no bundle_name arg)."""
        mock_bundle = MagicMock()
        bridge_backend._load_bundle = AsyncMock(return_value=mock_bundle)
        bridge_backend._compute_bundle_version = MagicMock(return_value="")

        from amplifier_distro.server.session_backend import FoundationBackend

        await FoundationBackend.startup(bridge_backend)

        bridge_backend._load_bundle.assert_awaited_once_with()

    async def test_startup_sets_bundle_version(self, bridge_backend):
        """startup() also sets _bundle_version from _compute_bundle_version()."""
        mock_bundle = MagicMock()
        bridge_backend._load_bundle = AsyncMock(return_value=mock_bundle)
        bridge_backend._compute_bundle_version = MagicMock(return_value="1234567.89")

        from amplifier_distro.server.session_backend import FoundationBackend

        await FoundationBackend.startup(bridge_backend)

        assert bridge_backend._bundle_version == "1234567.89"
```

**Step 2: Run to confirm FAIL**

```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-bundle-prewarm/distro-server
uv run pytest tests/test_session_backend.py::TestFoundationBackendStartup -v
```

Expected:
```
FAILED ... AttributeError: type object 'FoundationBackend' has no attribute 'startup'
```

All three tests fail because `startup()` doesn't exist yet.

---

## Task 4: Implement `startup()`; Verify Pass; Commit

**File:** `distro-server/src/amplifier_distro/server/session_backend.py`

### Step 1: Add `startup()` to `FoundationBackend`

Add this method immediately after `_load_bundle()` (after line ~340, before `_wire_event_queue`):

```python
    async def startup(self) -> None:
        """Pre-warm the bundle at server startup so first create_session() is instant.

        Called by FastAPI's startup event handler (wired in app.py).
        Sets self._prepared_bundle so _load_bundle() returns the cached value
        on all subsequent calls.
        """
        logger.info("Pre-warming bundle...")
        self._prepared_bundle = await self._load_bundle()
        self._bundle_version = self._compute_bundle_version()
        logger.info("Bundle pre-warmed and ready")
```

> **Note:** `_load_bundle()` is called without arguments here. At startup time `_prepared_bundle` is `None` (freshly initialized), so the cache check is a no-op and the real load runs. `startup()` then stores the result, arming the cache for all subsequent calls.

> **Note:** `_compute_bundle_version()` is added in Task 9–10. For now, add a stub below `startup()` so the code doesn't raise `AttributeError`:
>
> ```python
>     def _compute_bundle_version(self) -> str:
>         """Return a version string based on overlay file mtime. Stub — see Task 9."""
>         return ""
> ```

### Step 2: Run the startup tests to confirm PASS

```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-bundle-prewarm/distro-server
uv run pytest tests/test_session_backend.py::TestFoundationBackendStartup -v
```

Expected:
```
PASSED ... test_startup_sets_prepared_bundle
PASSED ... test_startup_calls_load_bundle_once
PASSED ... test_startup_sets_bundle_version
```

### Step 3: Run the full suite to confirm no regressions

```bash
uv run pytest tests/ -v
```

Expected: all tests pass.

### Step 4: Commit

```bash
git add distro-server/src/amplifier_distro/server/session_backend.py
git commit -m "feat: add startup() to FoundationBackend for bundle pre-warming"
```

---

## Task 5: Write Failing Test — `reload_bundle()` Invalidates Cache and Reloads

**File:** `distro-server/tests/test_session_backend.py`

**Step 1: Append this new test class**

```python
# ── reload_bundle() ───────────────────────────────────────────────────────


class TestFoundationBackendReloadBundle:
    """reload_bundle() clears the cache and loads a fresh bundle."""

    async def test_reload_bundle_clears_and_reloads(self, bridge_backend):
        """reload_bundle() sets _prepared_bundle to None then reloads it."""
        old_bundle = MagicMock()
        new_bundle = MagicMock()

        bridge_backend._prepared_bundle = old_bundle

        load_calls = []

        async def fake_load_bundle(*args, **kwargs):
            load_calls.append(1)
            return new_bundle

        bridge_backend._load_bundle = fake_load_bundle
        bridge_backend._compute_bundle_version = MagicMock(return_value="new-version")

        from amplifier_distro.server.session_backend import FoundationBackend

        await FoundationBackend.reload_bundle(bridge_backend)

        # Cache must be the new bundle, not the old one
        assert bridge_backend._prepared_bundle is new_bundle
        assert bridge_backend._prepared_bundle is not old_bundle
        assert len(load_calls) == 1

    async def test_reload_bundle_updates_bundle_version(self, bridge_backend):
        """reload_bundle() calls _compute_bundle_version() and stores it."""
        new_bundle = MagicMock()
        bridge_backend._prepared_bundle = MagicMock()
        bridge_backend._load_bundle = AsyncMock(return_value=new_bundle)
        bridge_backend._compute_bundle_version = MagicMock(return_value="9999.0")

        from amplifier_distro.server.session_backend import FoundationBackend

        await FoundationBackend.reload_bundle(bridge_backend)

        assert bridge_backend._bundle_version == "9999.0"
```

**Step 2: Run to confirm FAIL**

```bash
uv run pytest tests/test_session_backend.py::TestFoundationBackendReloadBundle -v
```

Expected:
```
FAILED ... AttributeError: type object 'FoundationBackend' has no attribute 'reload_bundle'
```

---

## Task 6: Implement `reload_bundle()` (Cache Invalidation + Reload); Verify Pass; Commit

**File:** `distro-server/src/amplifier_distro/server/session_backend.py`

### Step 1: Add `reload_bundle()` immediately after `startup()`

```python
    async def reload_bundle(self) -> None:
        """Invalidate the bundle cache and reload from scratch.

        Called after overlay writes so a server restart isn't needed.
        After reloading, notifies all active sessions via their surface's
        on_bundle_reload callback (each surface defines its own restart policy).
        """
        logger.info("Reloading bundle...")
        self._prepared_bundle = None  # Invalidate cache so _load_bundle() does real I/O
        self._prepared_bundle = await self._load_bundle()
        self._bundle_version = self._compute_bundle_version()
        logger.info("Bundle reloaded")

        # Notify active session surfaces (surface notification loop added in Task 7-8)
```

> **Important:** Setting `self._prepared_bundle = None` before calling `self._load_bundle()` forces `_load_bundle()` past the cache check so it runs the real load. Then `reload_bundle()` stores the freshly-loaded bundle back into `self._prepared_bundle`.

### Step 2: Run the reload tests to confirm PASS

```bash
uv run pytest tests/test_session_backend.py::TestFoundationBackendReloadBundle -v
```

Expected:
```
PASSED ... test_reload_bundle_clears_and_reloads
PASSED ... test_reload_bundle_updates_bundle_version
```

### Step 3: Full suite

```bash
uv run pytest tests/ -v
```

### Step 4: Commit

```bash
git add distro-server/src/amplifier_distro/server/session_backend.py
git commit -m "feat: add reload_bundle() to FoundationBackend"
```

---

## Task 7: Write Failing Test — `reload_bundle()` Calls `on_bundle_reload` on Active Session Surfaces

**File:** `distro-server/tests/test_session_backend.py`

**Step 1: Append these tests to the existing `TestFoundationBackendReloadBundle` class** (add inside the class, after the last method)

```python
    async def test_reload_bundle_calls_on_bundle_reload_on_active_surfaces(
        self, bridge_backend
    ):
        """reload_bundle() calls on_bundle_reload() on every session surface."""
        new_bundle = MagicMock()
        bridge_backend._load_bundle = AsyncMock(return_value=new_bundle)
        bridge_backend._compute_bundle_version = MagicMock(return_value="")

        # Two handles with on_bundle_reload surfaces
        on_reload_a = AsyncMock()
        surface_a = MagicMock()
        surface_a.on_bundle_reload = on_reload_a

        handle_a = _make_mock_handle("sess-reload-a")
        handle_a.surface = surface_a

        on_reload_b = AsyncMock()
        surface_b = MagicMock()
        surface_b.on_bundle_reload = on_reload_b

        handle_b = _make_mock_handle("sess-reload-b")
        handle_b.surface = surface_b

        bridge_backend._sessions = {
            "sess-reload-a": handle_a,
            "sess-reload-b": handle_b,
        }

        from amplifier_distro.server.session_backend import FoundationBackend

        await FoundationBackend.reload_bundle(bridge_backend)

        on_reload_a.assert_awaited_once()
        on_reload_b.assert_awaited_once()

    async def test_reload_bundle_skips_sessions_without_surface(
        self, bridge_backend
    ):
        """reload_bundle() silently skips handles with no surface attribute."""
        new_bundle = MagicMock()
        bridge_backend._load_bundle = AsyncMock(return_value=new_bundle)
        bridge_backend._compute_bundle_version = MagicMock(return_value="")

        # Handle with no surface (legacy / headless session)
        handle = _make_mock_handle("sess-no-surface")
        # Explicitly set surface to None (no surface)
        handle.surface = None
        bridge_backend._sessions = {"sess-no-surface": handle}

        from amplifier_distro.server.session_backend import FoundationBackend

        # Must not raise
        await FoundationBackend.reload_bundle(bridge_backend)

    async def test_reload_bundle_skips_surfaces_with_no_on_bundle_reload(
        self, bridge_backend
    ):
        """reload_bundle() silently skips surfaces that have no on_bundle_reload."""
        new_bundle = MagicMock()
        bridge_backend._load_bundle = AsyncMock(return_value=new_bundle)
        bridge_backend._compute_bundle_version = MagicMock(return_value="")

        # Surface with on_bundle_reload = None
        surface = MagicMock()
        surface.on_bundle_reload = None
        handle = _make_mock_handle("sess-no-cb")
        handle.surface = surface
        bridge_backend._sessions = {"sess-no-cb": handle}

        from amplifier_distro.server.session_backend import FoundationBackend

        # Must not raise
        await FoundationBackend.reload_bundle(bridge_backend)

    async def test_reload_bundle_continues_past_surface_error(
        self, bridge_backend
    ):
        """If one surface's on_bundle_reload raises, reload continues to the next."""
        new_bundle = MagicMock()
        bridge_backend._load_bundle = AsyncMock(return_value=new_bundle)
        bridge_backend._compute_bundle_version = MagicMock(return_value="")

        # Surface A raises
        surface_a = MagicMock()
        surface_a.on_bundle_reload = AsyncMock(side_effect=RuntimeError("surface exploded"))
        handle_a = _make_mock_handle("sess-err-a")
        handle_a.surface = surface_a

        # Surface B should still be called
        on_reload_b = AsyncMock()
        surface_b = MagicMock()
        surface_b.on_bundle_reload = on_reload_b
        handle_b = _make_mock_handle("sess-err-b")
        handle_b.surface = surface_b

        bridge_backend._sessions = {
            "sess-err-a": handle_a,
            "sess-err-b": handle_b,
        }

        from amplifier_distro.server.session_backend import FoundationBackend

        # Must not raise (error is caught and logged as warning)
        await FoundationBackend.reload_bundle(bridge_backend)

        # Surface B must still have been called despite A's error
        on_reload_b.assert_awaited_once()
```

**Step 2: Run to confirm FAIL**

```bash
uv run pytest tests/test_session_backend.py::TestFoundationBackendReloadBundle -v
```

Expected: the new notification tests FAIL because `reload_bundle()` doesn't call surfaces yet. The two tests from Task 5 should still PASS.

---

## Task 8: Implement Surface Notification Loop in `reload_bundle()`; Verify Pass; Commit

**File:** `distro-server/src/amplifier_distro/server/session_backend.py`

### Step 1: Replace the stub in `reload_bundle()` with the full notification loop

Replace the entire `reload_bundle()` method body:

```python
    async def reload_bundle(self) -> None:
        """Invalidate the bundle cache and reload from scratch.

        Called after overlay writes so a server restart isn't needed.
        After reloading, notifies all active sessions via their surface's
        on_bundle_reload callback (each surface defines its own restart policy).
        """
        logger.info("Reloading bundle...")
        self._prepared_bundle = None  # Invalidate cache so _load_bundle() does real I/O
        self._prepared_bundle = await self._load_bundle()
        self._bundle_version = self._compute_bundle_version()
        logger.info("Bundle reloaded")

        # Notify all active sessions via their surface's on_bundle_reload callback.
        # Each surface defines its own restart policy (web chat, Slack, headless).
        # Use getattr(..., None) so this works before fix/approval-display lands
        # (handles that predate SessionSurface have no surface attribute).
        for session_id, handle in list(self._sessions.items()):
            surface = getattr(handle, "surface", None)
            if surface is None:
                continue
            on_reload = getattr(surface, "on_bundle_reload", None)
            if on_reload is None:
                continue
            try:
                await on_reload()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Error calling on_bundle_reload for session %s",
                    session_id,
                    exc_info=True,
                )
```

### Step 2: Run the full `TestFoundationBackendReloadBundle` class to confirm all PASS

```bash
uv run pytest tests/test_session_backend.py::TestFoundationBackendReloadBundle -v
```

Expected: 6 tests pass (2 from Task 5, 4 from Task 7).

### Step 3: Full suite

```bash
uv run pytest tests/ -v
```

### Step 4: Commit

```bash
git add distro-server/src/amplifier_distro/server/session_backend.py \
        distro-server/tests/test_session_backend.py
git commit -m "feat: reload_bundle() notifies active session surfaces via on_bundle_reload"
```

---

## Task 9: Write Failing Test — `_compute_bundle_version()` Returns Mtime String

**File:** `distro-server/tests/test_session_backend.py`

**Step 1: Append this new test class**

```python
# ── _compute_bundle_version() ─────────────────────────────────────────────


class TestFoundationBackendComputeBundleVersion:
    """_compute_bundle_version() returns a string based on overlay mtime."""

    def test_returns_empty_string_when_no_overlay(self, bridge_backend):
        """Returns '' when overlay doesn't exist (no bundle.yaml on disk)."""
        with patch("amplifier_distro.overlay.overlay_exists", return_value=False):
            from amplifier_distro.server.session_backend import FoundationBackend

            version = FoundationBackend._compute_bundle_version(bridge_backend)

        assert version == ""

    def test_returns_mtime_string_when_overlay_bundle_yaml_exists(
        self, bridge_backend, tmp_path
    ):
        """Returns str(bundle.yaml.stat().st_mtime) when overlay exists."""
        bundle_yaml = tmp_path / "bundle.yaml"
        bundle_yaml.write_text("bundle:\n  name: test\n  version: 0.1.0\n")

        with (
            patch("amplifier_distro.overlay.overlay_exists", return_value=True),
            patch("amplifier_distro.overlay.overlay_dir", return_value=tmp_path),
        ):
            from amplifier_distro.server.session_backend import FoundationBackend

            version = FoundationBackend._compute_bundle_version(bridge_backend)

        expected = str(bundle_yaml.stat().st_mtime)
        assert version == expected
        assert version != ""  # sanity: mtime is a real non-empty string

    def test_returns_empty_string_when_overlay_dir_exists_but_bundle_yaml_missing(
        self, bridge_backend, tmp_path
    ):
        """Returns '' when overlay is declared to exist but bundle.yaml is absent."""
        # tmp_path exists but no bundle.yaml inside it
        with (
            patch("amplifier_distro.overlay.overlay_exists", return_value=True),
            patch("amplifier_distro.overlay.overlay_dir", return_value=tmp_path),
        ):
            from amplifier_distro.server.session_backend import FoundationBackend

            version = FoundationBackend._compute_bundle_version(bridge_backend)

        assert version == ""
```

**Step 2: Run to confirm FAIL**

```bash
uv run pytest tests/test_session_backend.py::TestFoundationBackendComputeBundleVersion -v
```

Expected:
```
FAILED ... test_returns_empty_string_when_no_overlay — the stub always returns ""
  AssertionError (or test about mtime will fail because stub returns "")
```

Wait — the stub from Task 4 returns `""` always, so `test_returns_empty_string_when_no_overlay` would accidentally pass. That's fine — only the mtime test will fail, proving the stub is incomplete. Confirm:

```
PASSED ... test_returns_empty_string_when_no_overlay      ← accidentally passes (stub returns "")
FAILED ... test_returns_mtime_string_when_overlay_bundle_yaml_exists  ← fails (stub ignores mtime)
PASSED ... test_returns_empty_string_when_overlay_dir_exists_but_bundle_yaml_missing  ← accidentally passes
```

Two "accidentally passing" tests are OK here — the mtime test is the one that drives the real implementation.

---

## Task 10: Implement `_compute_bundle_version()`; Verify Pass; Commit

**File:** `distro-server/src/amplifier_distro/server/session_backend.py`

### Step 1: Replace the stub `_compute_bundle_version()` with the real implementation

Find the stub added in Task 4 and replace it entirely:

```python
    def _compute_bundle_version(self) -> str:
        """Return a version string based on overlay bundle.yaml mtime.

        Uses the modification time of bundle.yaml as a cheap staleness signal.
        Sessions store this at creation time so they can detect a bundle upgrade.
        Returns '' if no overlay exists or bundle.yaml is absent.
        """
        from amplifier_distro.overlay import overlay_dir, overlay_exists

        if not overlay_exists():
            return ""
        path = overlay_dir() / "bundle.yaml"
        if not path.exists():
            return ""
        return str(path.stat().st_mtime)
```

### Step 2: Run the bundle version tests to confirm all PASS

```bash
uv run pytest tests/test_session_backend.py::TestFoundationBackendComputeBundleVersion -v
```

Expected: all 3 tests pass.

### Step 3: Full suite

```bash
uv run pytest tests/ -v
```

### Step 4: Commit

```bash
git add distro-server/src/amplifier_distro/server/session_backend.py \
        distro-server/tests/test_session_backend.py
git commit -m "feat: implement _compute_bundle_version() using overlay bundle.yaml mtime"
```

---

## Task 11: Write Failing Test — `_SessionHandle` Has `bundle_version` and `surface` Fields

**File:** `distro-server/tests/test_session_backend.py`

**Step 1: Append this new test class**

```python
# ── _SessionHandle new fields ─────────────────────────────────────────────


class TestSessionHandlePrewarmFields:
    """_SessionHandle must carry bundle_version and surface for prewarm support."""

    def test_session_handle_has_bundle_version_field_defaulting_to_empty_string(self):
        """bundle_version defaults to '' — no bundle version known yet."""
        from amplifier_distro.server.session_backend import _SessionHandle

        handle = _SessionHandle(
            session_id="s001",
            project_id="p001",
            working_dir=Path("/tmp"),
            session=None,
        )
        assert handle.bundle_version == ""

    def test_session_handle_has_surface_field_defaulting_to_none(self):
        """surface defaults to None — no surface attached until create_session()."""
        from amplifier_distro.server.session_backend import _SessionHandle

        handle = _SessionHandle(
            session_id="s002",
            project_id="p002",
            working_dir=Path("/tmp"),
            session=None,
        )
        assert handle.surface is None

    def test_session_handle_accepts_explicit_bundle_version(self):
        """bundle_version can be set at construction time."""
        from amplifier_distro.server.session_backend import _SessionHandle

        handle = _SessionHandle(
            session_id="s003",
            project_id="p003",
            working_dir=Path("/tmp"),
            session=None,
            bundle_version="1700000000.0",
        )
        assert handle.bundle_version == "1700000000.0"
```

**Step 2: Run to confirm FAIL**

```bash
uv run pytest tests/test_session_backend.py::TestSessionHandlePrewarmFields -v
```

Expected:
```
FAILED ... TypeError: _SessionHandle.__init__() got an unexpected keyword argument 'bundle_version'
```

---

## Task 12: Add `bundle_version` and `surface` to `_SessionHandle`; Populate in `create_session()`; Verify Pass; Commit

**Files:**
- Modify: `distro-server/src/amplifier_distro/server/session_backend.py`

### Step 1: Add the new fields to `_SessionHandle`

Find the `_SessionHandle` dataclass (line 44). Add two new fields after the existing `prepared` field:

```python
@dataclass
class _SessionHandle:
    """Lightweight handle wrapping a foundation session.

    Keeps the metadata the backend needs without coupling to bridge
    internals.  The ``session`` field holds the actual
    ``AmplifierSession`` object from amplifier-foundation.
    """

    session_id: str
    project_id: str
    working_dir: Path
    session: Any  # AmplifierSession from foundation
    _cleanup_done: bool = field(default=False, repr=False)
    prepared: Any = field(default=None, repr=False)
    # Bundle version at session creation time (overlay bundle.yaml mtime).
    # Stored for staleness detection — a mismatch means the bundle was reloaded
    # while this session was running.
    bundle_version: str = field(default="", repr=False)
    # Surface reference for on_bundle_reload notification.
    # Type is Any to avoid importing SessionSurface (fix/approval-display) here.
    surface: Any = field(default=None, repr=False)
```

### Step 2: Pass `bundle_version` when creating the handle in `create_session()`

Find the `_SessionHandle(...)` constructor call inside `create_session()` (around line 518). Add `bundle_version=self._bundle_version` to it:

```python
        handle = _SessionHandle(
            session_id=session_id,
            project_id=project_id,
            working_dir=wd,
            session=session,
            prepared=prepared,
            bundle_version=self._bundle_version,
        )
```

### Step 3: Write a test for this population (add to `TestSessionHandlePrewarmFields`)

Add one more test inside `TestSessionHandlePrewarmFields`:

```python
    async def test_create_session_stores_bundle_version_on_handle(
        self, bridge_backend
    ):
        """create_session() stamps the handle with the current _bundle_version."""
        mock_session = MagicMock()
        mock_session.session_id = "sess-bv-001"

        mock_prepared = MagicMock()
        mock_prepared.create_session = AsyncMock(return_value=mock_session)
        bridge_backend._load_bundle = AsyncMock(return_value=mock_prepared)
        bridge_backend._bundle_version = "mtime-99999.0"

        from amplifier_distro.server.session_backend import FoundationBackend

        await FoundationBackend.create_session(bridge_backend, working_dir="/tmp")

        handle = bridge_backend._sessions["sess-bv-001"]
        assert handle.bundle_version == "mtime-99999.0"

        # Cleanup worker
        if "sess-bv-001" in bridge_backend._worker_tasks:
            bridge_backend._worker_tasks["sess-bv-001"].cancel()
```

### Step 4: Run all `TestSessionHandlePrewarmFields` tests to confirm PASS

```bash
uv run pytest tests/test_session_backend.py::TestSessionHandlePrewarmFields -v
```

Expected: 4 tests pass.

### Step 5: Full suite

```bash
uv run pytest tests/ -v
```

### Step 6: Commit

```bash
git add distro-server/src/amplifier_distro/server/session_backend.py \
        distro-server/tests/test_session_backend.py
git commit -m "feat: add bundle_version and surface fields to _SessionHandle; stamp bundle_version in create_session()"
```

---

## Task 13: Wire `backend.startup()` to FastAPI Startup Event; Test; Commit

**Files:**
- Modify: `distro-server/src/amplifier_distro/server/services.py`
- Modify: `distro-server/src/amplifier_distro/server/app.py`
- Modify: `distro-server/tests/test_services.py`

This is a wiring task. There's no pure "failing test first" here because the test is an integration check on a function that doesn't exist yet. Write the test, watch it fail with `ImportError`, implement, watch it pass.

### Step 1: Write the failing test in `test_services.py`

Add this new class at the **end** of `test_services.py`:

```python
# ---------------------------------------------------------------------------
# start_services() — pre-warm backend at startup
# ---------------------------------------------------------------------------


class TestStartServices:
    """start_services() calls backend.startup() if available."""

    @pytest.mark.asyncio
    async def test_start_services_calls_backend_startup(self):
        """start_services() must call backend.startup() when the backend has it."""
        from unittest.mock import AsyncMock

        from amplifier_distro.server.services import (
            init_services,
            reset_services,
            start_services,
        )

        mock_backend = AsyncMock()
        mock_backend.startup = AsyncMock()

        reset_services()
        init_services(backend=mock_backend)

        await start_services()

        mock_backend.startup.assert_awaited_once()
        reset_services()

    @pytest.mark.asyncio
    async def test_start_services_safe_without_startup_method(self):
        """start_services() must not raise if backend lacks startup()."""
        from amplifier_distro.server.services import (
            init_services,
            reset_services,
            start_services,
        )
        from amplifier_distro.server.session_backend import MockBackend

        reset_services()
        init_services(backend=MockBackend())

        await start_services()  # MockBackend has no startup() — must not raise
        reset_services()

    @pytest.mark.asyncio
    async def test_start_services_safe_before_init(self):
        """start_services() must not raise if services were never initialized."""
        from amplifier_distro.server.services import reset_services, start_services

        reset_services()
        await start_services()  # should silently do nothing
```

### Step 2: Run to confirm FAIL

```bash
uv run pytest tests/test_services.py::TestStartServices -v
```

Expected:
```
FAILED ... ImportError: cannot import name 'start_services' from 'amplifier_distro.server.services'
```

### Step 3: Add `start_services()` to `services.py`

Find the existing `stop_services()` function (line 127). Add `start_services()` directly after it, following the identical pattern:

```python
async def start_services() -> None:
    """Pre-warm the backend bundle at server startup.

    Called during FastAPI startup (wired in app.py via add_event_handler).
    Safe to call even if services haven't been initialized or if the backend
    doesn't implement startup() (e.g., MockBackend in dev mode).
    """
    with _instance_lock:
        instance = _instance

    if instance is None:
        return

    backend = instance.backend
    if hasattr(backend, "startup"):
        await backend.startup()
```

### Step 4: Wire `start_services` to FastAPI startup in `app.py`

Find `DistroServer.__init__` in `app.py` (line 90). Find the line that registers the shutdown handler:

```python
        self._app.add_event_handler("shutdown", stop_services)
```

Add the startup handler directly above it:

```python
        # Pre-warm the bundle at server startup
        from amplifier_distro.server.services import start_services, stop_services

        self._app.add_event_handler("startup", start_services)
        self._app.add_event_handler("shutdown", stop_services)
```

> **Note:** The existing import `from amplifier_distro.server.services import stop_services` must be replaced with the combined import above to avoid a duplicate import.

### Step 5: Run `TestStartServices` to confirm PASS

```bash
uv run pytest tests/test_services.py::TestStartServices -v
```

Expected: 3 tests pass.

### Step 6: Full suite

```bash
uv run pytest tests/ -v
```

### Step 7: Commit

```bash
git add distro-server/src/amplifier_distro/server/services.py \
        distro-server/src/amplifier_distro/server/app.py \
        distro-server/tests/test_services.py
git commit -m "feat: wire backend.startup() to FastAPI startup event via start_services()"
```

---

## Task 14: Call `backend.reload_bundle()` from `overlay.py` After Writes; Test; Commit

**Files:**
- Modify: `distro-server/src/amplifier_distro/overlay.py`
- Create: `distro-server/tests/test_overlay_reload.py`

### Step 1: Create the test file

Create `/Users/samule/repo/amplifier-distro-msft/.worktrees/fix-bundle-prewarm/distro-server/tests/test_overlay_reload.py` with the following content:

```python
"""Tests that overlay writes trigger a live bundle reload.

When the server is running (services initialized) and the overlay is written
(via ensure_overlay, add_include, or remove_include), _write_overlay() must
schedule a reload task on the running event loop.  When no server is running
(CLI / wizard context), the call must be silently skipped.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestOverlayWriteTriggersReload:
    """_write_overlay() fires backend.reload_bundle() when a server is running."""

    async def test_write_overlay_schedules_reload_when_services_available(
        self, tmp_path, monkeypatch
    ):
        """After _write_overlay() completes, reload_bundle() is eventually awaited."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "amplifier_distro.conventions.DISTRO_OVERLAY_DIR",
            str(tmp_path / "bundle"),
        )

        mock_backend = MagicMock()
        mock_backend.reload_bundle = AsyncMock()

        from amplifier_distro.server.services import init_services, reset_services

        reset_services()
        init_services(backend=mock_backend)

        try:
            from amplifier_distro.overlay import _write_overlay

            _write_overlay(
                {
                    "bundle": {
                        "name": "test-overlay",
                        "version": "0.1.0",
                        "description": "test",
                    }
                }
            )

            # Yield to the event loop so the create_task callback runs
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            mock_backend.reload_bundle.assert_awaited_once()
        finally:
            reset_services()

    def test_write_overlay_safe_when_no_services_initialized(
        self, tmp_path, monkeypatch
    ):
        """_write_overlay() must not raise when services are not initialized."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "amplifier_distro.conventions.DISTRO_OVERLAY_DIR",
            str(tmp_path / "bundle"),
        )

        from amplifier_distro.server.services import reset_services

        reset_services()  # Ensure no services are initialized

        from amplifier_distro.overlay import _write_overlay

        # Must not raise RuntimeError even though get_services() would raise
        _write_overlay({"bundle": {"name": "test", "version": "0.1.0"}})

    async def test_write_overlay_safe_when_backend_has_no_reload_bundle(
        self, tmp_path, monkeypatch
    ):
        """_write_overlay() must not raise when backend lacks reload_bundle()."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "amplifier_distro.conventions.DISTRO_OVERLAY_DIR",
            str(tmp_path / "bundle"),
        )

        from amplifier_distro.server.session_backend import MockBackend
        from amplifier_distro.server.services import init_services, reset_services

        reset_services()
        init_services(backend=MockBackend())

        try:
            from amplifier_distro.overlay import _write_overlay

            # MockBackend has no reload_bundle() — must not raise
            _write_overlay({"bundle": {"name": "test", "version": "0.1.0"}})
            await asyncio.sleep(0)
        finally:
            reset_services()
```

### Step 2: Run to confirm FAIL

```bash
uv run pytest tests/test_overlay_reload.py -v
```

Expected:
```
FAILED ... test_write_overlay_schedules_reload_when_services_available
  AssertionError: Expected 'reload_bundle' to have been awaited once. Awaited 0 times.
PASSED ... test_write_overlay_safe_when_no_services_initialized       ← accidentally passes
PASSED ... test_write_overlay_safe_when_backend_has_no_reload_bundle  ← accidentally passes
```

The first test fails because `_write_overlay()` doesn't trigger reload yet.

### Step 3: Modify `_write_overlay()` in `overlay.py`

Find `_write_overlay()` (line 58). Add the reload trigger at the end of the function body, before `return path`:

```python
def _write_overlay(data: dict[str, Any]) -> Path:
    """Write the overlay bundle.yaml to disk.

    After writing, schedules a live bundle reload on the running event loop
    (if a server is active) so a server restart isn't required.
    """
    path = overlay_bundle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    # Trigger live bundle reload if the server is running.
    # Uses get_running_loop() so this is a no-op when called from the CLI / wizard
    # (no event loop running) and silently skips when services aren't initialized.
    try:
        from amplifier_distro.server.services import get_services

        services = get_services()
        if hasattr(services.backend, "reload_bundle"):
            loop = asyncio.get_running_loop()
            loop.create_task(services.backend.reload_bundle())
    except RuntimeError:
        # RuntimeError covers both "services not initialized" (from get_services())
        # and "no running event loop" (from get_running_loop()) — both are expected
        # in non-server contexts and can be safely ignored.
        pass

    return path
```

Add `import asyncio` at the top of `overlay.py` (it currently doesn't import asyncio). The top of the file currently has:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
```

Change it to:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml
```

### Step 4: Run overlay reload tests to confirm PASS

```bash
uv run pytest tests/test_overlay_reload.py -v
```

Expected: all 3 tests pass.

### Step 5: Full suite

```bash
uv run pytest tests/ -v
```

### Step 6: Commit

```bash
git add distro-server/src/amplifier_distro/overlay.py \
        distro-server/tests/test_overlay_reload.py
git commit -m "feat: trigger backend.reload_bundle() after overlay writes"
```

---

## Task 15: Final Green Suite — Run Everything; Commit If Clean

### Step 1: Run the entire test suite with verbose output

```bash
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-bundle-prewarm/distro-server
uv run pytest tests/ -v 2>&1 | tail -40
```

Expected summary line:
```
===== N passed in X.XXs =====
```

Where N ≥ 945 (baseline) plus all the new tests added in this plan. Count the new tests:
- Task 1–2: 2 cache tests
- Task 3–4: 3 startup tests
- Task 5–8: 6 reload tests
- Task 9–10: 3 bundle version tests
- Task 11–12: 4 handle fields tests
- Task 13: 3 start_services tests
- Task 14: 3 overlay reload tests

**Total new tests: 24.** Final count should be ≥ 969.

### Step 2: Verify no warnings about unawaited coroutines

```bash
uv run pytest tests/ -v -W error::RuntimeWarning 2>&1 | tail -20
```

If this raises `RuntimeWarning` failures, there is an unawaited coroutine somewhere in the new code. Fix before continuing.

### Step 3: Final commit (if not already green)

If all tests pass and there are no new linting issues:

```bash
git add -A
git commit -m "test: verify all 969 tests pass after bundle pre-warm implementation"
```

---

## Appendix: Quick Reference

### Where Each New Symbol Lives After This Plan

| Symbol | File | Line (approx) |
|--------|------|---------------|
| `FoundationBackend._prepared_bundle` | `session_backend.py` | `__init__` |
| `FoundationBackend._bundle_version` | `session_backend.py` | `__init__` |
| `FoundationBackend.startup()` | `session_backend.py` | after `_load_bundle()` |
| `FoundationBackend.reload_bundle()` | `session_backend.py` | after `startup()` |
| `FoundationBackend._compute_bundle_version()` | `session_backend.py` | after `reload_bundle()` |
| `_SessionHandle.bundle_version` | `session_backend.py` | dataclass field |
| `_SessionHandle.surface` | `session_backend.py` | dataclass field |
| `start_services()` | `services.py` | after `stop_services()` |
| FastAPI startup wiring | `app.py` | `DistroServer.__init__` |
| reload trigger | `overlay.py` | end of `_write_overlay()` |

### Common Mistakes to Avoid

1. **Forgetting to update `bridge_backend` fixture** when new fields are added to `__init__`. Tests will fail with `AttributeError` instead of the expected behavior.

2. **Calling `asyncio.create_task()` without a running loop.** The `_write_overlay()` trigger uses `asyncio.get_running_loop()` which raises `RuntimeError` when there's no loop — this is intentional and caught by the `except RuntimeError` block.

3. **Not cancelling worker tasks in tests.** Any test that calls `create_session()` via `FoundationBackend.create_session(bridge_backend, ...)` creates a real asyncio task. Add cleanup at the end:
   ```python
   if "sess-id" in bridge_backend._worker_tasks:
       bridge_backend._worker_tasks["sess-id"].cancel()
   ```

4. **`startup()` calling `_load_bundle()` when `_prepared_bundle` is already set.** This can't happen in normal operation (startup is called once before any sessions exist) but could cause issues in tests. The `bridge_backend` fixture sets `_prepared_bundle = None`, so `startup()` in tests always hits the real load path — which is why tests mock `bridge_backend._load_bundle` before calling `startup()`.

5. **`reload_bundle()` notification loop modifying `_sessions` while iterating.** The implementation uses `list(self._sessions.items())` — the `list()` call takes a snapshot so that if `on_bundle_reload()` causes a session to end (and remove itself from `_sessions`), the loop doesn't skip items or raise `RuntimeError`.
