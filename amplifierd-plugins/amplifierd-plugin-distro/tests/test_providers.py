"""Tests for distro_plugin.providers — catalog, key management, registration."""

from __future__ import annotations

import os
import stat

import yaml

from distro_plugin.providers import (
    PROVIDERS,
    Provider,
    ProviderRegistrationResult,
    _keys_path,
    _settings_path,
    add_provider_config,
    check_provider_status,
    detect_provider,
    get_provider_catalog,
    handle_provider_request,
    load_keys,
    persist_api_key,
    register_provider,
    resolve_provider,
    sync_providers,
)


# -- PROVIDERS catalog -------------------------------------------------------


def test_catalog_has_entries():
    """PROVIDERS catalog contains all 6 expected providers."""
    assert len(PROVIDERS) == 6
    for pid in ("anthropic", "openai", "google", "ollama", "azure", "github-copilot"):
        assert pid in PROVIDERS


def test_provider_has_required_fields():
    """Each Provider has name, env_var, and include fields."""
    for pid, provider in PROVIDERS.items():
        assert isinstance(provider, Provider)
        assert isinstance(provider.name, str) and provider.name
        assert isinstance(provider.env_var, str) and provider.env_var
        assert isinstance(provider.include, str) and provider.include


# -- detect_provider ---------------------------------------------------------


def test_detect_provider_known_prefixes():
    """detect_provider identifies anthropic, openai, and google by key prefix."""
    assert detect_provider("sk-ant-abc123") == "anthropic"
    assert detect_provider("sk-proj-abc123") == "openai"
    assert detect_provider("AIzaSyAbc123") == "google"


def test_detect_provider_unknown_returns_none():
    """detect_provider returns None for unrecognised key formats."""
    assert detect_provider("unknown-key-format") is None


# -- resolve_provider --------------------------------------------------------


def test_resolve_provider_aliases():
    """resolve_provider maps aliases to canonical provider IDs."""
    assert resolve_provider("gemini") == "google"
    assert resolve_provider("azure-openai") == "azure"
    assert resolve_provider("provider-anthropic") == "anthropic"


# -- load_keys / persist_api_key ---------------------------------------------


def test_load_keys_returns_empty_when_missing(settings):
    """load_keys returns {} when keys.env does not exist."""
    assert load_keys(settings) == {}


def test_persist_api_key_writes_to_keys_env(settings, monkeypatch):
    """persist_api_key creates keys.env with the provider's env var."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    persist_api_key(settings, "anthropic", "sk-ant-test123")
    content = _keys_path(settings).read_text()
    assert 'ANTHROPIC_API_KEY="sk-ant-test123"' in content


def test_persist_api_key_sets_env_var(settings, monkeypatch):
    """persist_api_key also sets the key in os.environ."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    persist_api_key(settings, "anthropic", "sk-ant-envtest")
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-envtest"


def test_persist_api_key_updates_existing(settings, monkeypatch):
    """persist_api_key updates an existing key in keys.env."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    persist_api_key(settings, "anthropic", "sk-ant-old")
    persist_api_key(settings, "anthropic", "sk-ant-new")
    keys = load_keys(settings)
    assert keys["ANTHROPIC_API_KEY"] == "sk-ant-new"
    # Should appear only once
    lines = _keys_path(settings).read_text().splitlines()
    key_lines = [ln for ln in lines if ln.startswith("ANTHROPIC_API_KEY")]
    assert len(key_lines) == 1


def test_persist_api_key_preserves_other_keys(settings, monkeypatch):
    """persist_api_key does not clobber unrelated keys in keys.env."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    persist_api_key(settings, "anthropic", "sk-ant-first")
    persist_api_key(settings, "openai", "sk-proj-second")
    keys = load_keys(settings)
    assert keys["ANTHROPIC_API_KEY"] == "sk-ant-first"
    assert keys["OPENAI_API_KEY"] == "sk-proj-second"


def test_persist_api_key_sets_chmod_600(settings, monkeypatch):
    """persist_api_key sets file permissions to 600 on keys.env."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    persist_api_key(settings, "anthropic", "sk-ant-perm")
    mode = _keys_path(settings).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_load_keys_strips_quotes(settings):
    """load_keys strips surrounding double and single quotes from values."""
    path = _keys_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("FOO=\"bar\"\nBAZ='qux'\nPLAIN=hello\n")
    keys = load_keys(settings)
    assert keys["FOO"] == "bar"
    assert keys["BAZ"] == "qux"
    assert keys["PLAIN"] == "hello"


# -- add_provider_config -----------------------------------------------------


def test_add_provider_config_writes_settings_yaml(settings):
    """add_provider_config creates settings.yaml with provider entry."""
    add_provider_config(settings, "anthropic")
    data = yaml.safe_load(_settings_path(settings).read_text())
    providers_list = data["config"]["providers"]
    assert len(providers_list) == 1
    entry = providers_list[0]
    assert entry["module"] == "provider-anthropic"
    assert entry["config"]["priority"] == 1


def test_add_provider_config_is_idempotent(settings):
    """add_provider_config does not duplicate an existing provider entry."""
    add_provider_config(settings, "anthropic")
    add_provider_config(settings, "anthropic")
    data = yaml.safe_load(_settings_path(settings).read_text())
    providers_list = data["config"]["providers"]
    assert len(providers_list) == 1


def test_add_provider_config_demotes_existing_priority(settings):
    """add_provider_config demotes existing priority-1 providers to 10."""
    add_provider_config(settings, "anthropic")
    add_provider_config(settings, "openai")
    data = yaml.safe_load(_settings_path(settings).read_text())
    providers_list = data["config"]["providers"]
    # Anthropic was demoted, OpenAI is priority 1
    anthropic_entry = next(
        e for e in providers_list if e["module"] == "provider-anthropic"
    )
    openai_entry = next(e for e in providers_list if e["module"] == "provider-openai")
    assert anthropic_entry["config"]["priority"] == 10
    assert openai_entry["config"]["priority"] == 1


# -- register_provider -------------------------------------------------------


def test_register_provider_full_orchestration(settings, monkeypatch):
    """register_provider writes key, settings, and overlay in one call."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = register_provider(settings, "anthropic", "sk-ant-orch")
    assert isinstance(result, ProviderRegistrationResult)
    assert result.ok is True
    assert result.key_saved is True
    assert result.settings_updated is True
    assert result.overlay_updated is True

    # Key written
    keys = load_keys(settings)
    assert keys["ANTHROPIC_API_KEY"] == "sk-ant-orch"

    # Settings written
    data = yaml.safe_load(_settings_path(settings).read_text())
    modules = [e["module"] for e in data["config"]["providers"]]
    assert "provider-anthropic" in modules

    # Overlay updated
    from distro_plugin.overlay import get_includes

    includes = get_includes(settings)
    assert PROVIDERS["anthropic"].include in includes


# -- get_provider_catalog ----------------------------------------------------


def test_get_provider_catalog_returns_all_providers_with_status(settings):
    """get_provider_catalog returns entries for all 6 providers with status."""
    catalog = get_provider_catalog(settings)
    assert len(catalog) == 6
    ids = {entry["id"] for entry in catalog}
    assert ids == {"anthropic", "openai", "google", "ollama", "azure", "github-copilot"}
    for entry in catalog:
        assert "has_key" in entry
        assert "in_settings" in entry
        assert "in_overlay" in entry
        assert "configured" in entry


def test_github_copilot_in_catalog():
    """github-copilot provider exists in PROVIDERS with correct attributes."""
    assert "github-copilot" in PROVIDERS
    copilot = PROVIDERS["github-copilot"]
    assert copilot.needs_key is False
    assert copilot.module_id == "provider-github-copilot"
    assert copilot.env_var == "GITHUB_TOKEN"
    assert copilot.default_model == "claude-sonnet-4.6"
    assert len(copilot.fallback_models) == 13


def test_needs_key_defaults_true():
    """All standard API-key providers have needs_key=True."""
    for pid in ("anthropic", "openai", "google", "ollama", "azure"):
        assert PROVIDERS[pid].needs_key is True, f"{pid}.needs_key should be True"


def test_fallback_models_populated_for_all_providers():
    """Every provider in the catalog has at least one fallback model."""
    for pid, provider in PROVIDERS.items():
        assert len(provider.fallback_models) > 0, f"{pid} has no fallback_models"


def test_provider_defaults_updated():
    """default_model matches design table for all 6 providers."""
    expected = {
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-5.4",
        "google": "gemini-3.1-pro-preview",
        "ollama": "llama3.1",
        "azure": "gpt-5.2",
        "github-copilot": "claude-sonnet-4.6",
    }
    for pid, model in expected.items():
        assert PROVIDERS[pid].default_model == model, (
            f"{pid}.default_model expected {model!r}, got {PROVIDERS[pid].default_model!r}"
        )


# -- handle_provider_request -------------------------------------------------


def test_handle_provider_request_with_explicit_key(settings, monkeypatch):
    """handle_provider_request with an explicit key detects and registers."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = handle_provider_request(settings, api_key="sk-ant-handle")
    assert result["status"] == "ok"
    assert result["provider"] == "anthropic"


def test_handle_provider_request_unknown_key(settings):
    """handle_provider_request returns error for unrecognised key format."""
    result = handle_provider_request(settings, api_key="unknown-format-key")
    assert result["status"] == "error"


# -- check_provider_status ---------------------------------------------------


def test_check_provider_status_unconfigured(settings, monkeypatch):
    """check_provider_status returns all False when nothing is configured."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    status = check_provider_status(settings, "anthropic")
    assert status["has_key"] is False
    assert status["in_settings"] is False
    assert status["in_overlay"] is False
    assert status["configured"] is False


def test_check_provider_status_fully_configured(settings, monkeypatch):
    """check_provider_status returns all True after full registration."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    register_provider(settings, "anthropic", "sk-ant-status")
    status = check_provider_status(settings, "anthropic")
    assert status["has_key"] is True
    assert status["in_settings"] is True
    assert status["in_overlay"] is True
    assert status["configured"] is True


# -- sync_providers ----------------------------------------------------------


def test_add_provider_config_writes_id_field(settings):
    """add_provider_config writes the provider catalog id to each entry."""
    add_provider_config(settings, "anthropic")
    data = yaml.safe_load(_settings_path(settings).read_text())
    entry = data["config"]["providers"][0]
    assert entry["id"] == "anthropic"


def test_add_provider_config_does_not_write_base_url(settings):
    """add_provider_config never writes a base_url field."""
    add_provider_config(settings, "anthropic")
    data = yaml.safe_load(_settings_path(settings).read_text())
    entry = data["config"]["providers"][0]
    assert "base_url" not in entry["config"]


def test_sync_providers_registers_incomplete(settings, monkeypatch):
    """sync_providers auto-registers providers with keys but incomplete config."""
    # Set up a key in env without settings or overlay
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-sync")
    results = sync_providers(settings)
    assert len(results) >= 1
    anthropic_result = next(r for r in results if r.provider_id == "anthropic")
    assert anthropic_result.ok is True


# -- Legacy settings.yaml compatibility (#229) --------------------------------


class TestLegacySettingsCompat:
    """Regression tests for #229: legacy settings.yaml entries without 'id' field.

    amplifier-app-cli writes entries that only have ``module`` + ``config``,
    not the ``id`` field that this plugin writes.  These tests ensure the
    distro plugin correctly detects, skips-duplication-of, and migrates
    such legacy entries.
    """

    def test_add_provider_config_idempotent_legacy_no_id_field(self, settings):
        """Legacy entries without 'id' should not be duplicated."""
        settings_path = settings.distro_home / "settings.yaml"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            yaml.dump(
                {
                    "config": {
                        "providers": [
                            {
                                "module": "provider-anthropic",
                                "config": {
                                    "priority": 1,
                                    "api_key": "${ANTHROPIC_API_KEY}",
                                },
                                "source": "git+https://example.com",
                            }
                        ]
                    }
                }
            )
        )
        add_provider_config(settings, "anthropic")
        data = yaml.safe_load(settings_path.read_text())
        assert len(data["config"]["providers"]) == 1
        # Should have migrated the id field in-place
        assert data["config"]["providers"][0]["id"] == "anthropic"

    def test_add_provider_config_legacy_does_not_demote_priority(self, settings):
        """When a legacy entry matches, its priority should NOT be demoted."""
        settings_path = settings.distro_home / "settings.yaml"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            yaml.dump(
                {
                    "config": {
                        "providers": [
                            {
                                "module": "provider-anthropic",
                                "config": {
                                    "priority": 1,
                                    "api_key": "${ANTHROPIC_API_KEY}",
                                },
                            }
                        ]
                    }
                }
            )
        )
        add_provider_config(settings, "anthropic")
        data = yaml.safe_load(settings_path.read_text())
        assert data["config"]["providers"][0]["config"]["priority"] == 1

    def test_add_provider_config_legacy_preserves_custom_fields(self, settings):
        """Legacy entries with custom config fields should be preserved, not overwritten."""
        settings_path = settings.distro_home / "settings.yaml"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            yaml.dump(
                {
                    "config": {
                        "providers": [
                            {
                                "module": "provider-anthropic",
                                "config": {
                                    "priority": 1,
                                    "api_key": "${ANTHROPIC_API_KEY}",
                                    "base_url": "${ANTHROPIC_BASE_URL}",
                                    "default_model": "claude-opus-4-6",
                                    "enable_1m_context": "true",
                                    "enable_prompt_caching": "true",
                                },
                                "source": "git+https://example.com",
                            }
                        ]
                    }
                }
            )
        )
        add_provider_config(settings, "anthropic")
        data = yaml.safe_load(settings_path.read_text())
        entry = data["config"]["providers"][0]
        # One entry — no duplicate
        assert len(data["config"]["providers"]) == 1
        # Custom fields must survive
        assert entry["config"]["enable_1m_context"] == "true"
        assert entry["config"]["enable_prompt_caching"] == "true"
        assert entry["config"]["default_model"] == "claude-opus-4-6"

    def test_check_provider_status_finds_legacy_entry(self, settings):
        """check_provider_status should detect legacy entries without 'id'."""
        settings_path = settings.distro_home / "settings.yaml"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            yaml.dump(
                {
                    "config": {
                        "providers": [
                            {
                                "module": "provider-anthropic",
                                "config": {"priority": 1},
                            }
                        ]
                    }
                }
            )
        )
        status = check_provider_status(settings, "anthropic")
        assert status["in_settings"] is True

    def test_sync_providers_does_not_duplicate_legacy(self, settings, monkeypatch):
        """sync_providers should not re-register providers present in legacy format."""
        settings_path = settings.distro_home / "settings.yaml"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            yaml.dump(
                {
                    "config": {
                        "providers": [
                            {
                                "module": "provider-anthropic",
                                "config": {
                                    "priority": 1,
                                    "api_key": "${ANTHROPIC_API_KEY}",
                                },
                            }
                        ]
                    }
                }
            )
        )
        # Set API key in both keys.env and os.environ
        keys_path = settings.amplifier_home / "keys.env"
        keys_path.parent.mkdir(parents=True, exist_ok=True)
        keys_path.write_text('ANTHROPIC_API_KEY="sk-ant-test123"\n')
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test123")

        # Set up overlay so that in_overlay=True
        overlay_path = settings.distro_home / "bundle" / "bundle.yaml"
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text(
            yaml.dump(
                {
                    "bundle": {"name": "amplifier-distro", "version": "0.1.0"},
                    "includes": [{"bundle": PROVIDERS["anthropic"].include}],
                }
            )
        )

        results = sync_providers(settings)

        # anthropic should NOT be in the results (already fully configured)
        assert not any(r.provider_id == "anthropic" for r in results)
        # The legacy anthropic entry should appear exactly once (no duplication)
        data = yaml.safe_load(settings_path.read_text())
        anthropic_entries = [
            e
            for e in data["config"]["providers"]
            if e.get("module") == "provider-anthropic" or e.get("id") == "anthropic"
        ]
        assert len(anthropic_entries) == 1


def test_add_provider_config_uses_model_override(settings):
    """add_provider_config with model param writes that model instead of catalog default."""
    add_provider_config(settings, "anthropic", model="claude-opus-4-6")
    data = yaml.safe_load(_settings_path(settings).read_text())
    entry = data["config"]["providers"][0]
    assert entry["config"]["default_model"] == "claude-opus-4-6"


def test_add_provider_config_omits_api_key_for_keyless(settings):
    """settings.yaml entry for GitHub Copilot has no api_key field."""
    add_provider_config(settings, "github-copilot")
    data = yaml.safe_load(_settings_path(settings).read_text())
    entry = next(e for e in data["config"]["providers"] if e["id"] == "github-copilot")
    assert "api_key" not in entry["config"]
    assert entry["config"]["default_model"] == "claude-sonnet-4.6"


def test_register_with_model_override(settings, monkeypatch):
    """register_provider with model param writes that model to settings.yaml."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = register_provider(
        settings, "anthropic", "sk-ant-test", model="claude-opus-4-6"
    )
    assert result.ok is True
    data = yaml.safe_load(_settings_path(settings).read_text())
    entry = next(e for e in data["config"]["providers"] if e["id"] == "anthropic")
    assert entry["config"]["default_model"] == "claude-opus-4-6"


def test_register_github_copilot_skips_keys_env(settings, monkeypatch):
    """register_provider for a keyless provider skips keys.env entirely."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = register_provider(settings, "github-copilot", "")
    assert result.key_saved is True  # "no key needed" still counts as ok
    assert result.settings_updated is True
    keys_path = _keys_path(settings)
    assert not keys_path.exists() or "GITHUB_TOKEN" not in keys_path.read_text()


def test_register_github_copilot_writes_gh_token_when_provided(settings, monkeypatch):
    """When gh_token is provided, register_provider writes GITHUB_TOKEN to keys.env."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = register_provider(
        settings, "github-copilot", "", gh_token="gho_extracted_token_123"
    )
    assert result.ok is True
    keys_path = _keys_path(settings)
    assert keys_path.exists()
    content = keys_path.read_text()
    assert "GITHUB_TOKEN" in content
    assert "gho_extracted_token_123" in content


def test_handle_provider_request_activates_keyless(settings, monkeypatch):
    """Sending provider='github-copilot' with no api_key activates it directly."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = handle_provider_request(settings, provider="github-copilot", api_key="")
    assert result["status"] == "ok"
    assert result["provider"] == "github-copilot"


def test_handle_provider_request_with_model_and_gh_token(settings, monkeypatch):
    """handle_provider_request passes model and gh_token through to registration."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    result = handle_provider_request(
        settings,
        provider="github-copilot",
        api_key="",
        model="gpt-5.4",
        gh_token="gho_test_token",
    )
    assert result["status"] == "ok"
    # Verify model was written to settings.yaml
    data = yaml.safe_load(_settings_path(settings).read_text())
    entry = next(e for e in data["config"]["providers"] if e["id"] == "github-copilot")
    assert entry["config"]["default_model"] == "gpt-5.4"
    # Verify gh_token was written to keys.env
    keys = load_keys(settings)
    assert keys.get("GITHUB_TOKEN") == "gho_test_token"


def test_sync_providers_does_not_autoregister_keyless(settings, monkeypatch):
    """sync_providers() does not auto-register keyless providers."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken123")
    results = sync_providers(settings)
    assert not any(r.provider_id == "github-copilot" for r in results)


def test_check_provider_status_github_copilot_has_key_without_env(
    settings, monkeypatch
):
    """Keyless providers always report has_key=True regardless of env vars."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_AGENT_TOKEN", raising=False)
    status = check_provider_status(settings, "github-copilot")
    assert status["has_key"] is True
    assert status["in_settings"] is False
    assert status["configured"] is False


def test_check_provider_status_github_copilot_configured(settings, monkeypatch):
    """Fully registered GitHub Copilot shows configured=True (no overlay needed)."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    register_provider(settings, "github-copilot", "")
    status = check_provider_status(settings, "github-copilot")
    assert status["has_key"] is True
    assert status["in_settings"] is True
    assert status["in_overlay"] is False  # keyless providers skip overlay
    assert (
        status["configured"] is True
    )  # configured = has_key + in_settings for keyless


def test_get_provider_catalog_includes_needs_key_and_fallback_models(settings):
    """Catalog response includes needs_key, default_model, and fallback_models for all providers."""
    catalog = get_provider_catalog(settings)
    for entry in catalog:
        assert "needs_key" in entry, f"{entry['id']} missing needs_key"
        assert "default_model" in entry, f"{entry['id']} missing default_model"
        assert "fallback_models" in entry, f"{entry['id']} missing fallback_models"

    copilot = next(p for p in catalog if p["id"] == "github-copilot")
    assert copilot["needs_key"] is False
    assert copilot["default_model"] == "claude-sonnet-4.6"
    assert len(copilot["fallback_models"]) == 13

    anthropic = next(p for p in catalog if p["id"] == "anthropic")
    assert anthropic["needs_key"] is True
    assert anthropic["default_model"] == "claude-sonnet-4-6"


# -- Provider settings path separation (#new) --------------------------------


def test_settings_path_uses_distro_home(settings):
    """_settings_path returns distro_home/settings.yaml, not amplifier_home/settings.yaml."""
    expected = settings.distro_home / "settings.yaml"
    assert _settings_path(settings) == expected


def test_add_provider_config_writes_to_distro_home(settings):
    """add_provider_config creates settings.yaml under distro_home, never amplifier_home."""
    add_provider_config(settings, "anthropic")
    assert (settings.distro_home / "settings.yaml").exists()
    assert not (settings.amplifier_home / "settings.yaml").exists()
