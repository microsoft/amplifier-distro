# Session Spawning Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Register `session.spawn` capability in `FoundationBackend` so the `delegate` and `recipes` tools can spawn sub-sessions instead of failing with "Session spawning not available."

**Architecture:** Foundation's `PreparedBundle.spawn()` does all the heavy lifting. We create a thin `spawn_registration.py` that builds a `child_bundle` from agent config and calls `prepared.spawn()`. Wire it into `FoundationBackend` by storing `prepared` on `_SessionHandle` and calling `register_spawning()` after session creation. Sub-session events flow to the parent's event queue, activating the existing `delegate:*` UI in chat.

**Tech Stack:** `amplifier_foundation` (`PreparedBundle`, `Bundle`, `generate_sub_session_id`), `amplifier_core` (`ALL_EVENTS`, `HookResult`), existing `session_backend.py` infrastructure.

**Key references:**
- Reference spawn impl: `~/.amplifier/cache/amplifier-foundation-.../examples/07_full_workflow.py:225-297`
- Spawn fn signature (what tool-delegate calls): `agent_name, instruction, parent_session, agent_configs, sub_session_id, tool_inheritance, hook_inheritance, orchestrator_config, provider_preferences, self_delegation_depth, **kwargs`
- Wire point: `distro-server/src/amplifier_distro/server/session_backend.py`
- Existing tests: `distro-server/tests/test_session_backend.py`

---

## Task 1: Create `spawn_registration.py`

**Files:**
- Create: `distro-server/src/amplifier_distro/server/spawn_registration.py`
- Create: `distro-server/tests/test_spawn_registration.py`

**Step 1: Write the failing test**

```python
# distro-server/tests/test_spawn_registration.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from amplifier_distro.server.spawn_registration import register_spawning


def make_mock_session(session_id="test-session-001"):
    session = MagicMock()
    session.session_id = session_id
    session.config = {
        "agents": {
            "test-agent": {
                "instruction": "You are a test agent.",
                "tools": [],
                "providers": [],
                "hooks": [],
                "session": {},
            }
        }
    }
    coordinator = MagicMock()
    coordinator.register_capability = MagicMock()
    session.coordinator = coordinator
    return session


def make_mock_prepared(agents=None):
    prepared = MagicMock()
    bundle = MagicMock()
    bundle.agents = agents or {}
    prepared.bundle = bundle
    prepared.spawn = AsyncMock(return_value={"response": "done", "session_id": "child-001"})
    return prepared


def test_register_spawning_registers_capability():
    """register_spawning puts session.spawn on the coordinator."""
    session = make_mock_session()
    prepared = make_mock_prepared()
    register_spawning(session, prepared, "test-session-001")
    session.coordinator.register_capability.assert_called_once()
    call_args = session.coordinator.register_capability.call_args
    assert call_args[0][0] == "session.spawn"
    assert asyncio.iscoroutinefunction(call_args[0][1])


@pytest.mark.asyncio
async def test_spawn_fn_calls_prepared_spawn_for_known_agent():
    """spawn fn resolves named agent from agent_configs and calls prepared.spawn."""
    session = make_mock_session()
    prepared = make_mock_prepared()
    register_spawning(session, prepared, "test-session-001")

    spawn_fn = session.coordinator.register_capability.call_args[0][1]
    result = await spawn_fn(
        agent_name="test-agent",
        instruction="do the thing",
        parent_session=session,
        agent_configs={"test-agent": {"instruction": "You are a test agent.", "tools": [], "providers": [], "hooks": [], "session": {}}},
        sub_session_id="child-001",
    )

    prepared.spawn.assert_called_once()
    call_kwargs = prepared.spawn.call_args[1]
    assert call_kwargs["instruction"] == "do the thing"
    assert call_kwargs["session_id"] == "child-001"
    assert result == {"response": "done", "session_id": "child-001"}


@pytest.mark.asyncio
async def test_spawn_fn_falls_back_to_bundle_agents():
    """spawn fn finds agent in prepared.bundle.agents when not in agent_configs."""
    session = make_mock_session()
    prepared = make_mock_prepared(agents={"bundle-agent": {"instruction": "Bundle agent."}})
    register_spawning(session, prepared, "test-session-001")

    spawn_fn = session.coordinator.register_capability.call_args[0][1]
    await spawn_fn(
        agent_name="bundle-agent",
        instruction="go",
        parent_session=session,
        agent_configs={},
        sub_session_id="child-002",
    )
    prepared.spawn.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_fn_handles_self_agent():
    """'self' agent uses empty config (inherits from parent via prepared.spawn)."""
    session = make_mock_session()
    prepared = make_mock_prepared()
    register_spawning(session, prepared, "test-session-001")

    spawn_fn = session.coordinator.register_capability.call_args[0][1]
    await spawn_fn(
        agent_name="self",
        instruction="recurse",
        parent_session=session,
        agent_configs={},
        sub_session_id="child-003",
    )
    prepared.spawn.assert_called_once()


@pytest.mark.asyncio
async def test_spawn_fn_unknown_agent_raises():
    """Unknown agent name raises ValueError with helpful message."""
    session = make_mock_session()
    prepared = make_mock_prepared()
    register_spawning(session, prepared, "test-session-001")

    spawn_fn = session.coordinator.register_capability.call_args[0][1]
    with pytest.raises(ValueError, match="not found"):
        await spawn_fn(
            agent_name="nonexistent",
            instruction="fail",
            parent_session=session,
            agent_configs={},
            sub_session_id="child-bad",
        )


@pytest.mark.asyncio
async def test_spawn_fn_forwards_provider_preferences():
    """provider_preferences is forwarded to prepared.spawn."""
    session = make_mock_session()
    prepared = make_mock_prepared()
    register_spawning(session, prepared, "test-session-001")

    prefs = [{"provider": "anthropic", "model": "claude-*"}]
    spawn_fn = session.coordinator.register_capability.call_args[0][1]
    await spawn_fn(
        agent_name="test-agent",
        instruction="go",
        parent_session=session,
        agent_configs={"test-agent": {}},
        sub_session_id="child-004",
        provider_preferences=prefs,
    )
    call_kwargs = prepared.spawn.call_args[1]
    assert call_kwargs["provider_preferences"] == prefs


@pytest.mark.asyncio
async def test_spawn_fn_accepts_extra_kwargs():
    """spawn fn accepts unknown kwargs without crashing (future-proof)."""
    session = make_mock_session()
    prepared = make_mock_prepared()
    register_spawning(session, prepared, "test-session-001")

    spawn_fn = session.coordinator.register_capability.call_args[0][1]
    # Should not raise even with unknown kwargs
    await spawn_fn(
        agent_name="test-agent",
        instruction="go",
        parent_session=session,
        agent_configs={"test-agent": {}},
        sub_session_id="child-005",
        some_future_kwarg="ignored",
    )
```

**Step 2: Run test to confirm it fails**

```bash
cd distro-server && uv run pytest tests/test_spawn_registration.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'amplifier_distro.server.spawn_registration'`

**Step 3: Write minimal implementation**

```python
# distro-server/src/amplifier_distro/server/spawn_registration.py
"""Session spawning capability for the distro server.

Registers the ``session.spawn`` capability on a coordinator so the
``delegate`` and ``recipes`` tools can spawn sub-sessions.

Without this, both tools return "Session spawning not available" and the
LLM falls back to inline execution in the parent session -- no sub-agent
isolation, no sub-agent nesting cards in the chat UI.

Reference implementation: amplifier-foundation examples/07_full_workflow.py
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def register_spawning(session: Any, prepared: Any, session_id: str) -> None:
    """Register ``session.spawn`` capability on *session*'s coordinator.

    Args:
        session:    AmplifierSession whose coordinator receives the capability.
        prepared:   PreparedBundle used to create *session*. Its ``spawn()``
                    method and ``bundle.agents`` registry are used for
                    sub-session creation.
        session_id: ID of *session* (for logging only).
    """
    coordinator = session.coordinator

    async def spawn_fn(
        agent_name: str,
        instruction: str,
        parent_session: Any,
        agent_configs: dict[str, dict[str, Any]] | None = None,
        sub_session_id: str | None = None,
        orchestrator_config: dict[str, Any] | None = None,
        parent_messages: list[dict[str, Any]] | None = None,
        tool_inheritance: dict[str, list[str]] | None = None,
        hook_inheritance: dict[str, list[str]] | None = None,
        provider_preferences: list[Any] | None = None,
        self_delegation_depth: int = 0,
        **kwargs: Any,  # future-proof: accept new kwargs without crashing
    ) -> dict[str, Any]:
        """Spawn a sub-session for *agent_name* and execute *instruction*.

        Resolves the agent name to a Bundle config (checking *agent_configs*
        first, then ``prepared.bundle.agents``, with "self" as a special
        pass-through).  Delegates actual session creation and execution to
        ``PreparedBundle.spawn()``.

        Args:
            agent_name:           Agent identifier (or "self" to clone parent).
            instruction:          Task prompt for the sub-session.
            parent_session:       Parent AmplifierSession for lineage.
            agent_configs:        Per-agent config overrides from the bundle.
            sub_session_id:       Pre-generated session ID from tool-delegate.
            orchestrator_config:  Orchestrator config to inherit (e.g. rate
                                  limits).
            parent_messages:      Context messages from parent session.
            tool_inheritance:     Tool allow/blocklist policy (app-layer).
            hook_inheritance:     Hook allow/blocklist policy (app-layer).
            provider_preferences: Ordered provider/model preferences.
            self_delegation_depth: Current recursion depth for depth limiting.
            **kwargs:             Ignored; accepts future tool-delegate args.

        Returns:
            dict with at minimum ``{"response": str, "session_id": str}``.

        Raises:
            ValueError: If *agent_name* is not "self" and cannot be resolved.
        """
        configs = agent_configs or {}

        # --- Resolve agent name → Bundle config ----------------------------
        if agent_name == "self":
            # Clone the parent: spawn with no overrides so prepared.spawn
            # inherits providers/tools from the parent session.
            config: dict[str, Any] = {}
        elif agent_name in configs:
            config = configs[agent_name]
        elif hasattr(prepared, "bundle") and hasattr(prepared.bundle, "agents") \
                and agent_name in prepared.bundle.agents:
            config = prepared.bundle.agents[agent_name]
        else:
            available = sorted(
                list(configs.keys())
                + (list(prepared.bundle.agents.keys()) if hasattr(prepared, "bundle")
                   and hasattr(prepared.bundle, "agents") else [])
            )
            raise ValueError(
                f"Agent '{agent_name}' not found. Available: {available}"
            )

        # --- Build child Bundle from config --------------------------------
        from amplifier_foundation import Bundle  # type: ignore[import]

        child_bundle = Bundle(
            name=agent_name,
            version="1.0.0",
            session=config.get("session", {}),
            providers=config.get("providers", []),
            tools=config.get("tools", []),
            hooks=config.get("hooks", []),
            instruction=(
                config.get("instruction")
                or config.get("system", {}).get("instruction")
            ),
        )

        logger.debug(
            "Spawning sub-session: agent=%s session_id=%s parent=%s",
            agent_name,
            sub_session_id,
            session_id,
        )

        # --- Delegate to PreparedBundle.spawn() ----------------------------
        return await prepared.spawn(
            child_bundle=child_bundle,
            instruction=instruction,
            session_id=sub_session_id,
            parent_session=parent_session,
            orchestrator_config=orchestrator_config,
            parent_messages=parent_messages,
            provider_preferences=provider_preferences,
            self_delegation_depth=self_delegation_depth,
        )

    coordinator.register_capability("session.spawn", spawn_fn)
    logger.info("session.spawn capability registered for session %s", session_id)
```

**Step 4: Run tests**

```bash
cd distro-server && uv run pytest tests/test_spawn_registration.py -v
```
Expected: all 7 tests PASS.

**Step 5: Commit**

```bash
git add distro-server/src/amplifier_distro/server/spawn_registration.py \
        distro-server/tests/test_spawn_registration.py
git commit -m "feat: add spawn_registration module for session.spawn capability"
```

---

## Task 2: Wire `register_spawning` into `FoundationBackend`

**Files:**
- Modify: `distro-server/src/amplifier_distro/server/session_backend.py`
- Modify: `distro-server/tests/test_session_backend.py`

**Context:** `FoundationBackend.create_session()` (line 475) and `_reconnect()` (line 637) both load a bundle and create a session. Neither stores the `PreparedBundle` or calls `register_spawning`. We need to:
1. Add `prepared: Any = field(default=None)` to `_SessionHandle`
2. Store `prepared` on the handle in `create_session()` and `_reconnect()`
3. Call `register_spawning(session, handle.prepared, session_id)` in both paths

**Step 1: Write the failing test**

Add to `distro-server/tests/test_session_backend.py`:

```python
def test_create_session_registers_spawn_capability(foundation_backend, mock_session_factory):
    """FoundationBackend.create_session() registers session.spawn on the coordinator."""
    import asyncio
    session_info = asyncio.run(foundation_backend.create_session(working_dir="/tmp"))
    session = mock_session_factory.last_session
    session.coordinator.register_capability.assert_any_call(
        "session.spawn", unittest.mock.ANY
    )
```

Check the existing conftest/fixtures in `test_session_backend.py` to match the pattern already there.

**Step 2: Confirm test fails**

```bash
cd distro-server && uv run pytest tests/test_session_backend.py::test_create_session_registers_spawn_capability -v
```

**Step 3: Apply the changes to `session_backend.py`**

Three targeted edits:

**(a) Add `prepared` field to `_SessionHandle`:**
```python
# After: _cleanup_done: bool = field(default=False, repr=False)
prepared: Any = field(default=None, repr=False)
```

**(b) In `create_session()` — store prepared on handle and call register_spawning:**
```python
# Replace the handle construction block (around line 492-497):
handle = _SessionHandle(
    session_id=session_id,
    project_id=project_id,
    working_dir=wd,
    session=session,
    prepared=prepared,          # ← add this
)
self._sessions[session_id] = handle

# Wire persistence hooks
...  # (existing code unchanged)

# Wire streaming/display/approval when event_queue provided
if event_queue is not None:
    self._wire_event_queue(session, session_id, event_queue)

# Register session spawning capability
from amplifier_distro.server.spawn_registration import register_spawning
register_spawning(session, prepared, session_id)  # ← add this line
```

**(c) In `_reconnect()` — store prepared on handle and call register_spawning:**
```python
# Replace the handle construction block (around line 698-703):
handle = _SessionHandle(
    session_id=session_id,
    project_id=project_id,
    working_dir=wd,
    session=session,
    prepared=prepared,          # ← add this
)
self._sessions[session_id] = handle

# Wire persistence hooks on reconnect too
...  # (existing code unchanged)

# Register session spawning capability on reconnect
from amplifier_distro.server.spawn_registration import register_spawning
register_spawning(session, prepared, session_id)  # ← add this line
```

**Step 4: Run all session backend tests**

```bash
cd distro-server && uv run pytest tests/test_session_backend.py -v
```
Expected: all tests PASS (including new test).

**Step 5: Run full test suite**

```bash
cd distro-server && uv run pytest tests/ -x -q 2>&1 | tail -20
```
Expected: green.

**Step 6: Commit**

```bash
git add distro-server/src/amplifier_distro/server/session_backend.py \
        distro-server/tests/test_session_backend.py
git commit -m "feat: wire session.spawn capability into FoundationBackend"
```

---

## Task 3: Final verification

**Step 1: Run full test suite clean**

```bash
cd distro-server && uv run pytest tests/ -q 2>&1 | tail -10
```

**Step 2: Confirm capability is registered end-to-end**

```bash
cd distro-server && python3 -c "
from amplifier_distro.server.spawn_registration import register_spawning
from unittest.mock import MagicMock, AsyncMock
s = MagicMock()
p = MagicMock()
p.spawn = AsyncMock()
p.bundle.agents = {}
register_spawning(s, p, 'test')
print('session.spawn registered:', s.coordinator.register_capability.call_args[0][0])
"
```
Expected: `session.spawn registered: session.spawn`

**Step 3: Type-check the new file**

```bash
cd distro-server && uv run pyright src/amplifier_distro/server/spawn_registration.py 2>&1 | tail -5
```

**Step 4: Final commit if anything cleaned up**
