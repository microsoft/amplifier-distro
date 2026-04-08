"""Provider management — catalog, key persistence, registration.

Ported from ``amplifier-distro/distro-server`` with adaptations for the
plugin architecture:

* Every public function takes ``settings: DistroPluginSettings`` so paths
  are fully determined by the caller (no global state).
* Module installation is **not** included — the distro daemon handles
  that separately.
* Overlay mutations delegate to :mod:`distro_plugin.overlay`.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from distro_plugin.config import DistroPluginSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    description: str
    include: str
    key_prefix: str
    env_var: str
    default_model: str
    module_id: str = ""
    source_url: str = ""
    console_url: str = ""
    fallback_models: tuple[str, ...] = ()
    auth_env_vars: tuple[
        str, ...
    ] = ()  # checked in priority order; non-empty only for keyless providers
    needs_key: bool = True


@dataclass
class ProviderRegistrationResult:
    """Outcome of a provider registration attempt."""

    provider_id: str
    provider_name: str
    default_model: str
    key_saved: bool = False
    settings_updated: bool = False
    overlay_updated: bool = False
    overlay_error: str = ""

    @property
    def ok(self) -> bool:
        """Registration succeeded — key + settings required, overlay optional for keyless."""
        return self.key_saved and self.settings_updated


# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------

_AMPLIFIER_START_URI = "git+https://github.com/microsoft/amplifier-bundle-distro@main"
_FOUNDATION_GIT_URI = "git+https://github.com/microsoft/amplifier-foundation@main"

_PRIMARY_PRIORITY = 1
_DEMOTED_PRIORITY = 10

PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        id="anthropic",
        name="Anthropic",
        description="Claude models (Sonnet, Opus, Haiku)",
        include=f"{_FOUNDATION_GIT_URI}#subdirectory=providers/anthropic-sonnet.yaml",
        key_prefix="sk-ant-",
        env_var="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-6",
        module_id="provider-anthropic",
        source_url="git+https://github.com/microsoft/amplifier-module-provider-anthropic@main",
        console_url="https://console.anthropic.com/settings/keys",
        fallback_models=(
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
        ),
    ),
    "openai": Provider(
        id="openai",
        name="OpenAI",
        description="GPT models",
        include=f"{_FOUNDATION_GIT_URI}#subdirectory=providers/openai-gpt.yaml",
        key_prefix="sk-",
        env_var="OPENAI_API_KEY",
        default_model="gpt-5.4",
        module_id="provider-openai",
        source_url="git+https://github.com/microsoft/amplifier-module-provider-openai@main",
        console_url="https://platform.openai.com/api-keys",
        fallback_models=("gpt-5.4", "gpt-5.2", "gpt-5-mini", "gpt-4.1"),
    ),
    "google": Provider(
        id="google",
        name="Google",
        description="Gemini models (Pro, Flash)",
        include=f"{_AMPLIFIER_START_URI}#subdirectory=providers/gemini-pro.yaml",
        key_prefix="AI",
        env_var="GOOGLE_API_KEY",
        default_model="gemini-3.1-pro-preview",
        module_id="provider-gemini",
        source_url="git+https://github.com/microsoft/amplifier-module-provider-gemini@main",
        console_url="https://aistudio.google.com/apikey",
        fallback_models=(
            "gemini-3.1-pro-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
        ),
    ),
    "ollama": Provider(
        id="ollama",
        name="Ollama",
        description="Local models via Ollama",
        include=f"{_AMPLIFIER_START_URI}#subdirectory=providers/ollama.yaml",
        key_prefix="",
        env_var="OLLAMA_HOST",
        default_model="llama3.1",
        module_id="provider-ollama",
        source_url="git+https://github.com/microsoft/amplifier-module-provider-ollama@main",
        console_url="https://ollama.com/",
        fallback_models=("llama3.1", "mistral", "codellama"),
    ),
    "azure": Provider(
        id="azure",
        name="Azure OpenAI",
        description="OpenAI models via Azure",
        include=f"{_AMPLIFIER_START_URI}#subdirectory=providers/azure-openai.yaml",
        key_prefix="",
        env_var="AZURE_OPENAI_API_KEY",
        default_model="gpt-5.2",
        module_id="provider-azure-openai",
        source_url="git+https://github.com/microsoft/amplifier-module-provider-azure-openai@main",
        console_url="https://portal.azure.com/",
        fallback_models=("gpt-5.2", "gpt-5-mini", "gpt-4.1"),
    ),
    "github-copilot": Provider(
        id="github-copilot",
        name="GitHub Copilot",
        description="Claude, GPT, and Gemini models via your GitHub Copilot subscription",
        include=f"{_AMPLIFIER_START_URI}#subdirectory=providers/github-copilot.yaml",
        key_prefix="",
        env_var="GITHUB_TOKEN",
        default_model="claude-sonnet-4.6",
        module_id="provider-github-copilot",
        source_url="git+https://github.com/microsoft/amplifier-module-provider-github-copilot@main",
        console_url="https://github.com/features/copilot",
        fallback_models=(
            "claude-sonnet-4.6",
            "claude-opus-4.6",
            "claude-haiku-4.5",
            "claude-sonnet-4.5",
            "claude-opus-4.5",
            "claude-sonnet-4",
            "gpt-5.4",
            "gpt-5.3-codex",
            "gpt-5.2-codex",
            "gpt-5.2",
            "gpt-5.1",
            "gpt-5-mini",
            "gpt-4.1",
        ),
        auth_env_vars=(
            "COPILOT_AGENT_TOKEN",
            "COPILOT_GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ),
        needs_key=False,
    ),
}


# ---------------------------------------------------------------------------
# Aliases & detection
# ---------------------------------------------------------------------------

PROVIDER_ALIASES: dict[str, str] = {
    "gemini": "google",
    "azure-openai": "azure",
}


def resolve_provider(name: str) -> str:
    """Resolve a provider name through aliases to its canonical key."""
    normalized = name.removeprefix("provider-")
    return PROVIDER_ALIASES.get(normalized, normalized)


def detect_provider(api_key: str) -> str | None:
    """Detect provider from API key format.

    Prefixes are checked in explicit order (``sk-ant-`` before ``sk-``)
    rather than derived from ``PROVIDERS[*].key_prefix`` because ordering
    matters for correct matching.
    """
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("sk-"):
        return "openai"
    if api_key.startswith("AI"):
        return "google"
    return None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _find_existing_entry(
    providers_list: list[dict[str, Any]], provider: Provider
) -> dict[str, Any] | None:
    """Find an existing provider entry, supporting both id-based and legacy module-based matching.

    New entries (written by this module) have an ``id`` field.  Legacy entries
    written by ``amplifier-app-cli`` only have a ``module`` field and no ``id``.
    Each provider module is unique, so a module-name match on legacy entries is
    sufficient.
    """
    for entry in providers_list:
        # New format: match by id (fast path, exact)
        if entry.get("id") == provider.id:
            return entry

        # Legacy format: no id field present — fall back to module matching
        if entry.get("id") is not None:
            continue
        if entry.get("module") != provider.module_id:
            continue

        # Module name is unique per provider — match is sufficient
        return entry

    return None


def _keys_path(settings: DistroPluginSettings) -> Path:
    return Path(settings.amplifier_home) / "keys.env"


def _settings_path(settings: DistroPluginSettings) -> Path:
    return Path(settings.distro_home) / "settings.yaml"


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def load_keys(settings: DistroPluginSettings) -> dict[str, str]:
    """Load keys.env if it exists, returning a dict of key=value pairs."""
    keys_path = _keys_path(settings)
    if not keys_path.exists():
        return {}
    result: dict[str, str] = {}
    try:
        for raw_line in keys_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key:
                result[key] = value
    except OSError:
        pass
    return result


def persist_api_key(
    settings: DistroPluginSettings, provider_id: str, api_key: str
) -> None:
    """Write an API key to keys.env (.env format, chmod 600).

    Existing keys are preserved; the target key is added or updated.

    .. warning::

       This function **mutates** ``os.environ`` as a side effect, setting
       the provider's env var in the running process.  Callers that need
       isolation (e.g. tests) should use ``monkeypatch`` or equivalent.
    """
    provider = PROVIDERS[provider_id]
    _write_raw_key(settings, provider.env_var, api_key)


def _write_raw_key(settings: DistroPluginSettings, key_name: str, value: str) -> None:
    """Write an arbitrary key=value to keys.env and set in os.environ.

    Unlike :func:`persist_api_key`, this does not require a provider ID —
    it writes a raw key name directly.  Used for ``GITHUB_TOKEN`` extracted
    from ``gh auth token``.
    """
    keys_path = _keys_path(settings)
    keys_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    found = False
    if keys_path.exists():
        for raw_line in keys_path.read_text().splitlines():
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                existing_key, _, _ = stripped.partition("=")
                if existing_key.strip() == key_name:
                    lines.append(f'{key_name}="{value}"')
                    found = True
                    continue
            lines.append(raw_line)

    if not found:
        lines.append(f'{key_name}="{value}"')

    keys_path.write_text("\n".join(lines) + "\n")
    with contextlib.suppress(OSError):
        keys_path.chmod(0o600)

    os.environ[key_name] = value


# ---------------------------------------------------------------------------
# Settings management
# ---------------------------------------------------------------------------


def add_provider_config(
    settings: DistroPluginSettings, provider_id: str, *, model: str = ""
) -> None:
    """Add a provider to config.providers[] in settings.yaml (additive).

    Skips if the provider module is already listed.  Stores API keys
    as ``${VAR}`` placeholders (never raw values).  Demotes any existing
    ``priority: 1`` provider to ``priority: 10`` before adding.

    Args:
        settings: Plugin settings with path configuration.
        provider_id: Canonical provider ID (e.g. ``"anthropic"``).
        model: Override the catalog default model.  When empty the
            provider's ``default_model`` is used.
    """
    provider = PROVIDERS[provider_id]
    settings_path = _settings_path(settings)
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if settings_path.exists():
        data = yaml.safe_load(settings_path.read_text()) or {}

    config = data.setdefault("config", {})
    providers_list: list[dict[str, Any]] = config.setdefault("providers", [])

    # Idempotency: skip if an equivalent entry already exists.
    # Supports both new-format entries (have an ``id`` field) and legacy
    # entries written by amplifier-app-cli (only have a ``module`` field).
    # When a matching legacy entry is found, its ``id`` field is stamped
    # in-place so future lookups use the fast id-based path.
    existing = _find_existing_entry(providers_list, provider)
    if existing is not None:
        if existing.get("id") is None:
            # Migrate legacy entry: stamp the canonical id for future idempotency
            existing["id"] = provider.id
            settings_path.write_text(
                yaml.dump(data, default_flow_style=False, sort_keys=False)
            )
        return

    effective_model = model if model else provider.default_model
    config_block: dict[str, Any] = {
        "default_model": effective_model,
        "priority": _PRIMARY_PRIORITY,
    }
    if provider.needs_key:
        config_block["api_key"] = f"${{{provider.env_var}}}"

    new_entry: dict[str, Any] = {
        "id": provider.id,
        "module": provider.module_id,
        "config": config_block,
    }
    if provider.source_url:
        new_entry["source"] = provider.source_url

    # Demote existing priority-1 providers to priority 10
    for existing in providers_list:
        existing_config = existing.get("config", {})
        if existing_config.get("priority") == _PRIMARY_PRIORITY:
            existing_config["priority"] = _DEMOTED_PRIORITY

    providers_list.append(new_entry)

    settings_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


# ---------------------------------------------------------------------------
# Registration & status
# ---------------------------------------------------------------------------


def register_provider(
    settings: DistroPluginSettings,
    provider_id: str,
    api_key: str,
    *,
    model: str = "",
    gh_token: str = "",
) -> ProviderRegistrationResult:
    """Register a provider: persist API key, update settings, update overlay.

    This is the single entry point for adding a provider to the distro.
    Module installation is handled separately by the daemon.

    Args:
        settings: Plugin settings with path configuration.
        provider_id: Canonical provider ID (e.g. ``"anthropic"``).
        api_key: API key to persist.  Pass an empty string for keyless
            providers (e.g. ``"github-copilot"``).
        model: Override the catalog default model written to settings.yaml.
        gh_token: For keyless providers: an optional GitHub token extracted
            from ``gh auth token`` to write to ``keys.env`` as
            ``GITHUB_TOKEN``.
    """
    from distro_plugin.overlay import add_include

    provider = PROVIDERS[provider_id]
    result = ProviderRegistrationResult(
        provider_id=provider_id,
        provider_name=provider.name,
        default_model=model if model else provider.default_model,
    )

    # Steps 1 and 2 propagate exceptions on failure — key/settings writes
    # are unrecoverable so we let the caller handle them.  Step 3 catches
    # OSError because overlay writes are recoverable (recorded in result).

    # 1. Write key to keys.env and set in current process env (keyless: skip or write gh_token)
    if provider.needs_key:
        persist_api_key(settings, provider_id, api_key)
    elif gh_token:
        _write_raw_key(settings, "GITHUB_TOKEN", gh_token)
    result.key_saved = True

    # 2. Add provider module config to settings.yaml
    add_provider_config(settings, provider_id, model=model)
    result.settings_updated = True

    # 3. Add provider include to overlay bundle (recoverable).
    #    add_include() is idempotent — safe to call for all providers.
    try:
        add_include(settings, provider.include)
        result.overlay_updated = True
    except OSError as exc:
        result.overlay_error = str(exc)

    return result


def update_provider_model(
    settings: DistroPluginSettings, provider_id: str, model: str
) -> bool:
    """Update the default_model for an already-configured provider in settings.yaml.

    Returns ``True`` if the model was updated, ``False`` if the provider
    is not found in the catalog or not configured in settings.
    """
    provider = PROVIDERS.get(provider_id)
    if not provider:
        return False

    settings_path = _settings_path(settings)
    if not settings_path.exists():
        return False

    data = yaml.safe_load(settings_path.read_text()) or {}
    providers_list = data.get("config", {}).get("providers", [])

    entry = _find_existing_entry(providers_list, provider)
    if entry is None:
        return False

    entry.setdefault("config", {})["default_model"] = model
    settings_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return True


def check_provider_status(
    settings: DistroPluginSettings, provider_id: str
) -> dict[str, bool]:
    """Check whether a provider is fully configured across all three sources.

    Returns a dict with:
        has_key      — API key in ``os.environ`` or ``keys.env``
        in_settings  — provider module listed in ``settings.yaml``
        in_overlay   — provider include URI in overlay ``bundle.yaml``
        configured   — all three are ``True``
    """
    from distro_plugin.overlay import get_includes

    provider = PROVIDERS[provider_id]

    # 1. Key in env or keys.env (keyless providers always have_key=True)
    if not provider.needs_key:
        has_key = True
    else:
        keys = load_keys(settings)
        has_key = bool(os.environ.get(provider.env_var) or keys.get(provider.env_var))

    # 2. Provider module listed in settings.yaml
    in_settings = False
    settings_path = _settings_path(settings)
    if settings_path.exists():
        try:
            data = yaml.safe_load(settings_path.read_text()) or {}
            providers_list = data.get("config", {}).get("providers", [])
            in_settings = _find_existing_entry(providers_list, provider) is not None
        except (yaml.YAMLError, OSError):
            pass

    # 3. Provider include URI in overlay bundle.yaml
    current_uris = set(get_includes(settings))
    in_overlay = provider.include in current_uris

    return {
        "has_key": has_key,
        "in_settings": in_settings,
        "in_overlay": in_overlay,
        "configured": (
            has_key and in_settings and in_overlay
            if provider.needs_key
            else has_key and in_settings
        ),
    }


def get_provider_catalog(
    settings: DistroPluginSettings,
) -> list[dict[str, object]]:
    """Build the full provider catalog with configuration status."""
    providers: list[dict[str, object]] = []
    for pid, p in PROVIDERS.items():
        status = check_provider_status(settings, pid)
        providers.append(
            {
                "id": pid,
                "name": p.name,
                "description": p.description,
                "console_url": p.console_url,
                "key_prefix": p.key_prefix,
                "needs_key": p.needs_key,
                "default_model": p.default_model,
                "fallback_models": list(p.fallback_models),
                "has_key": status["has_key"],
                "in_settings": status["in_settings"],
                "in_overlay": status["in_overlay"],
                "configured": status["configured"],
                "token_detected": (
                    bool(any(os.environ.get(v) for v in p.auth_env_vars))
                    if not p.needs_key and p.auth_env_vars
                    else status["has_key"]
                ),
            }
        )
    return providers


def _build_result(reg: ProviderRegistrationResult) -> dict[str, object]:
    """Build a success result dict from a registration outcome."""
    result: dict[str, object] = {
        "status": "ok",
        "verified": True,
        "provider": reg.provider_id,
        "provider_name": reg.provider_name,
        "model": reg.default_model,
    }
    if reg.overlay_error:
        result["overlay_error"] = reg.overlay_error
    return result


def handle_provider_request(
    settings: DistroPluginSettings,
    *,
    provider: str = "",
    api_key: str = "",
    model: str = "",
    gh_token: str = "",
) -> dict[str, object]:
    """Shared handler for provider add/update requests.

    Supports three modes:

    - **Explicit key**: *api_key* provided — detect provider and register.
    - **Keyless**: *provider* is a keyless provider (e.g. ``github-copilot``) —
      register directly without requiring a key, optionally accepting *model*
      and *gh_token*.
    - **Use existing key**: *provider* set, no *api_key* — look up key from
      environment or ``keys.env`` and register.
    """
    if api_key.strip():
        provider_id = detect_provider(api_key) or provider
        if not provider_id or provider_id not in PROVIDERS:
            return {"status": "error", "detail": "Unknown provider or key format"}
        return _build_result(
            register_provider(settings, provider_id, api_key, model=model)
        )

    if provider and provider in PROVIDERS:
        prov = PROVIDERS[provider]
        if not prov.needs_key:
            return _build_result(
                register_provider(
                    settings, provider, "", model=model, gh_token=gh_token
                )
            )
        key = os.environ.get(prov.env_var) or load_keys(settings).get(prov.env_var)
        if not key:
            return {
                "status": "error",
                "detail": f"No key found for {prov.name} in environment or keys.env",
            }
        return _build_result(register_provider(settings, provider, key, model=model))

    return {"status": "error", "detail": "Provide api_key or provider ID"}


def sync_providers(
    settings: DistroPluginSettings,
) -> list[ProviderRegistrationResult]:
    """Auto-register providers that have keys but aren't fully configured.

    Scans ``os.environ`` and ``keys.env`` for known provider keys.
    For each provider with a key but missing config, calls
    :func:`register_provider` to complete the setup.
    """
    keys = load_keys(settings)
    results: list[ProviderRegistrationResult] = []

    for pid, provider in PROVIDERS.items():
        if not provider.needs_key:
            continue
        key = os.environ.get(provider.env_var) or keys.get(provider.env_var)
        if not key:
            continue
        status = check_provider_status(settings, pid)
        if not status["configured"]:
            reg = register_provider(settings, pid, key)
            results.append(reg)

    return results
