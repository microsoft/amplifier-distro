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
            orchestrator_config:  Orchestrator config to inherit (e.g. rate limits).
            parent_messages:      Context messages from parent session.
            tool_inheritance:     Tool allow/blocklist policy (app-layer, unused here).
            hook_inheritance:     Hook allow/blocklist policy (app-layer, unused here).
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
        elif (
            hasattr(prepared, "bundle")
            and hasattr(prepared.bundle, "agents")
            and agent_name in prepared.bundle.agents
        ):
            config = prepared.bundle.agents[agent_name]
        else:
            available = sorted(
                list(configs.keys())
                + (
                    list(prepared.bundle.agents.keys())
                    if hasattr(prepared, "bundle")
                    and hasattr(prepared.bundle, "agents")
                    else []
                )
            )
            raise ValueError(f"Agent '{agent_name}' not found. Available: {available}")

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
                config.get("instruction") or config.get("system", {}).get("instruction")
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
