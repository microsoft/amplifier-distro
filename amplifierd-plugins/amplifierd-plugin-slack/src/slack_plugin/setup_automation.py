"""
Slack setup automation via agent-browser.

Automates manual steps of Slack app creation:
1. Navigate to Slack app creation with pre-filled manifest
2. Walk through creation wizard (select workspace, review, create)
3. Generate App-Level Token (xapp-) — this CANNOT be done via any API
4. Install app to workspace
5. Retrieve Bot Token (xoxb-)
6. Return both tokens
"""
import asyncio
import subprocess
import json
import re
import logging
from dataclasses import dataclass, field
from typing import Any
import threading

logger = logging.getLogger(__name__)

# Thread-safe automation status for SSE streaming
_automation_status: dict[str, Any] = {}
_status_lock = threading.Lock()

@dataclass
class AutomationResult:
    success: bool
    bot_token: str = ""
    app_token: str = ""
    error: str = ""
    step_reached: str = ""

def get_automation_status() -> dict[str, Any]:
    with _status_lock:
        return dict(_automation_status)

def _set_status(step: str, detail: str, complete: bool = False):
    with _status_lock:
        _automation_status.update({"step": step, "detail": detail, "complete": complete})

def _run_browser_cmd(*args: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["agent-browser", *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"agent-browser failed: {result.stderr.strip()}")
    return result.stdout

def check_agent_browser() -> bool:
    try:
        result = subprocess.run(["agent-browser", "--version"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

def _find_ref(snapshot: str, *keywords: str) -> str | None:
    """Find an element ref (@eN) from snapshot lines matching ALL keywords."""
    for line in snapshot.strip().split("\n"):
        lower = line.lower()
        if all(kw.lower() in lower for kw in keywords):
            m = re.search(r"(@e\d+)", line)
            if m:
                return m.group(1)
    return None

async def automate_slack_setup(manifest_yaml: str) -> AutomationResult:
    import urllib.parse

    if not check_agent_browser():
        return AutomationResult(
            success=False,
            error="agent-browser not installed. Install: npm install -g agent-browser && agent-browser install",
            step_reached="preflight",
        )

    encoded = urllib.parse.quote(manifest_yaml)
    url = f"https://api.slack.com/apps?new_app=1&manifest_yaml={encoded}"

    try:
        _set_status("opening", "Opening Slack app creation page...")
        _run_browser_cmd("open", url)
        await asyncio.sleep(3)

        snapshot = _run_browser_cmd("snapshot", "-ic")
        if "sign in" in snapshot.lower() or "email" in snapshot.lower():
            _set_status("login_required", "Please log into Slack in the browser window", complete=True)
            return AutomationResult(success=False, error="Please log into Slack, then retry.", step_reached="login_required")

        _set_status("creating", "Navigating app creation wizard...")
        result = await _navigate_creation_wizard()
        if not result.success:
            _set_status("failed", result.error, complete=True)
            return result

        _set_status("app_token", "Generating App-Level Token...")
        app_token = await _generate_app_level_token()
        if not app_token:
            _set_status("partial", "App-Level Token generation failed", complete=True)
            return AutomationResult(success=False, error="Failed to generate app-level token.", step_reached="app_token")

        _set_status("installing", "Installing app to workspace...")
        bot_token = await _install_and_get_bot_token()
        if not bot_token:
            _set_status("partial", "Bot token retrieval failed", complete=True)
            return AutomationResult(success=False, app_token=app_token, error="Failed to get bot token.", step_reached="bot_token")

        _run_browser_cmd("close")
        _set_status("done", "Setup complete!", complete=True)
        return AutomationResult(success=True, bot_token=bot_token, app_token=app_token)
    except Exception as e:
        logger.exception("Automation failed")
        _set_status("error", str(e), complete=True)
        return AutomationResult(success=False, error=str(e), step_reached="unknown")

async def _navigate_creation_wizard() -> AutomationResult:
    for _ in range(5):
        await asyncio.sleep(2)
        snapshot = _run_browser_cmd("snapshot", "-ic")
        if "basic information" in snapshot.lower() or "app-level tokens" in snapshot.lower():
            return AutomationResult(success=True, step_reached="app_created")
        for label in ["next", "create"]:
            ref = _find_ref(snapshot, label)
            if ref:
                _run_browser_cmd("click", ref)
                await asyncio.sleep(2)
                break
    return AutomationResult(success=False, error="Could not navigate creation wizard", step_reached="wizard")

async def _generate_app_level_token() -> str:
    snapshot = _run_browser_cmd("snapshot", "-ic")
    ref = _find_ref(snapshot, "generate token") or _find_ref(snapshot, "app-level token")
    if ref:
        _run_browser_cmd("click", ref)
        await asyncio.sleep(2)

    snapshot = _run_browser_cmd("snapshot", "-ic")
    ref = _find_ref(snapshot, "name")
    if ref:
        _run_browser_cmd("fill", ref, "amplifier")

    snapshot = _run_browser_cmd("snapshot", "-ic")
    ref = _find_ref(snapshot, "scope") or _find_ref(snapshot, "connections:write")
    if ref:
        _run_browser_cmd("click", ref)
        await asyncio.sleep(1)

    snapshot = _run_browser_cmd("snapshot", "-ic")
    ref = _find_ref(snapshot, "generate")
    if ref:
        _run_browser_cmd("click", ref)
        await asyncio.sleep(2)

    snapshot = _run_browser_cmd("snapshot", "-ic")
    m = re.search(r"(xapp-\S+)", snapshot)
    return m.group(1) if m else ""

async def _install_and_get_bot_token() -> str:
    snapshot = _run_browser_cmd("snapshot", "-ic")
    ref = _find_ref(snapshot, "oauth", "permission")
    if ref:
        _run_browser_cmd("click", ref)
        await asyncio.sleep(2)

    snapshot = _run_browser_cmd("snapshot", "-ic")
    ref = _find_ref(snapshot, "install", "workspace")
    if ref:
        _run_browser_cmd("click", ref)
        await asyncio.sleep(3)

    snapshot = _run_browser_cmd("snapshot", "-ic")
    ref = _find_ref(snapshot, "allow")
    if ref:
        _run_browser_cmd("click", ref)
        await asyncio.sleep(3)

    snapshot = _run_browser_cmd("snapshot", "-ic")
    m = re.search(r"(xoxb-\S+)", snapshot)
    if m:
        return m.group(1)

    ref = _find_ref(snapshot, "copy") or _find_ref(snapshot, "show") or _find_ref(snapshot, "reveal")
    if ref:
        _run_browser_cmd("click", ref)
        await asyncio.sleep(1)

    snapshot = _run_browser_cmd("snapshot", "-ic")
    m = re.search(r"(xoxb-\S+)", snapshot)
    return m.group(1) if m else ""
