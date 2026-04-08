"""Tests for POST /distro/setup/steps/* wizard endpoints."""

from __future__ import annotations

from distro_plugin.distro_settings import load as load_distro_settings
from distro_plugin.features import FEATURES, get_enabled_features
from distro_plugin.overlay import add_include


def test_step_welcome_saves_identity_data(settings, client):
    """POST /distro/setup/steps/welcome saves workspace_root, github_handle, git_email."""
    resp = client.post(
        "/distro/setup/steps/welcome",
        json={
            "workspace_root": "/home/user/projects",
            "github_handle": "testuser",
            "git_email": "test@example.com",
        },
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data.get("status") == "ok"

    # Verify data was persisted via distro_settings
    ds = load_distro_settings(settings)
    assert ds.workspace_root == "/home/user/projects"
    assert ds.identity.github_handle == "testuser"
    assert ds.identity.git_email == "test@example.com"


def test_step_config_returns_ok(client):
    """POST /distro/setup/steps/config returns ok (passthrough)."""
    resp = client.post("/distro/setup/steps/config", json={})
    assert resp.status_code == 200

    data = resp.json()
    assert data.get("status") == "ok"


def test_step_modules_enables_requested_disables_unrequested(settings, client):
    """POST /distro/setup/steps/modules enables requested and disables unrequested."""
    # Pre-enable two features so we can verify disable behavior
    for inc in FEATURES["dev-memory"].includes:
        add_include(settings, inc)
    for inc in FEATURES["deliberate-dev"].includes:
        add_include(settings, inc)

    # Request only dev-memory; deliberate-dev should get disabled
    resp = client.post(
        "/distro/setup/steps/modules",
        json={"modules": ["dev-memory"]},
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data.get("status") == "ok"

    enabled = get_enabled_features(settings)
    assert "dev-memory" in enabled
    assert "deliberate-dev" not in enabled


def test_step_interfaces_skip_returns_ok(client, monkeypatch):
    """POST /distro/setup/steps/interfaces with no installs requested returns ok with install flags."""
    import shutil as _shutil

    # Mock shutil.which so the test is environment-independent: tools not on PATH
    monkeypatch.setattr(_shutil, "which", lambda name: None)

    resp = client.post(
        "/distro/setup/steps/interfaces",
        json={"install_cli": False, "install_tui": False},
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data.get("status") == "ok"
    assert data["cli_installed"] is False
    assert data["tui_installed"] is False


def test_step_provider_with_key_registers(settings, client, monkeypatch):
    """POST /distro/setup/steps/provider with a key registers the provider."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    resp = client.post(
        "/distro/setup/steps/provider",
        json={"provider": "anthropic", "api_key": "sk-ant-test-key-12345"},
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data.get("status") == "ok"
    assert data.get("provider") == "anthropic"


def test_step_provider_empty_body_returns_ok_without_sync(
    settings, client, monkeypatch
):
    """POST /distro/setup/steps/provider with empty provider+key returns synced=0."""
    # Verify that sync_providers is never called from routes (it's not even imported there)
    # by confirming the response carries synced=0 regardless of env state.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-key-in-env")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-key-in-env")

    resp = client.post(
        "/distro/setup/steps/provider",
        json={"provider": "", "api_key": ""},
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data.get("status") == "ok"
    assert data.get("synced") == 0, (
        "synced must be 0 — no auto-registration on empty body"
    )


def test_step_provider_empty_body_does_not_call_handle_provider_request(
    settings, client, monkeypatch
):
    """POST /distro/setup/steps/provider with empty body does not register any provider."""
    register_called = []
    monkeypatch.setattr(
        "distro_plugin.routes.handle_provider_request",
        lambda *a, **kw: (register_called.append((a, kw)), {})[1],
    )

    resp = client.post(
        "/distro/setup/steps/provider",
        json={"provider": "", "api_key": ""},
    )
    assert resp.status_code == 200
    assert resp.json().get("synced") == 0
    assert len(register_called) == 0, (
        "handle_provider_request must NOT be called on empty body"
    )


def test_step_verify_returns_status(settings, client):
    """POST /distro/setup/steps/verify returns phase, ready, overlay_exists."""
    resp = client.post("/distro/setup/steps/verify", json={})
    assert resp.status_code == 200

    data = resp.json()
    assert "phase" in data
    assert "ready" in data
    assert "overlay_exists" in data
    # Default: unconfigured, not ready, no overlay
    assert data["phase"] == "unconfigured"
    assert data["ready"] is False
    assert data["overlay_exists"] is False


# ---------------------------------------------------------------------------
# RED tests for Defect 1a: step_verify returns all 8 fields
# ---------------------------------------------------------------------------


def test_step_verify_returns_all_fields(settings, client):
    """Defect 1a: POST /distro/setup/steps/verify must return all 8 fields the wizard HTML expects."""
    resp = client.post("/distro/setup/steps/verify", json={})
    assert resp.status_code == 200

    data = resp.json()
    # status field must be present
    assert data.get("status") == "ok"
    # already-present fields
    assert "phase" in data
    assert "ready" in data
    assert "overlay_exists" in data
    # missing fields that wizard HTML reads
    assert "workspace_root" in data, "wizard reads data.workspace_root"
    assert "github_handle" in data, "wizard reads data.github_handle"
    assert "has_api_key" in data, "wizard reads data.has_api_key"
    assert "cli_installed" in data, "wizard reads data.cli_installed"
    assert "tui_installed" in data, "wizard reads data.tui_installed"


# ---------------------------------------------------------------------------
# RED tests for Defect 1b: step_welcome must not erase identity on empty fields
# ---------------------------------------------------------------------------


def test_step_welcome_empty_fields_do_not_erase_identity(settings, client):
    """Defect 1b: POST /distro/setup/steps/welcome with empty handle/email must preserve existing values."""
    from distro_plugin.distro_settings import update as update_distro_settings

    # Pre-populate identity in distro settings
    update_distro_settings(
        settings,
        section="identity",
        github_handle="existing-user",
        git_email="old@example.com",
    )

    # POST with all empty identity fields (but a valid workspace_root)
    resp = client.post(
        "/distro/setup/steps/welcome",
        json={
            "workspace_root": "/home/user/new-workspace",
            "github_handle": "",
            "git_email": "",
        },
    )
    assert resp.status_code == 200

    ds = load_distro_settings(settings)
    # Identity must NOT have been erased
    assert ds.identity.github_handle == "existing-user", (
        "github_handle was erased by empty string"
    )
    assert ds.identity.git_email == "old@example.com", (
        "git_email was erased by empty string"
    )
    # workspace_root was non-empty so it should have been updated
    assert ds.workspace_root == "/home/user/new-workspace"


# ---------------------------------------------------------------------------
# RED tests for Defect 2a: get_detect missing flat convenience fields
# ---------------------------------------------------------------------------


def test_get_detect_includes_flat_convenience_fields(settings, client):
    """Defect 2a: GET /distro/detect must return workspace_root, github_handle, cli_installed, tui_installed."""
    resp = client.get("/distro/detect")
    assert resp.status_code == 200

    data = resp.json()
    assert "workspace_root" in data, "wizard JS reads data.workspace_root"
    assert "github_handle" in data, (
        "wizard JS reads data.github_handle (not github_user)"
    )
    assert "cli_installed" in data, "wizard JS reads data.cli_installed"
    assert "tui_installed" in data, "wizard JS reads data.tui_installed"


# ---------------------------------------------------------------------------
# RED tests for Defect 2b: InterfacesData must use bool flags, not list
# ---------------------------------------------------------------------------


def test_step_interfaces_accepts_bool_flags(client):
    """Defect 2b: POST /distro/setup/steps/interfaces must accept {install_cli: bool, install_tui: bool}."""
    resp = client.post(
        "/distro/setup/steps/interfaces",
        json={"install_cli": False, "install_tui": False},
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data.get("status") == "ok"
    assert "cli_installed" in data
    assert "tui_installed" in data


# ---------------------------------------------------------------------------
# RED tests for Defect 2c + 2d: correct package URLs and shutil.which check
# ---------------------------------------------------------------------------


def test_step_interfaces_install_cli_uses_correct_git_url(client, monkeypatch):
    """Defect 2c: install_cli=True must invoke uv with the correct git URL, not 'amplifier-app-cli'."""
    calls = []

    async def fake_uv_install(binary, package_url):
        calls.append((binary, package_url))
        return {"status": "ok", "installed": True}

    monkeypatch.setattr("distro_plugin.routes._uv_tool_install", fake_uv_install)

    resp = client.post(
        "/distro/setup/steps/interfaces",
        json={"install_cli": True, "install_tui": False},
    )
    assert resp.status_code == 200

    assert len(calls) == 1, "expected exactly one _uv_tool_install call"
    binary, url = calls[0]
    assert binary == "amplifier", f"wrong binary: {binary}"
    assert url == "git+https://github.com/microsoft/amplifier", (
        f"wrong package URL: {url}"
    )


def test_step_interfaces_uses_shutil_which_for_installed_flag(client, monkeypatch):
    """Defect 2d: cli_installed/tui_installed in response must come from shutil.which, not uv exit code."""

    # Make _uv_tool_install succeed (simulating a successful install)
    async def fake_uv_install(binary, package_url):
        return {"status": "ok", "installed": True}

    monkeypatch.setattr("distro_plugin.routes._uv_tool_install", fake_uv_install)

    # shutil.which returns None for both (tool not actually on PATH even though uv "succeeded").
    # Patch via the shutil module directly; routes.py imports shutil and calls shutil.which(...)
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: None)

    resp = client.post(
        "/distro/setup/steps/interfaces",
        json={"install_cli": True, "install_tui": True},
    )
    assert resp.status_code == 200

    data = resp.json()
    # Even though uv "succeeded", shutil.which returned None → both must be False
    assert data["cli_installed"] is False, (
        "cli_installed must reflect shutil.which, not uv exit code"
    )
    assert data["tui_installed"] is False, (
        "tui_installed must reflect shutil.which, not uv exit code"
    )


# ---------------------------------------------------------------------------
# Fix 2: step_modules provisions memory directory for dev-memory feature
# ---------------------------------------------------------------------------


def test_step_modules_creates_memory_dir_for_dev_memory(client, settings):
    """Enabling dev-memory feature should create the ~/.amplifier/memory/ directory.

    Regression test for the Fix 2 provisioning change: after the include loop,
    step_modules() must mkdir(parents=True) when feat.category == "memory" so
    that the doctor _check_memory_dir() check passes immediately after setup.
    """
    memory_dir = settings.amplifier_home / "memory"
    assert not memory_dir.exists(), "pre-condition: memory dir must not exist yet"

    resp = client.post(
        "/distro/setup/steps/modules",
        json={"modules": ["dev-memory"]},
    )
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"
    assert memory_dir.exists(), (
        "step_modules must create the memory directory when dev-memory is enabled"
    )


# ---------------------------------------------------------------------------
# Phase 1 validate endpoint tests
# ---------------------------------------------------------------------------


def test_validate_provider_returns_model_list_for_standard_provider(client):
    """POST /distro/setup/steps/provider/validate returns needs_model + model list for Anthropic."""
    resp = client.post(
        "/distro/setup/steps/provider/validate",
        json={"provider": "anthropic", "api_key": "sk-ant-test123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_model"
    assert "models" in data
    assert "claude-sonnet-4-6" in data["models"]
    assert data["default_model"] == "claude-sonnet-4-6"


def test_validate_provider_copilot_with_env_token(client, monkeypatch):
    """POST /distro/setup/steps/provider/validate for Copilot with GITHUB_TOKEN in env."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_envtoken123")
    resp = client.post(
        "/distro/setup/steps/provider/validate",
        json={"provider": "github-copilot"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_model"
    assert data["token_source"] == "env"
    assert data["gh_token"] == ""  # env tokens are not exposed
    assert "claude-sonnet-4.6" in data["models"]


def test_validate_provider_copilot_with_gh_cli(client, monkeypatch):
    """POST /distro/setup/steps/provider/validate for Copilot with gh auth token subprocess."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_AGENT_TOKEN", raising=False)

    import subprocess

    def fake_run(*args, **kwargs):
        class FakeResult:
            returncode = 0
            stdout = "gho_fake_gh_cli_token"

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    resp = client.post(
        "/distro/setup/steps/provider/validate",
        json={"provider": "github-copilot"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_model"
    assert data["token_source"] == "gh_cli"
    assert data["gh_token"] == "gho_fake_gh_cli_token"


def test_validate_provider_copilot_no_auth(client, monkeypatch):
    """POST /distro/setup/steps/provider/validate for Copilot with no auth returns error."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_AGENT_TOKEN", raising=False)

    import subprocess

    def fake_run(*args, **kwargs):
        class FakeResult:
            returncode = 1
            stdout = ""

        return FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    resp = client.post(
        "/distro/setup/steps/provider/validate",
        json={"provider": "github-copilot"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    assert "gh auth login" in data["detail"]


def test_validate_provider_unknown_returns_error(client):
    """POST /distro/setup/steps/provider/validate with unknown provider returns 400."""
    resp = client.post(
        "/distro/setup/steps/provider/validate",
        json={"provider": "nonexistent"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Phase 2 registration with model + gh_token tests
# ---------------------------------------------------------------------------


def test_step_provider_with_model_writes_to_settings(settings, client, monkeypatch):
    """POST /distro/setup/steps/provider with model param writes that model to settings.yaml."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.post(
        "/distro/setup/steps/provider",
        json={
            "provider": "anthropic",
            "api_key": "sk-ant-test-key-12345",
            "model": "claude-opus-4-6",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model"] == "claude-opus-4-6"


def test_step_provider_copilot_with_gh_token(settings, client, monkeypatch):
    """POST /distro/setup/steps/provider for Copilot with gh_token writes GITHUB_TOKEN to keys.env."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    resp = client.post(
        "/distro/setup/steps/provider",
        json={
            "provider": "github-copilot",
            "api_key": "",
            "model": "claude-sonnet-4.6",
            "gh_token": "gho_phase2_token",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["provider"] == "github-copilot"

    # Verify gh_token was written
    from distro_plugin.providers import load_keys

    keys = load_keys(settings)
    assert keys.get("GITHUB_TOKEN") == "gho_phase2_token"
