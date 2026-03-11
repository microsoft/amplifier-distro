# Bundle Prewarm + Loading Screen

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate 30+ second cold-start on first session by prewarming the bundle at server startup and showing a loading screen while it prepares.

**Architecture:** Restructure amplifierd's `_lifespan` to yield before bundle loading so the server accepts connections immediately. Run bundle prewarm (load + prepare) as a cancellable background task. The distro plugin registers the overlay bundle, serves a loading screen that polls `GET /ready`, and triggers reload when the overlay changes.

**Tech Stack:** Python 3.12, FastAPI lifespan, asyncio.Event + asyncio.Task, amplifier-foundation BundleRegistry

**Repos touched:**
- `amplifierd` (upstream daemon) — lifespan restructure, `/ready` endpoint, 503 route guard, background prewarm
- `amplifierd-plugin-distro` (this repo) — overlay registration, loading screen, reload trigger

**Repos NOT touched:**
- `amplifier-foundation` — use public API only (`registry.update()` for cache invalidation)
- `amplifier-chat` — consumer only; 503 route guard in amplifierd handles it

---

## Problem

When `amplifierd` starts, `_lifespan()` calls `await bundle_registry.load(default_bundle)` **before** `yield`. This means:

1. The server doesn't accept HTTP connections for 30+ seconds on first run (git clones + SDK installs via `bundle.prepare()`)
2. No loading screen is possible — nobody can reach the server
3. If bundle loading fails, the server never starts

Additionally, the distro plugin writes a user-customized overlay bundle to `~/.amplifier-distro/bundle/bundle.yaml` but never registers it with `BundleRegistry`. Sessions get the raw upstream "distro" bundle, ignoring the user's provider and feature selections.

## Design

### Current `_lifespan` flow (app.py)

```
1. Settings, EventBus, InstallStateManager.invalidate()
2. BundleRegistry created, settings.bundles registered (name->URI, no I/O)
3. await registry.load(default_bundle)          <- BLOCKS 30+ seconds
4. SessionManager created
5. discover_plugins() -> create_router(state)
6. yield                                         <- server starts here (too late)
```

### New `_lifespan` flow

```
1.  Settings, EventBus, InstallStateManager.invalidate()
2.  BundleRegistry created, settings.bundles registered (name->URI, no I/O)
3.  SessionManager created                       <- moved up (doesn't need loaded bundle)
4.  discover_plugins() -> create_router(state)   <- moved up (so plugins can register overlay)
5.  app.state.bundles_ready = asyncio.Event()
6.  app.state.prewarm_task = asyncio.create_task(_prewarm(...))
7.  yield                                        <- server accepts connections IMMEDIATELY
    |-- User hits /distro/ -> sees loading.html
    |-- _prewarm runs: load -> inject_providers -> prepare
    |-- On success: bundles_ready.set()
    |-- On error: prewarm_error set, bundles_ready stays UNSET
    +-- loading.html polls GET /ready -> redirects when {ready: true}
--- shutdown ---
8.  Cancel prewarm task if still running
9.  session_manager.shutdown()
```

### Key design decisions

**1. No new classes — just asyncio primitives on app.state**

```python
app.state.bundles_ready: asyncio.Event       # readiness signal (set ONLY on success)
app.state.prewarm_task: asyncio.Task | None  # cancellable task ref
app.state.prewarm_error: str | None          # error message if prewarm failed
```

No `prepared_bundle` cache — after prewarm, `registry.load()` is a cache hit (instant) and `prepare()` finds SDKs already installed (2-5s no-op). This avoids shared mutable state, cache coherence problems on reload, and a second code path in `SessionManager.create()`.

**2. Prewarm replicates the full SessionManager.create() pipeline**

`bundle.prepare()` must be called AFTER `inject_providers()` — otherwise provider SDKs won't be installed. The prewarm task does:

```python
async def _prewarm(app):
    registry = app.state.bundle_registry
    bundle = await registry.load("distro")
    providers = load_provider_config()
    inject_providers(bundle, providers)
    await bundle.prepare()
    app.state.bundles_ready.set()
```

**3. No SessionManager changes — warm caches are sufficient**

After prewarm completes:
- `registry.load("distro")` -> `_loaded_bundles` cache hit -> instant
- `inject_providers()` -> in-memory dict merge -> instant
- `bundle.prepare()` -> `uv pip install -e` finds packages installed -> 2-5s no-op

The 2-5s no-op is invisible to users who just waited through a loading screen. Caching `PreparedBundle` would introduce shared mutable state and a second code path in SessionManager for marginal benefit.

**4. Route-level 503 guard during prewarm**

Session creation (`POST /sessions`) and resume (`POST /sessions/{id}/resume`) return 503 with `Retry-After: 5` header when `bundles_ready` is not set. This prevents:
- Concurrent `prepare()` subprocess runs between prewarm and session creation
- Confusing errors for programmatic API clients

The loading screen prevents browser users from hitting this in practice.

**5. Overlay registration happens in plugin startup**

The distro plugin's `create_router(state)` registers the overlay path with `BundleRegistry`, shadowing the well-known "distro" URI:

```python
def create_router(state):
    settings = DistroPluginSettings()
    if overlay_exists(settings):
        overlay_dir = str(Path(settings.distro_home) / "bundle")
        state.bundle_registry.register({"distro": overlay_dir})
    ...
```

`register()` overwrites the existing "distro" entry's URI (from `git+https://...` to the local path). This is intentional — the overlay's `bundle.yaml` includes the upstream distro bundle via its `includes:` list, so the upstream bundle is still pulled in via composition.

Since plugin discovery now runs BEFORE the prewarm task starts, the prewarm loads the overlay (with user's providers and features) instead of the raw upstream bundle.

**6. Live-reload uses `registry.update()` for cache invalidation**

When the overlay changes (user adds/removes a provider or feature), the plugin:
1. Re-registers the overlay URI (in case it was just created by the wizard)
2. Cancels any in-flight prewarm task — **serially**: `await old_task` after `cancel()` before starting new one (prevents concurrent `uv pip install` subprocess runs)
3. Calls `await registry.update("distro")` — bypasses the `_loaded_bundles` cache for the root bundle and re-reads from disk
4. Clears `bundles_ready`, clears `prewarm_error`
5. Starts a new prewarm task

**Why `registry.update()` works:** Code-intel verified the implementation. `_load_single(refresh=True)` bypasses the cache check at registry.py line 364. The `# noqa: ARG002` comment is stale — the parameter IS used. For overlay changes, included sub-bundles that haven't changed are correctly served from cache (fast), while new includes are loaded fresh.

**Why serial cancellation matters:** `asyncio.Task.cancel()` is cooperative — it raises `CancelledError` at the next `await`. But `bundle.prepare()` runs blocking `subprocess.run()` calls. If we cancel and immediately start a new prewarm, two `uv pip install` processes run concurrently on the same venv. The fix:

```python
old_task.cancel()
try:
    await old_task  # Wait for subprocess to actually finish
except (asyncio.CancelledError, Exception):
    pass
# NOW safe to start new prewarm
```

**7. Reload is debounced**

Rapid wizard changes (toggling multiple features) each call `add_include()` / `remove_include()`. Each overlay write triggers a reload request. A 500ms debounce ensures only one reload actually fires after a burst of changes.

**8. Loading screen**

`loading.html` is a self-contained HTML page (inline CSS/JS, no external dependencies) served by the distro plugin when `bundles_ready` is not set. It:
- Shows an animated loading indicator
- Polls `GET /ready` every 2 seconds with `AbortController` timeout (3s per request)
- On `{ready: true}`: check phase, redirect to `/distro/setup` (unconfigured) or `/distro/` (ready)
- On `{ready: false, error: "..."}`: show error message with retry button (calls `POST /ready/retry`)
- On `{ready: false}` (no error): keep polling
- 60-second timeout: show "taking longer than expected" with continue-anyway link

**9. Error recovery**

If prewarm fails (network error, missing `uv`, etc.):
- `prewarm_error` is set with the error message
- `bundles_ready` stays **UNSET** (not set on error — loading screen keeps showing)
- `GET /ready` returns `{ready: false, error: "..."}` — loading screen shows error + retry button
- The 60-second timeout independently prevents infinite hanging
- `POST /ready/retry` clears error and starts a fresh prewarm attempt
- Server restart always retries (fresh `BundleRegistry`, fresh task)

---

## Changes by repo

### amplifierd (upstream daemon)

| File | Change |
|------|--------|
| `src/amplifierd/app.py` | Restructure `_lifespan`: move SessionManager + plugin discovery before yield, add background prewarm task, add `bundles_ready`/`prewarm_error` to app.state, cancel task on shutdown |
| `src/amplifierd/routes/health.py` | Add `GET /ready` and `POST /ready/retry` endpoints |
| `src/amplifierd/routes/sessions.py` | Add 503 guard on `POST /sessions` and `POST /sessions/{id}/resume` when `bundles_ready` not set |

### amplifierd-plugin-distro (this repo)

| File | Change |
|------|--------|
| `src/distro_plugin/__init__.py` | Register overlay with BundleRegistry in `create_router(state)` |
| `src/distro_plugin/overlay.py` | After each `_write_overlay()`, trigger debounced reload via `app.state` |
| `src/distro_plugin/routes.py` | Modify `get_dashboard()` to serve `loading.html` when bundles not ready; pass `app` ref to overlay functions |
| `src/distro_plugin/static/loading.html` | New: self-contained loading page with poll + redirect logic |
| `src/distro_plugin/reload.py` | New: `request_reload(app)` function with debounce + serial cancel + restart logic |

---

## Implementation Plan

### Task 1: Restructure `_lifespan` in amplifierd (yield before loading)

**Files:**
- Modify: `amplifierd/src/amplifierd/app.py`
- Test: `amplifierd/tests/test_app.py`

**Step 1: Write failing test** — test that server accepts connections before bundle loading completes

```python
# tests/test_app.py
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_server_accepts_connections_before_bundle_load():
    """Server should accept HTTP connections while bundle is still loading."""
    load_started = asyncio.Event()
    load_blocked = asyncio.Event()

    async def slow_load(name):
        load_started.set()
        await load_blocked.wait()  # Block until test releases
        return AsyncMock()()

    with patch("amplifierd.app.BundleRegistry") as MockRegistry:
        instance = MockRegistry.return_value
        instance.register = lambda x: None
        instance.load = slow_load

        from amplifierd.app import create_app
        app = create_app()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Server should respond to /health even while bundle is loading
            response = await client.get("/health")
            assert response.status_code == 200

            # Bundle load should have started
            assert load_started.is_set()

            # Release the blocked load
            load_blocked.set()
```

**Step 2:** Run test, verify it FAILS (current code blocks on load before yield).

**Step 3: Implement** — Restructure `_lifespan` in `app.py`:
- Move `SessionManager` creation and `discover_plugins()` before `yield`
- Move `registry.load()` into a background `_prewarm` task that also calls `inject_providers()` + `bundle.prepare()`
- Add `bundles_ready`, `prewarm_task`, `prewarm_error` to `app.state`
- On prewarm success: `bundles_ready.set()`
- On prewarm failure: set `prewarm_error`, do NOT set `bundles_ready`
- Cancel prewarm task on shutdown

**Step 4:** Run test, verify PASS.

**Step 5:** Commit: `feat(amplifierd): restructure lifespan to yield before bundle loading`

---

### Task 2: Add `GET /ready`, `POST /ready/retry`, and 503 route guard

**Files:**
- Modify: `amplifierd/src/amplifierd/routes/health.py`
- Modify: `amplifierd/src/amplifierd/routes/sessions.py`
- Test: `amplifierd/tests/test_health.py`

**Step 1: Write failing tests**

```python
@pytest.mark.asyncio
async def test_ready_returns_false_during_prewarm(client_with_slow_bundle):
    response = await client_with_slow_bundle.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["ready"] is False

@pytest.mark.asyncio
async def test_ready_returns_true_after_prewarm(client_with_loaded_bundle):
    response = await client_with_loaded_bundle.get("/ready")
    data = response.json()
    assert data["ready"] is True

@pytest.mark.asyncio
async def test_ready_returns_error_on_failure(client_with_failed_bundle):
    response = await client_with_failed_bundle.get("/ready")
    data = response.json()
    assert data["ready"] is False  # NOT set on error
    assert data["error"] is not None

@pytest.mark.asyncio
async def test_session_creation_returns_503_during_prewarm(client_with_slow_bundle):
    response = await client_with_slow_bundle.post("/sessions", json={"bundle_name": "distro"})
    assert response.status_code == 503
    assert "Retry-After" in response.headers
```

**Step 2:** Run test, verify FAIL.

**Step 3: Implement:**
- `GET /ready`: returns `{ready: bool, error: str | null}` based on `app.state.bundles_ready.is_set()` and `app.state.prewarm_error`
- `POST /ready/retry`: clears `prewarm_error`, clears `bundles_ready`, cancels existing task, starts new prewarm
- In `sessions.py`: add guard at top of `create_session()` and `resume_session()`:
  ```python
  bundles_ready = getattr(request.app.state, "bundles_ready", None)
  if bundles_ready and not bundles_ready.is_set():
      raise HTTPException(503, detail="Bundles loading", headers={"Retry-After": "5"})
  ```

**Step 4:** Run test, verify PASS.

**Step 5:** Commit: `feat(amplifierd): add /ready endpoint and 503 guard during prewarm`

---

### Task 3: Distro plugin registers overlay with BundleRegistry

**Files:**
- Modify: `amplifierd-plugin-distro/src/distro_plugin/__init__.py`
- Test: `amplifierd-plugin-distro/tests/test_init.py`

**Step 1: Write failing test**

```python
def test_create_router_registers_overlay_when_exists(tmp_path, mock_state):
    """Plugin should register overlay path as 'distro' bundle."""
    overlay_dir = tmp_path / "bundle"
    overlay_dir.mkdir()
    (overlay_dir / "bundle.yaml").write_text("bundle:\n  name: test\n")

    with patch.dict(os.environ, {"DISTRO_PLUGIN_DISTRO_HOME": str(tmp_path)}):
        create_router(mock_state)

    mock_state.bundle_registry.register.assert_called()
    call_args = mock_state.bundle_registry.register.call_args[0][0]
    assert "distro" in call_args

def test_create_router_skips_registration_when_no_overlay(tmp_path, mock_state):
    """Plugin should not register if overlay doesn't exist."""
    with patch.dict(os.environ, {"DISTRO_PLUGIN_DISTRO_HOME": str(tmp_path)}):
        create_router(mock_state)

    mock_state.bundle_registry.register.assert_not_called()
```

**Step 2:** Run test, verify FAIL.

**Step 3: Implement** — In `create_router(state)`:
- After creating settings, check `overlay_exists(settings)`
- If overlay exists and `state.bundle_registry` is not None, call `state.bundle_registry.register({"distro": str(overlay_dir)})`
- This deliberately overwrites the well-known git URI — the overlay includes the upstream bundle via composition

**Step 4:** Run test, verify PASS.

**Step 5:** Commit: `feat(distro-plugin): register overlay bundle with BundleRegistry`

---

### Task 4: Create loading.html

**Files:**
- Create: `amplifierd-plugin-distro/src/distro_plugin/static/loading.html`

**Step 1:** Create `loading.html` — self-contained page with:
- Animated "amplifier" wordmark (inline CSS, no external deps — server may be slow serving static files during prewarm)
- Polls `GET /ready` every 2 seconds with `AbortController` timeout (3s per request)
- Three response states:
  - `{ready: true}` -> check phase via `GET /distro/status`, redirect to `/distro/setup` (unconfigured) or `/distro/` (ready)
  - `{ready: false, error: "..."}` -> show error message + retry button (calls `POST /ready/retry`)
  - `{ready: false}` (no error) -> keep polling
- 60-second overall timeout: show "taking longer than expected" with continue-anyway link

**Step 2:** Commit: `feat(distro-plugin): add loading.html for bundle prewarm screen`

---

### Task 5: Serve loading screen from dashboard route

**Files:**
- Modify: `amplifierd-plugin-distro/src/distro_plugin/routes.py`
- Test: `amplifierd-plugin-distro/tests/test_routes.py`

**Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_dashboard_serves_loading_when_not_ready(client_not_ready):
    response = await client_not_ready.get("/distro/")
    assert response.status_code == 200
    assert "loading" in response.text.lower()

@pytest.mark.asyncio
async def test_dashboard_serves_dashboard_when_ready(client_ready):
    response = await client_ready.get("/distro/")
    assert response.status_code == 200
    assert "dashboard" in response.text.lower()
```

**Step 2:** Run test, verify FAIL.

**Step 3: Implement** — In `get_dashboard()`:
- Before the existing `compute_phase` check, check `bundles_ready`:
  ```python
  bundles_ready = getattr(request.app.state, "bundles_ready", None)
  if bundles_ready and not bundles_ready.is_set():
      html_path = _STATIC_DIR / "loading.html"
      return HTMLResponse(content=html_path.read_text())
  ```
- Otherwise, existing dashboard logic unchanged

**Step 4:** Run test, verify PASS.

**Step 5:** Commit: `feat(distro-plugin): serve loading screen during bundle prewarm`

---

### Task 6: Create reload module with debounce + serial cancellation

**Files:**
- Create: `amplifierd-plugin-distro/src/distro_plugin/reload.py`
- Test: `amplifierd-plugin-distro/tests/test_reload.py`

**Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_reload_awaits_old_task_before_starting_new(mock_app):
    """Reload must await old task completion (serial cancellation)."""
    from distro_plugin.reload import request_reload
    old_task = mock_app.state.prewarm_task
    await request_reload(mock_app)
    # Old task must have been awaited (not just cancelled)
    assert old_task.await_count > 0
    assert mock_app.state.prewarm_task is not old_task

@pytest.mark.asyncio
async def test_reload_debounces_rapid_calls(mock_app):
    """Multiple rapid reload requests should coalesce into one."""
    from distro_plugin.reload import request_reload
    for _ in range(5):
        request_reload(mock_app, debounce_seconds=0.1)
    await asyncio.sleep(0.3)
    assert mock_app.state.reload_count == 1
```

**Step 2:** Run test, verify FAIL.

**Step 3: Implement** `reload.py`:
- `request_reload(app, debounce_seconds=0.5)`: cancels any pending debounce timer, schedules new one via `call_later`
- When debounce fires (`_do_reload(app)`):
  1. Cancel old task: `old_task.cancel()` then `await old_task` (wrapped in try/except)
  2. Re-register overlay if it exists: `registry.register({"distro": overlay_dir})`
  3. Invalidate cache: `await registry.update("distro")`
  4. Clear state: `bundles_ready.clear()`, `prewarm_error = None`
  5. Start new prewarm: `app.state.prewarm_task = asyncio.create_task(_prewarm(app))`

**Step 4:** Run test, verify PASS.

**Step 5:** Commit: `feat(distro-plugin): reload module with debounce and serial cancellation`

---

### Task 7: Wire overlay writes to trigger reload

**Files:**
- Modify: `amplifierd-plugin-distro/src/distro_plugin/overlay.py`
- Modify: `amplifierd-plugin-distro/src/distro_plugin/routes.py`
- Test: `amplifierd-plugin-distro/tests/test_overlay.py`

**Step 1: Write failing test**

```python
def test_add_include_triggers_reload(mock_app, tmp_overlay):
    settings = tmp_overlay
    add_include(settings, "git+https://example.com/bundle@main", app=mock_app)
    assert mock_app.reload_requested

def test_add_include_works_without_app(tmp_overlay):
    """Backward compat: app parameter is optional."""
    settings = tmp_overlay
    add_include(settings, "git+https://example.com/bundle@main")
    # Should not raise
```

**Step 2:** Run test, verify FAIL.

**Step 3: Implement:**
- Add optional `app` parameter to `_write_overlay()`, `add_include()`, `remove_include()`
- After writing, if `app` is provided, call `request_reload(app)`
- In `routes.py`, pass `request.app` to overlay mutation calls:
  - `post_features` (line 420)
  - `post_tier` (line 450)
  - `step_modules` (line 489)
  - `step_provider` (line 525)
  - `post_provider` (line 391)

**Step 4:** Run test, verify PASS.

**Step 5:** Commit: `feat(distro-plugin): overlay writes trigger bundle reload`

---

### Task 8: Integration tests

**Files:**
- Create: `amplifierd-plugin-distro/tests/test_prewarm_integration.py`

**Step 1: Write integration tests**

```python
@pytest.mark.asyncio
async def test_full_prewarm_flow():
    """End-to-end: server starts -> loading screen -> prewarm completes -> dashboard."""
    # 1. Start app with slow bundle mock
    # 2. GET /distro/ -> should return loading.html
    # 3. GET /ready -> {ready: false}
    # 4. Release the slow load
    # 5. GET /ready -> {ready: true}
    # 6. GET /distro/ -> should return dashboard.html
    ...

@pytest.mark.asyncio
async def test_overlay_change_during_prewarm():
    """Changing overlay while prewarm runs should cancel and restart."""
    # 1. Start app with slow bundle mock
    # 2. While loading, POST /distro/features to toggle a feature
    # 3. Original prewarm should be cancelled (after current subprocess finishes)
    # 4. New prewarm should start with updated overlay
    # 5. GET /ready eventually -> {ready: true}
    ...

@pytest.mark.asyncio
async def test_session_creation_blocked_during_prewarm():
    """POST /sessions returns 503 while bundles are loading."""
    # 1. Start app with slow bundle mock
    # 2. POST /sessions -> 503 with Retry-After header
    # 3. Release the slow load
    # 4. POST /sessions -> 201
    ...

@pytest.mark.asyncio
async def test_prewarm_error_shows_on_loading_screen():
    """Failed prewarm should show error, not redirect."""
    # 1. Start app with failing bundle mock
    # 2. GET /ready -> {ready: false, error: "..."}
    # 3. POST /ready/retry -> restarts prewarm
    # 4. GET /ready -> {ready: true} (after fixing the mock)
    ...
```

**Step 2:** Run test, verify PASS.

**Step 3:** Commit: `test: integration tests for bundle prewarm flow`

---

## Task dependency graph

```
Task 1 (lifespan restructure)
  +-> Task 2 (GET /ready + 503 guard)

Task 3 (overlay registration)  <- independent, can parallel with 1-2

Task 4 (loading.html)          <- independent, can parallel with 1-3

Task 5 (serve loading screen)  <- depends on Task 1 + Task 4

Task 6 (reload module)         <- depends on Task 1

Task 7 (wire overlay->reload)  <- depends on Task 3 + Task 6

Task 8 (integration test)      <- depends on all above
```

Tasks 1, 3, and 4 can be done in parallel. Task 2 follows Task 1. Tasks 5-7 follow their dependencies. Task 8 is last.
