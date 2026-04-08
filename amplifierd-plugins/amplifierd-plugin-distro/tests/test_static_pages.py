"""Tests for static HTML page endpoints serving real files."""

from __future__ import annotations

from pathlib import Path


def test_get_setup_serves_wizard_html(client):
    """GET /distro/setup serves HTML with 200 and contains 'wizard' or 'setup'."""
    resp = client.get("/distro/setup")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.text.lower()
    assert "wizard" in body or "setup" in body


def test_get_settings_serves_settings_html(client):
    """GET /distro/settings serves HTML with 200 and contains 'settings'."""
    resp = client.get("/distro/settings")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "settings" in resp.text.lower()


# ---------------------------------------------------------------------------
# Static asset routes
# ---------------------------------------------------------------------------


def test_favicon_svg_served(client):
    """GET /favicon.svg returns 200."""
    resp = client.get("/favicon.svg")
    assert resp.status_code == 200


def test_static_theme_init_js_served(client):
    """GET /static/theme-init.js returns 200."""
    resp = client.get("/static/theme-init.js")
    assert resp.status_code == 200


def test_static_amplifier_theme_css_served(client):
    """GET /static/amplifier-theme.css returns 200."""
    resp = client.get("/static/amplifier-theme.css")
    assert resp.status_code == 200


def test_static_feedback_widget_js_served(client):
    """GET /static/feedback-widget.js returns 200."""
    resp = client.get("/static/feedback-widget.js")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# API path correctness in HTML files
# ---------------------------------------------------------------------------


_STATIC_DIR = Path(__file__).parent.parent / "src" / "distro_plugin" / "static"


def test_wizard_html_uses_distro_api():
    """wizard.html must declare const API = '/distro', not the old /apps/install-wizard path."""
    content = (_STATIC_DIR / "wizard.html").read_text()
    assert "const API = '/distro'" in content
    assert "/apps/install-wizard" not in content


def test_settings_html_uses_distro_api():
    """settings.html must declare const API = '/distro', not the old /apps/settings path."""
    content = (_STATIC_DIR / "settings.html").read_text()
    assert "const API = '/distro'" in content
    assert "/apps/settings" not in content


def test_settings_html_detect_endpoint():
    """settings.html must call /distro/detect, not /apps/install-wizard/detect."""
    content = (_STATIC_DIR / "settings.html").read_text()
    assert "fetch('/distro/detect')" in content
    assert "/apps/install-wizard/detect" not in content


def test_settings_html_wizard_link():
    """settings.html footer must link to /distro/setup, not /apps/install-wizard/."""
    content = (_STATIC_DIR / "settings.html").read_text()
    assert 'href="/distro/setup"' in content
    assert 'href="/apps/install-wizard/"' not in content


# ---------------------------------------------------------------------------
# GET /distro/ dashboard route
# ---------------------------------------------------------------------------


def test_get_distro_root_unconfigured_redirects(client):
    """GET /distro/ with no overlay (unconfigured) redirects to /distro/setup."""
    # Fresh tmp_path has no overlay bundle → compute_phase returns "unconfigured"
    resp = client.get("/distro/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert "/distro/setup" in resp.headers["location"]


def test_get_distro_root_detected_redirects_to_setup(client, settings):
    """GET /distro/ with overlay but no configured provider ('detected') redirects to /distro/setup."""
    # Create overlay so phase is "detected" (not "unconfigured"), but don't register any provider
    overlay_path = settings.distro_home / "bundle" / "bundle.yaml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text("bundle: {name: test}\n")

    resp = client.get("/distro/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert "/distro/setup" in resp.headers["location"]


def test_get_distro_root_configured_serves_dashboard(client, settings):
    """GET /distro/ when configured (overlay exists) serves dashboard.html with 200."""
    # Bootstrap overlay so compute_phase returns "detected" (not "unconfigured")
    overlay_path = settings.distro_home / "bundle" / "bundle.yaml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text("bundle: {name: test}\n")

    resp = client.get("/distro/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "amplifier" in resp.text.lower()


def test_dashboard_html_uses_updated_paths():
    """dashboard.html must use /chat/ and /distro/settings, not the old /apps/ paths."""
    content = (_STATIC_DIR / "dashboard.html").read_text()
    assert 'href="/chat/"' in content
    assert 'href="/distro/settings"' in content
    assert 'href="/distro/setup"' in content
    assert "/apps/settings/" not in content
    assert "/apps/install-wizard/" not in content
    assert "/apps/voice/" not in content
    assert "/apps/slack/" not in content


def test_dashboard_chat_and_voice_open_in_new_tab():
    """Chat and Voice app cards must open in new tabs to preserve the dashboard."""
    content = (_STATIC_DIR / "dashboard.html").read_text()
    # Chat link must have target="_blank"
    assert 'href="/chat/" target="_blank"' in content
    # Voice link must have target="_blank"
    assert 'href="/voice/" target="_blank"' in content


_SLACK_STATIC_DIR = (
    Path(__file__).parent.parent.parent
    / "amplifierd-plugin-slack"
    / "src"
    / "slack_plugin"
    / "static"
)


def test_slack_setup_back_link_goes_to_dashboard():
    """Slack setup page must link back to the dashboard, not settings."""
    slack_html = _SLACK_STATIC_DIR / "slack-setup.html"
    if not slack_html.exists():
        import pytest

        pytest.skip("slack-setup.html not found")
    content = slack_html.read_text()
    assert 'href="/distro/"' in content
    assert "Back to Dashboard" in content
    # Must NOT link to settings (user came from dashboard, not settings)
    assert 'href="/distro/settings"' not in content


def test_static_styles_css_served(client):
    """GET /static/styles.css returns 200."""
    resp = client.get("/static/styles.css")
    assert resp.status_code == 200


def test_wizard_finish_buttons_go_to_distro():
    """wizard.html finish/continue buttons must navigate to /distro/, not /."""
    content = (_STATIC_DIR / "wizard.html").read_text()
    assert "window.location.href='/distro/'" in content
    assert "window.location.href='/'" not in content


# ---------------------------------------------------------------------------
# Loading screen during bundle prewarm
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Root redirect (GET /)
# ---------------------------------------------------------------------------


def test_root_redirects_to_chat_when_configured(app, client, settings, monkeypatch):
    """GET / redirects to /chat/ when phase is 'ready' (overlay + configured provider)."""
    import asyncio

    from distro_plugin.overlay import add_include
    from distro_plugin.providers import PROVIDERS, add_provider_config

    event = asyncio.Event()
    event.set()
    app.state.bundles_ready = event

    # Set up a fully configured provider so compute_phase returns "ready"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-12345")
    add_provider_config(settings, "anthropic")
    add_include(settings, PROVIDERS["anthropic"].include)

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/chat/"


def test_root_redirects_to_setup_when_unconfigured(client):
    """GET / redirects to /distro/setup when no overlay (unconfigured)."""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert "/distro/setup" in resp.headers["location"]


def test_root_redirects_to_distro_during_prewarm(app, client):
    """GET / redirects to /distro/ during prewarm (which serves loading screen)."""
    import asyncio

    app.state.bundles_ready = asyncio.Event()  # unset = still loading
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/distro/"


# ---------------------------------------------------------------------------
# Loading screen during bundle prewarm
# ---------------------------------------------------------------------------


def test_dashboard_serves_loading_when_not_ready(app, client):
    """GET /distro/ serves loading.html when bundles_ready event is unset."""
    import asyncio

    app.state.bundles_ready = asyncio.Event()  # unset = still loading
    resp = client.get("/distro/", follow_redirects=False)
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "starting up" in resp.text.lower()


def test_dashboard_serves_dashboard_when_ready(app, client, settings):
    """GET /distro/ serves dashboard when bundles_ready event is set."""
    import asyncio

    event = asyncio.Event()
    event.set()
    app.state.bundles_ready = event

    # Create overlay so compute_phase != "unconfigured"
    overlay_path = settings.distro_home / "bundle" / "bundle.yaml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text("bundle: {name: test}\n")

    resp = client.get("/distro/")
    assert resp.status_code == 200
    assert "starting up" not in resp.text.lower()


def test_dashboard_works_without_bundles_ready(client, settings):
    """GET /distro/ works normally when bundles_ready is not on app.state (backward compat)."""
    # No bundles_ready set on app.state — should fall through to normal behavior
    overlay_path = settings.distro_home / "bundle" / "bundle.yaml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text("bundle: {name: test}\n")

    resp = client.get("/distro/")
    assert resp.status_code == 200
    # Should serve the real dashboard, not the loading screen
    assert "starting up" not in resp.text.lower()
