# Distro Server — 6 Remaining Issues Design

## Goal

Fix six outstanding issues in `distro-server` covering bundle pre-warming, session surface abstraction, async correctness, Slack resource cleanup, reconnect robustness, and incorrect overlay injection.

## Background

`amplifier-distro-msft/distro-server` is a FastAPI server providing web chat, Slack, and voice interfaces built on `amplifier-foundation`. Six known issues have been validated and scoped for fix: slow first-session startup, an incomplete surface abstraction, two async correctness bugs, leaked aiohttp sessions in Slack, and a hook module being injected via the wrong mechanism in `overlay.py`.

## Worktrees and Merge Order

```
.worktrees/
├── fix/await-cancel      # Issues 3 + 5 (session_backend.py only)
├── fix/slack-aiohttp     # Issue 4
├── fix/overlay-hooks     # Issue 6 (bundle/behaviors/start.yaml + overlay.py)
├── fix/approval-display  # Issue 2 (SessionSurface abstraction)
└── fix/bundle-prewarm    # Issue 1
```

**Recommended merge order:** `await-cancel` → `overlay-hooks` → `slack-aiohttp` → `approval-display` → `bundle-prewarm`

---

## Issue 1 — Bundle Pre-warm

**Worktree:** `fix/bundle-prewarm`

### Problem

`_load_bundle()` is lazy-loaded on the first `create_session()` call, taking 7–37 seconds. If the WebSocket closes before the session is ready, the user sees "Session creation failed."

### Design

- Add `async def startup(self) -> None` to `FoundationBackend`. It calls `await self._load_bundle()` and stores the result in `self._prepared_bundle: Any | None = None`.
- `_load_bundle()` returns `self._prepared_bundle` if already set (cache hit), otherwise loads and prepares normally.
- Wire `backend.startup()` to FastAPI's startup event in `services.py` via `app.add_event_handler("startup", backend.startup)`.
- Add `async def reload_bundle(self) -> None` for live overlay reload: invalidates `self._prepared_bundle = None`, re-runs `await self._load_bundle()`, then calls `surface.on_bundle_reload()` on each active session's surface (each surface defines its own restart policy).
- Each `_SessionHandle` gets a `bundle_version` tag (hash of overlay mtime) so resumed sessions can detect bundle mismatch and log a warning.
- `overlay.py` (the settings app) calls `backend.reload_bundle()` after writing a new overlay — no server restart needed.

### Files Changed

- `distro-server/session_backend.py` — `startup()`, `reload_bundle()`, `_prepared_bundle` cache, `bundle_version` on `_SessionHandle`
- `distro-server/services.py` — wire startup event handler
- `distro-server/overlay.py` — call `backend.reload_bundle()` after overlay write

---

## Issue 2 — SessionSurface Abstraction

**Worktree:** `fix/approval-display`

### Problem

`_wire_event_queue` only registers `approval_system` and `display_system` when `event_queue is not None`. Surfaces (Slack, Voice, headless) have no way to provide their own implementations. Three separate args at different abstraction levels is confusing.

### Design

New `SessionSurface` dataclass in `session_backend.py` (or `protocol_adapters.py`):

```python
@dataclass
class SessionSurface:
    event_queue: asyncio.Queue | None = None
    approval_system: Any | None = None
    display_system: Any | None = None
    on_bundle_reload: Callable[[], Awaitable[None]] | None = None
```

Factory functions per surface type:

- `web_chat_surface(queue)` — wires all three through the queue (existing behaviour)
- `slack_surface(slack_client)` — no queue, Slack-native approval/display
- `headless_surface()` — no queue, `ApprovalSystem(auto_approve=True)` + `LogDisplaySystem()`

`create_session(surface=SessionSurface | None = None)` replaces `create_session(event_queue=...)`. Same change on `resume_session`. Resolution order inside `create_session`:

1. If `surface` explicitly provided → use its fields
2. If `surface` is `None` → use `headless_surface()` defaults

`_wire_event_queue` is replaced by `_attach_surface(session, surface)` which sets all coordinator capabilities from the surface.

New `LogDisplaySystem` class added to `protocol_adapters.py` — implements `show_message / push_nesting / pop_nesting` by routing to Python `logging.getLogger`. ~15 lines.

`SessionBackend` protocol updated to use `surface=` parameter.

### Files Changed

- `distro-server/session_backend.py` — `SessionSurface` dataclass, `_attach_surface()`, updated `create_session` / `resume_session` signatures
- `distro-server/protocol_adapters.py` — `LogDisplaySystem`, factory functions `web_chat_surface`, `slack_surface`, `headless_surface`
- `apps/slack/__init__.py` — switch to `slack_surface()`
- All call sites of `create_session(event_queue=...)` updated to `create_session(surface=...)`

---

## Issue 3 — `request_cancel` Not Awaited

**Worktree:** `fix/await-cancel`

### Problem

`session_backend.py:_SessionHandle.cancel()` calls `request_cancel(level)` without `await`, producing `RuntimeWarning: coroutine 'ModuleCoordinator.request_cancel' was never awaited` on every server shutdown.

### Design

```python
# Before
request_cancel(level)

# After
if asyncio.iscoroutinefunction(request_cancel):
    await request_cancel(level)
else:
    request_cancel(level)
```

The `iscoroutinefunction` guard future-proofs the call site if the coordinator API changes.

### Files Changed

- `distro-server/session_backend.py` — `_SessionHandle.cancel()`

---

## Issue 4 — Slack aiohttp Unclosed Sessions

**Worktree:** `fix/slack-aiohttp`

### Problem

`slack-bolt` and `slack-sdk` create `aiohttp.ClientSession` objects that are abandoned on server shutdown, producing `ERROR asyncio: Unclosed client session`.

### Design

In `apps/slack/__init__.py`:

- Declare a module-level `_slack_aiohttp_session: aiohttp.ClientSession | None = None`
- In `on_startup`: create `_slack_aiohttp_session = aiohttp.ClientSession()`, pass it explicitly to `AsyncWebClient(token=..., session=_slack_aiohttp_session)` and the Socket Mode adapter
- In `on_shutdown`: after stopping the socket adapter and bridge, call `await _slack_aiohttp_session.close()` if not already closed

Both the Web client session and the Socket Mode session get explicit references and are closed during shutdown.

### Files Changed

- `apps/slack/__init__.py` — module-level session reference, explicit session injection, close in `on_shutdown`

---

## Issue 5 — FileNotFoundError on Reconnect

**Worktree:** `fix/await-cancel` (same worktree as Issue 3)

### Problem

`_reconnect()` fails with `FileNotFoundError: [Errno 2] No such file or directory` when the server process's CWD has been deleted, because `BundleRegistry.__init__` calls `Path.cwd()` → `os.getcwd()`.

### Design

CWD safety guard in `_reconnect()`, inserted before calling `_load_bundle()`:

```python
try:
    os.getcwd()
except FileNotFoundError:
    os.chdir(os.path.expanduser("~"))
```

Edge-case safety net — moves the process to home if the CWD has been deleted, then lets the rest of `_reconnect()` proceed normally.

### Files Changed

- `distro-server/session_backend.py` — `_reconnect()`, CWD guard before `_load_bundle()`

---

## Issue 6 — `overlay.py` Incorrectly Injects `hooks-session-naming`

**Worktree:** `fix/overlay-hooks`

### Problem

`overlay.py:ensure_overlay()` injects `hooks-session-naming` as a bundle include in the user overlay:

```yaml
includes:
  - bundle: git+https://...#subdirectory=modules/hooks-session-naming
```

But `modules/hooks-session-naming` is a Python module package (has `pyproject.toml` entry point, not `bundle.md`). The registry tries to load it as a bundle and fires "Not a valid bundle" on every session.

Additionally, the overlay is for user-specific configuration (provider choice, feature flags). System-level hooks like session naming belong in the distro's default bundle at `bundle/behaviors/start.yaml`.

Investigation confirmed: `hooks-session-naming` is not currently declared anywhere in the `bundle/` directory chain. `bundle/behaviors/start.yaml` currently only declares `hooks-handoff` and `hooks-preflight`.

### Design

**Change 1 — Add `hooks-session-naming` to `bundle/behaviors/start.yaml`:**

```yaml
hooks:
  - module: hooks-handoff
    source: git+https://github.com/payneio/amplifier-start@main#subdirectory=modules/hooks-handoff
    config:
      enabled: true
  - module: hooks-preflight
    source: git+https://github.com/payneio/amplifier-start@main#subdirectory=modules/hooks-preflight
    config:
      enabled: true
      blocking: false
  - module: hooks-session-naming
    source: git+https://github.com/microsoft/amplifier-foundation@main#subdirectory=modules/hooks-session-naming
    config:
      initial_trigger_turn: 2
      update_interval_turns: 5
```

**Change 2 — Remove from `overlay.py`:** Delete the `SESSION_NAMING_URI` constant and all three injection sites (fresh overlay creation, existing overlay update, `current_uris` check).

**Change 3 — Migration:** On startup, `overlay.py` checks if an existing user overlay contains the stale `SESSION_NAMING_URI` entry under `includes:` and removes it.

**Result:** Session naming becomes a first-class declared hook in the distro bundle. The overlay remains purely for user configuration.

### Files Changed

- `bundle/behaviors/start.yaml` — add `hooks-session-naming` hook entry
- `distro-server/overlay.py` — remove `SESSION_NAMING_URI` constant, all three injection sites, add migration logic on startup

---

## Open Questions

- **Verify bundle transitivity:** Confirm whether `AMPLIFIER_START_URI` bundle transitively includes `foundation:behaviors/sessions` (which declares `hooks-session-naming`). If yes, the `bundle/behaviors/start.yaml` entry may be redundant and can be omitted.
- **`hooks-progress-monitor` missing entry point:** `amplifier-foundation` is missing the `pyproject.toml` entry point for `hooks-progress-monitor` — worth a separate PR in that repo.
- **Surface factory scope:** Decide whether `web_chat_surface` / `slack_surface` factory functions ship as part of the `fix/approval-display` worktree or as follow-on per-surface PRs.
