# fix/slack-aiohttp Implementation Plan

> **For execution:** Use `/execute-plan` mode or the subagent-driven-development recipe.

**Goal:** Eliminate "Unclosed client session" errors on server shutdown by explicitly managing the aiohttp ClientSession used by the Slack bridge.

**Architecture:** Declare a module-level `_slack_aiohttp_session` in `apps/slack/__init__.py`. Create it in `on_startup()` (socket mode only) and inject it into `SocketModeAdapter` so the adapter reuses one long-lived session rather than creating a new one per reconnect. Close it explicitly in `on_shutdown()`. Modify `SocketModeAdapter` to accept an externally-owned session and skip closing it in `_close_ws()`.

**Tech Stack:** Python 3.11+, aiohttp, slack-bolt, slack-sdk, pytest, uv

---

## Codebase Context

Before touching anything, read these files in full:

```
distro-server/src/amplifier_distro/server/apps/slack/__init__.py   (387 lines)
distro-server/src/amplifier_distro/server/apps/slack/socket_mode.py (426 lines)
distro-server/tests/test_slack_bridge.py                            (2264 lines)
distro-server/tests/test_socket_mode.py                             (107 lines)
distro-server/tests/conftest.py                                     (60 lines)
```

Key facts:
- `HttpSlackClient` uses `httpx` (context-managed, no leaked sessions). No changes needed there.
- `SocketModeAdapter._connection_loop()` creates a **new** `aiohttp.ClientSession()` on every
  reconnect attempt. Even though `_close_ws()` closes it, there are edge cases (race on startup,
  server forcibly killed) where it leaks. The fix: inject one long-lived external session.
- Tests use `asyncio.run()` inside sync test methods for simple cases, and `async def` test
  methods (no `@pytest.mark.asyncio` decorator needed — `asyncio_mode = "auto"` is configured)
  for tests that need genuine async composition.
- Module-level `_state: dict[str, Any]` and `_state_lock` already exist. We're adding a parallel
  `_slack_aiohttp_session: aiohttp.ClientSession | None = None` at the same scope.

---

## Task 1: Write the failing test class

**Files:**
- Modify: `distro-server/tests/test_slack_bridge.py`

Scroll to the **very end** of `test_slack_bridge.py` (currently ends around line 2264) and append
the following class. Do not change any existing code.

**Step 1: Append the new test class**

```python
# ---------------------------------------------------------------------------
# Issue 4 — Aiohttp session cleanup
# ---------------------------------------------------------------------------


class TestAiohttpSessionCleanup:
    """Verify that on_shutdown explicitly closes the shared aiohttp ClientSession.

    Issue 4: SocketModeAdapter creates aiohttp.ClientSession objects that are
    abandoned on server shutdown, producing 'ERROR asyncio: Unclosed client
    session' in the asyncio error log.

    Fix: manage a module-level _slack_aiohttp_session, inject it into the
    adapter, and close it in on_shutdown().
    """

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _initialized_state(self):
        """Return minimal initialized _state (simulator mode, no network)."""
        from amplifier_distro.server.apps.slack.backend import MockBackend
        from amplifier_distro.server.apps.slack.client import MemorySlackClient
        from amplifier_distro.server.apps.slack.config import SlackConfig

        return dict(
            config=SlackConfig(simulator_mode=True),
            client=MemorySlackClient(),
            backend=MockBackend(),
        )

    def _fresh_module(self):
        """Return the slack app module with state cleared."""
        import amplifier_distro.server.apps.slack as slack_app

        slack_app._state.clear()
        return slack_app

    # ------------------------------------------------------------------ #
    # Primary TDD test: drives the implementation                         #
    # ------------------------------------------------------------------ #

    async def test_on_shutdown_closes_open_module_session(self):
        """on_shutdown() must call close() on _slack_aiohttp_session when it
        is open and set the variable back to None afterwards."""
        from unittest.mock import AsyncMock, MagicMock

        slack_app = self._fresh_module()
        deps = self._initialized_state()
        slack_app.initialize(**deps)

        # Inject a mock open session directly into the module
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        slack_app._slack_aiohttp_session = mock_session

        await slack_app.on_shutdown()

        mock_session.close.assert_called_once()
        assert slack_app._slack_aiohttp_session is None

    # ------------------------------------------------------------------ #
    # Guard tests: expected to fail for the same reason until the fix     #
    # lands, then pass immediately                                         #
    # ------------------------------------------------------------------ #

    async def test_on_shutdown_does_not_double_close_already_closed_session(self):
        """on_shutdown() must NOT call close() when the session is already
        closed (aiohttp raises RuntimeError on double-close)."""
        from unittest.mock import AsyncMock, MagicMock

        slack_app = self._fresh_module()
        deps = self._initialized_state()
        slack_app.initialize(**deps)

        mock_session = MagicMock()
        mock_session.closed = True  # already closed
        mock_session.close = AsyncMock()
        slack_app._slack_aiohttp_session = mock_session

        await slack_app.on_shutdown()

        mock_session.close.assert_not_called()

    async def test_on_shutdown_with_no_session_does_not_crash(self):
        """on_shutdown() must not raise when _slack_aiohttp_session is None
        (the normal simulator / unconfigured path)."""
        slack_app = self._fresh_module()
        deps = self._initialized_state()
        slack_app.initialize(**deps)

        slack_app._slack_aiohttp_session = None

        # Should complete without raising
        await slack_app.on_shutdown()

        assert slack_app._slack_aiohttp_session is None

    async def test_socket_mode_adapter_uses_injected_session(self):
        """SocketModeAdapter must use the externally-provided session for
        ws_connect() instead of creating its own."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from amplifier_distro.server.apps.slack.socket_mode import SocketModeAdapter

        config = MagicMock()
        config.app_token = "xapp-test"
        event_handler = MagicMock()
        event_handler.handle_event_payload = AsyncMock(return_value={"ok": True})

        # Build a mock aiohttp session whose ws_connect returns a fake WebSocket
        mock_ws = MagicMock()
        mock_ws.closed = False
        mock_ws.close = AsyncMock()

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=mock_ws)

        adapter = SocketModeAdapter(config, event_handler, session=mock_session)

        # Patch _get_ws_url and _process_frames so we don't make real network calls
        with (
            patch.object(adapter, "_get_ws_url", AsyncMock(return_value="wss://fake")),
            patch.object(adapter, "_process_frames", AsyncMock()),
            patch.object(adapter, "_resolve_bot_id", AsyncMock(return_value="U_BOT")),
        ):
            # Run one iteration of the connection loop then stop
            adapter._running = True

            async def run_once():
                # Temporarily patch _running to stop after one iteration
                original = adapter._process_frames

                call_count = 0

                async def stop_after_first():
                    nonlocal call_count
                    call_count += 1
                    adapter._running = False
                    await original()

                adapter._process_frames = stop_after_first
                try:
                    await adapter._connection_loop()
                finally:
                    adapter._process_frames = original

            await run_once()

        # The injected session must have been used for ws_connect
        mock_session.ws_connect.assert_called_once_with("wss://fake")

    async def test_socket_mode_adapter_does_not_close_injected_session(self):
        """_close_ws() must NOT close the externally-injected session because
        the caller (on_shutdown) owns its lifetime."""
        from unittest.mock import AsyncMock, MagicMock

        from amplifier_distro.server.apps.slack.socket_mode import SocketModeAdapter

        config = MagicMock()
        event_handler = MagicMock()

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()

        adapter = SocketModeAdapter(config, event_handler, session=mock_session)
        adapter._session = mock_session  # simulate it being "in use"
        adapter._ws = None

        await adapter._close_ws()

        # The externally-owned session must NOT be closed by the adapter
        mock_session.close.assert_not_called()
        # But the reference is cleared so the adapter no longer holds it
        assert adapter._session is None
```

**Step 2: Run the tests to confirm they all FAIL**

```
cd /Users/samule/repo/amplifier-distro-msft/distro-server
uv run pytest tests/test_slack_bridge.py::TestAiohttpSessionCleanup -v
```

Expected output — every test must fail (not error, not skip). You will see failures like:

```
AttributeError: module 'amplifier_distro.server.apps.slack' has no attribute '_slack_aiohttp_session'
```

and

```
TypeError: SocketModeAdapter.__init__() got an unexpected keyword argument 'session'
```

If any test **passes** here, stop and re-read the test — it is testing the wrong thing.

**Step 3: Commit the failing tests**

```bash
cd /Users/samule/repo/amplifier-distro-msft
git add distro-server/tests/test_slack_bridge.py
git commit -m "test(slack): add failing tests for aiohttp session cleanup (Issue 4)"
```

---

## Task 2: Implement `socket_mode.py` changes

**Files:**
- Modify: `distro-server/src/amplifier_distro/server/apps/slack/socket_mode.py`

The `SocketModeAdapter` currently creates a **new** `aiohttp.ClientSession()` inside
`_connection_loop` on each reconnect. We need it to accept an externally-managed session,
use it instead of creating its own, and **not** close it (since the caller owns it).

Three surgical changes:

**Step 1: Read the file first (required)**

```bash
cat -n distro-server/src/amplifier_distro/server/apps/slack/socket_mode.py
```

**Step 2: Add `session=` parameter to `__init__`**

Current `__init__` signature (line 57–76):
```python
    def __init__(
        self,
        config: SlackConfig,
        event_handler: SlackEventHandler,
    ) -> None:
        self._config = config
        self._event_handler = event_handler
        self._task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._bot_user_id: str | None = None
        self._seen_events: dict[str, float] = {}
        self._pending_tasks: set[asyncio.Task] = set()
```

Replace with:
```python
    def __init__(
        self,
        config: SlackConfig,
        event_handler: SlackEventHandler,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._config = config
        self._event_handler = event_handler
        self._task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._external_session: aiohttp.ClientSession | None = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._bot_user_id: str | None = None
        self._seen_events: dict[str, float] = {}
        self._pending_tasks: set[asyncio.Task] = set()
```

**Step 3: Update `_connection_loop` to use injected session**

Current block inside `_connection_loop` (lines 133–136):
```python
                session = aiohttp.ClientSession()
                self._session = session
                self._ws = await session.ws_connect(url)
```

Replace with:
```python
                if self._external_session is not None:
                    session = self._external_session
                else:
                    session = aiohttp.ClientSession()
                self._session = session
                self._ws = await session.ws_connect(url)
```

**Step 4: Update `_close_ws` to skip closing an externally-owned session**

Current `_close_ws` (lines 386–400):
```python
    async def _close_ws(self) -> None:
        """Close WebSocket and HTTP session."""
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except OSError:
                logger.debug("Error closing WebSocket", exc_info=True)
        self._ws = None

        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except OSError:
                logger.debug("Error closing HTTP session", exc_info=True)
        self._session = None
```

Replace with:
```python
    async def _close_ws(self) -> None:
        """Close WebSocket and owned HTTP session.

        If the session was injected externally (via constructor ``session=``
        parameter), we do **not** close it here — the caller owns its lifetime.
        """
        if self._ws and not self._ws.closed:
            try:
                await self._ws.close()
            except OSError:
                logger.debug("Error closing WebSocket", exc_info=True)
        self._ws = None

        # Only close the session when we created it ourselves
        if (
            self._session is not None
            and self._session is not self._external_session
            and not self._session.closed
        ):
            try:
                await self._session.close()
            except OSError:
                logger.debug("Error closing HTTP session", exc_info=True)
        self._session = None
```

**Step 5: Verify the socket_mode tests still pass**

```bash
cd /Users/samule/repo/amplifier-distro-msft/distro-server
uv run pytest tests/test_socket_mode.py -v
```

Expected: all tests PASS (no regressions in the existing socket mode test suite).

---

## Task 3: Implement `__init__.py` changes

**Files:**
- Modify: `distro-server/src/amplifier_distro/server/apps/slack/__init__.py`

Three changes: add a module-level variable, update `on_startup`, update `on_shutdown`.

**Step 1: Read the file first (required)**

```bash
cat -n distro-server/src/amplifier_distro/server/apps/slack/__init__.py
```

**Step 2: Add module-level session variable**

Current module-level state block (lines 47–50):
```python
# --- Global bridge state (initialized on startup) ---

_state: dict[str, Any] = {}
_state_lock = threading.Lock()
```

Replace with:
```python
# --- Global bridge state (initialized on startup) ---

_state: dict[str, Any] = {}
_state_lock = threading.Lock()

# Shared aiohttp session for Socket Mode connections.
# Created in on_startup() when socket mode is active; closed in on_shutdown().
# None in simulator mode (MemorySlackClient needs no HTTP session).
_slack_aiohttp_session: "aiohttp.ClientSession | None" = None
```

The forward-reference string `"aiohttp.ClientSession | None"` avoids an unconditional import of
`aiohttp` at module load time (aiohttp is an optional dependency gated behind the `[slack]` extra).

**Step 3: Update `on_startup` to create and inject the session**

Current `on_startup` (lines 142–165):
```python
async def on_startup() -> None:
    """Initialize the Slack bridge on server startup."""
    initialize()
    with _state_lock:
        config: SlackConfig = _state["config"]
    logger.info(f"Slack bridge initialized (mode: {config.mode})")

    # Start Socket Mode connection if configured
    if config.socket_mode and config.is_configured:
        try:
            from .socket_mode import SocketModeAdapter

            with _state_lock:
                adapter = SocketModeAdapter(config, _state["event_handler"])
                _state["socket_adapter"] = adapter
            await adapter.start()
            logger.info("Socket Mode connection started")
        except ImportError:
            logger.error(
                "Socket Mode requires optional dependencies: "
                "uv pip install amplifier-distro[slack]  (aiohttp missing)"
            )
        except Exception:
            logger.exception("Socket Mode startup failed; Slack bridge degraded")
```

Replace with:
```python
async def on_startup() -> None:
    """Initialize the Slack bridge on server startup."""
    global _slack_aiohttp_session
    initialize()
    with _state_lock:
        config: SlackConfig = _state["config"]
    logger.info(f"Slack bridge initialized (mode: {config.mode})")

    # Start Socket Mode connection if configured
    if config.socket_mode and config.is_configured:
        try:
            import aiohttp

            from .socket_mode import SocketModeAdapter

            # Create one long-lived session shared across all Socket Mode
            # connections. Injecting it prevents the adapter from creating
            # (and potentially leaking) a new session on every reconnect.
            _slack_aiohttp_session = aiohttp.ClientSession()

            with _state_lock:
                adapter = SocketModeAdapter(
                    config,
                    _state["event_handler"],
                    session=_slack_aiohttp_session,
                )
                _state["socket_adapter"] = adapter
            await adapter.start()
            logger.info("Socket Mode connection started")
        except ImportError:
            logger.error(
                "Socket Mode requires optional dependencies: "
                "uv pip install amplifier-distro[slack]  (aiohttp missing)"
            )
        except Exception:
            logger.exception("Socket Mode startup failed; Slack bridge degraded")
```

**Step 4: Update `on_shutdown` to close the session**

Current `on_shutdown` (lines 168–189):
```python
async def on_shutdown() -> None:
    """Clean up the Slack bridge on server shutdown."""
    with _state_lock:
        socket_adapter = _state.get("socket_adapter")
        session_manager = _state.get("session_manager")
        backend = _state.get("backend")

    # Stop Socket Mode connection if running
    if socket_adapter is not None:
        await socket_adapter.stop()

    if session_manager is not None and backend is not None:
        # End all active sessions
        for mapping in session_manager.list_active():
            try:
                await backend.end_session(mapping.session_id)
            except (RuntimeError, ValueError, ConnectionError, OSError):
                logger.exception(f"Error ending session {mapping.session_id}")

    with _state_lock:
        _state.clear()
    logger.info("Slack bridge shut down")
```

Replace with:
```python
async def on_shutdown() -> None:
    """Clean up the Slack bridge on server shutdown."""
    global _slack_aiohttp_session
    with _state_lock:
        socket_adapter = _state.get("socket_adapter")
        session_manager = _state.get("session_manager")
        backend = _state.get("backend")

    # Stop Socket Mode connection if running
    if socket_adapter is not None:
        await socket_adapter.stop()

    if session_manager is not None and backend is not None:
        # End all active sessions
        for mapping in session_manager.list_active():
            try:
                await backend.end_session(mapping.session_id)
            except (RuntimeError, ValueError, ConnectionError, OSError):
                logger.exception(f"Error ending session {mapping.session_id}")

    # Close the shared aiohttp session that was injected into SocketModeAdapter.
    # Guard against double-close (aiohttp raises RuntimeError on an already-closed
    # session). In simulator mode this variable is None — no-op.
    if _slack_aiohttp_session is not None and not _slack_aiohttp_session.closed:
        await _slack_aiohttp_session.close()
    _slack_aiohttp_session = None

    with _state_lock:
        _state.clear()
    logger.info("Slack bridge shut down")
```

---

## Task 4: Verify the failing tests now pass

**Step 1: Run just the new test class**

```bash
cd /Users/samule/repo/amplifier-distro-msft/distro-server
uv run pytest tests/test_slack_bridge.py::TestAiohttpSessionCleanup -v
```

Expected output:

```
tests/test_slack_bridge.py::TestAiohttpSessionCleanup::test_on_shutdown_closes_open_module_session PASSED
tests/test_slack_bridge.py::TestAiohttpSessionCleanup::test_on_shutdown_does_not_double_close_already_closed_session PASSED
tests/test_slack_bridge.py::TestAiohttpSessionCleanup::test_on_shutdown_with_no_session_does_not_crash PASSED
tests/test_slack_bridge.py::TestAiohttpSessionCleanup::test_socket_mode_adapter_uses_injected_session PASSED
tests/test_slack_bridge.py::TestAiohttpSessionCleanup::test_socket_mode_adapter_does_not_close_injected_session PASSED

5 passed in ...
```

If any test fails, **stop here**. Read the failure message carefully. Common mistakes:
- Forgot to declare `global _slack_aiohttp_session` in `on_shutdown`
- The `_external_session` attribute name in `socket_mode.py` is misspelled
- The `_close_ws` identity check uses `==` instead of `is`

Do not proceed to Task 5 until all 5 tests pass.

---

## Task 5: Run the full test suite

**Step 1: Run all 945 tests**

```bash
cd /Users/samule/repo/amplifier-distro-msft/distro-server
uv run pytest tests/ -v 2>&1 | tail -30
```

Expected: ≥ 945 tests passing, **0 failures**, **0 errors**.

If the count is exactly 945 + 5 = 950 passing, that's perfect (the 5 new tests counted).

**Step 2: If there are failures, diagnose before touching anything**

Do not guess. Read the failure traceback completely. The most likely regressions:

| Symptom | Cause | Fix |
|---|---|---|
| `TypeError: __init__() got unexpected keyword 'session'` | Forgot to save `socket_mode.py` | Re-check the edit |
| `AttributeError: 'SocketModeAdapter' has no attribute '_external_session'` | `__init__` edit incomplete | Re-read lines 57–76 of the file |
| Existing `TestSocketModeStop` fails | `_close_ws` logic changed incorrectly | The existing tests create adapters without `session=`; `_external_session` will be `None`, so the session-close path must still work for self-created sessions |

For the last case: when `_external_session is None`, the guard in `_close_ws` is:
```python
self._session is not self._external_session  # True (X is not None → True)
```
So a self-created session WILL still be closed correctly. Verify this logic in your edit.

---

## Task 6: Commit

Once all tests pass, commit all three changed files together:

```bash
cd /Users/samule/repo/amplifier-distro-msft

git add distro-server/src/amplifier_distro/server/apps/slack/__init__.py
git add distro-server/src/amplifier_distro/server/apps/slack/socket_mode.py
git add distro-server/tests/test_slack_bridge.py

git commit -m "fix(slack): explicitly manage aiohttp ClientSession to prevent unclosed session warnings on shutdown

Issue 4 from 2026-02-27-distro-remaining-issues.md

Problem: SocketModeAdapter creates a new aiohttp.ClientSession() on every
reconnect inside _connection_loop. Although _close_ws() tries to close it,
race conditions between stop() and the background task could leave sessions
open, producing 'ERROR asyncio: Unclosed client session' on every shutdown.

Fix:
- Add _slack_aiohttp_session module-level variable in apps/slack/__init__.py
- Create one long-lived aiohttp.ClientSession in on_startup() (socket mode only)
- Inject it into SocketModeAdapter via new constructor session= parameter
- SocketModeAdapter reuses the injected session instead of creating its own;
  _close_ws() skips closing externally-owned sessions
- on_shutdown() explicitly closes and NULLs the session after stopping the adapter

Simulator mode is unaffected: _slack_aiohttp_session remains None.
Adds 5 new tests in TestAiohttpSessionCleanup."
```

---

## Appendix: What was NOT changed (and why)

**`client.py` / `HttpSlackClient`:** This class uses `httpx.AsyncClient()` as an async context
manager inside `_api_call()`. Each call creates and closes its own client. No aiohttp involved,
no leak possible. Do not touch this file.

**`initialize()` function:** No session parameter needed. The aiohttp session is only relevant
for the socket mode path, which is wired entirely inside `on_startup()`. Tests that call
`initialize()` directly (e.g. `bridge_client` fixture) continue to work unchanged.

**Simulator mode `on_startup` path:** The `if config.socket_mode and config.is_configured` guard
ensures `aiohttp.ClientSession()` is never imported or created in simulator/unconfigured mode.
No aiohttp dependency is introduced for the testing path.
