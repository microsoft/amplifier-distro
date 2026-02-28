# fix/overlay-hooks Implementation Plan

> **For execution:** Use `/execute-plan` mode or the subagent-driven-development recipe.

**Goal:** Move hooks-session-naming from the user overlay (overlay.py) into the distro's default bundle (bundle/behaviors/start.yaml) where it belongs, and add migration to clean up existing user overlays.

**Architecture:** Three changes — add hook to start.yaml, remove injection from overlay.py, add migration for stale overlays. All driven by two new test classes in a new `test_overlay.py` file.

**Tech Stack:** Python 3.11+, YAML, pytest, uv

---

## Orientation

**Worktree root:** `/Users/samule/repo/amplifier-distro-msft/.worktrees/fix-overlay-hooks`

All file paths below are relative to that root.

**Run all tests from:**
```
cd /Users/samule/repo/amplifier-distro-msft/.worktrees/fix-overlay-hooks/distro-server
```

**Baseline:** 945 tests passing on `main`.

---

## What You're Fixing

`distro-server/src/amplifier_distro/overlay.py` currently declares this constant:

```python
SESSION_NAMING_URI = (
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=modules/hooks-session-naming"
)
```

...and injects it as a bundle `include` in three places inside `ensure_overlay()`:

1. **Fresh overlay** (line 96): `{"bundle": SESSION_NAMING_URI},` in the initial `includes` list
2. **Existing overlay update** (lines 111–112): `if SESSION_NAMING_URI not in current_uris: includes.append(...)`

The problem: `modules/hooks-session-naming` is a **Python module package** (has a `pyproject.toml` entry point), not a bundle (no `bundle.md`). Loading it as a bundle fires "Not a valid bundle" on every session.

The fix is three surgical changes:
1. Add `hooks-session-naming` properly as a `hooks:` entry in `bundle/behaviors/start.yaml`
2. Delete `SESSION_NAMING_URI` from `overlay.py` and all three injection sites
3. Add migration logic so existing users' overlays get the stale entry removed on the next `ensure_overlay()` call

---

## Files Involved

| Action | Path |
|--------|------|
| **Create** | `distro-server/tests/test_overlay.py` |
| **Modify** | `distro-server/src/amplifier_distro/overlay.py` |
| **Modify** | `bundle/behaviors/start.yaml` |

---

## Task 1 — Write the failing test: fresh overlay must NOT contain `SESSION_NAMING_URI`

**File to create:** `distro-server/tests/test_overlay.py`

Create this file with the content below. It tests that `ensure_overlay()` on an empty filesystem does not inject the stale URI anywhere in `includes`.

```python
"""Overlay bundle management tests.

Tests for overlay.py:
  - ensure_overlay() on a fresh filesystem must NOT include SESSION_NAMING_URI
  - ensure_overlay() on an existing overlay with the stale URI must strip it
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from amplifier_distro import overlay
from amplifier_distro.features import AMPLIFIER_START_URI, PROVIDERS, provider_bundle_uri

# The URI that was wrongly injected by old versions of overlay.py.
# Defined here explicitly so the tests don't depend on the constant still
# existing in overlay.py after the fix.
_STALE_URI = (
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=modules/hooks-session-naming"
)

_ANTHROPIC = PROVIDERS["anthropic"]


@pytest.fixture
def overlay_path(tmp_path: Path, monkeypatch) -> Path:
    """Redirect overlay file operations to a temp directory.

    Patches overlay_bundle_path() and overlay_dir() so that no test
    ever touches ~/.amplifier-distro.
    """
    bundle_path = tmp_path / "bundle" / "bundle.yaml"
    monkeypatch.setattr(overlay, "overlay_bundle_path", lambda: bundle_path)
    monkeypatch.setattr(overlay, "overlay_dir", lambda: bundle_path.parent)
    return bundle_path


class TestFreshOverlayDoesNotInjectSessionNaming:
    """ensure_overlay() on an empty filesystem must NOT include SESSION_NAMING_URI."""

    def test_session_naming_uri_absent_from_fresh_overlay(self, overlay_path):
        """The stale bundle URI must not appear in a brand-new overlay."""
        overlay.ensure_overlay(_ANTHROPIC)

        data = yaml.safe_load(overlay_path.read_text())
        uris = [
            entry["bundle"] if isinstance(entry, dict) else entry
            for entry in data.get("includes", [])
        ]
        assert _STALE_URI not in uris, (
            f"SESSION_NAMING_URI should not appear in a fresh overlay but found "
            f"it in includes: {uris}"
        )

    def test_fresh_overlay_still_contains_start_uri(self, overlay_path):
        """Removing the stale URI must not also remove the distro start URI."""
        overlay.ensure_overlay(_ANTHROPIC)

        data = yaml.safe_load(overlay_path.read_text())
        uris = [
            entry["bundle"] if isinstance(entry, dict) else entry
            for entry in data.get("includes", [])
        ]
        assert AMPLIFIER_START_URI in uris, (
            f"AMPLIFIER_START_URI must still be present, got: {uris}"
        )

    def test_fresh_overlay_still_contains_provider_uri(self, overlay_path):
        """Removing the stale URI must not also remove the provider URI."""
        overlay.ensure_overlay(_ANTHROPIC)

        data = yaml.safe_load(overlay_path.read_text())
        uris = [
            entry["bundle"] if isinstance(entry, dict) else entry
            for entry in data.get("includes", [])
        ]
        assert provider_bundle_uri(_ANTHROPIC) in uris, (
            f"Provider URI must still be present, got: {uris}"
        )
```

---

## Task 2 — Verify Task 1 tests FAIL

Run **only** the new test class. All three tests must **fail** right now because `overlay.py` still injects the URI.

```
uv run pytest tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming -v
```

**Expected output:**
```
FAILED tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming::test_session_naming_uri_absent_from_fresh_overlay
FAILED tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming::test_fresh_overlay_still_contains_start_uri - AssertionError ...
...
3 failed
```

The first test fails because the URI is present. The other two may pass or fail depending on whether pytest even gets to them — the important thing is that the file runs without import errors and the first test fails. If only the first fails and the others pass, that is fine — the fix will keep the two passing.

---

## Task 3 — Remove `SESSION_NAMING_URI` from `overlay.py`

**File to modify:** `distro-server/src/amplifier_distro/overlay.py`

Replace the entire file with the version below. The changes are:
- **Delete** the `SESSION_NAMING_URI` constant (lines 26–29 of the original)
- **Add** private `_STALE_SESSION_NAMING_URI` constant (for use in migration only — never injected)
- **Remove** `{"bundle": SESSION_NAMING_URI},` from the fresh-overlay `includes` list
- **Remove** the `if SESSION_NAMING_URI not in current_uris:` block from the existing-overlay branch

```python
"""Local overlay bundle management.

The distro creates a local bundle that includes the maintained distro bundle.
The wizard and settings apps modify this overlay; the underlying
distro bundle is never touched.

The overlay is a directory containing a ``bundle.yaml`` file:

    ~/.amplifier-distro/bundle/
    └── bundle.yaml

Foundation's ``load_bundle()`` loads it by path and handles all
include resolution and composition automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .conventions import DISTRO_OVERLAY_DIR
from .features import AMPLIFIER_START_URI, Provider, provider_bundle_uri

# URI that was incorrectly injected by overlay.py in older installations.
# Kept here ONLY for migration — it is never added to new overlays.
_STALE_SESSION_NAMING_URI = (
    "git+https://github.com/microsoft/amplifier-foundation@main"
    "#subdirectory=modules/hooks-session-naming"
)


def overlay_dir() -> Path:
    """Return the overlay bundle directory path, expanded."""
    return Path(DISTRO_OVERLAY_DIR).expanduser()


def overlay_bundle_path() -> Path:
    """Return the path to the overlay bundle.yaml."""
    return overlay_dir() / "bundle.yaml"


def overlay_exists() -> bool:
    """Check whether the local overlay bundle has been created."""
    return overlay_bundle_path().exists()


def read_overlay() -> dict[str, Any]:
    """Read and parse the current overlay bundle. Returns {} if missing."""
    path = overlay_bundle_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError):
        return {}


def _write_overlay(data: dict[str, Any]) -> Path:
    """Write the overlay bundle.yaml to disk."""
    path = overlay_bundle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return path


def get_includes(data: dict[str, Any] | None = None) -> list[str]:
    """Extract the list of include URIs from overlay data."""
    if data is None:
        data = read_overlay()
    return [
        entry["bundle"] if isinstance(entry, dict) else entry
        for entry in data.get("includes", [])
    ]


def ensure_overlay(provider: Provider) -> Path:
    """Create (or update) the overlay bundle with include to maintained bundle + a provider.

    If the overlay already exists, the provider include is added only if
    not already present.  The distro bundle include is always ensured.
    Stale SESSION_NAMING_URI entries are silently removed (migration).
    Returns the path to the overlay directory.
    """
    data = read_overlay()

    if not data:
        # Fresh overlay — hooks-session-naming lives in bundle/behaviors/start.yaml,
        # not here.
        data = {
            "bundle": {
                "name": "amplifier-distro",
                "version": "0.1.0",
                "description": "Local Amplifier Distro environment",
            },
            "includes": [
                {"bundle": AMPLIFIER_START_URI},
                {"bundle": provider_bundle_uri(provider)},
            ],
        }
    else:
        # Migration: strip the stale session-naming URI if present from old installs.
        data["includes"] = [
            entry
            for entry in data.get("includes", [])
            if (entry.get("bundle") if isinstance(entry, dict) else entry)
            != _STALE_SESSION_NAMING_URI
        ]

        # Existing overlay — ensure required includes are present.
        current_uris = set(get_includes(data))
        includes = data.setdefault("includes", [])

        if AMPLIFIER_START_URI not in current_uris:
            includes.insert(0, {"bundle": AMPLIFIER_START_URI})

        prov_uri = provider_bundle_uri(provider)
        if prov_uri not in current_uris:
            includes.append({"bundle": prov_uri})

    _write_overlay(data)
    return overlay_dir()


def add_include(uri: str) -> None:
    """Add a bundle include to the overlay (idempotent)."""
    data = read_overlay()
    if not data:
        return  # Overlay must exist first

    current_uris = set(get_includes(data))
    if uri not in current_uris:
        data.setdefault("includes", []).append({"bundle": uri})
        _write_overlay(data)


def remove_include(uri: str) -> None:
    """Remove a bundle include from the overlay."""
    data = read_overlay()
    if not data:
        return

    data["includes"] = [
        entry
        for entry in data.get("includes", [])
        if (entry.get("bundle") if isinstance(entry, dict) else entry) != uri
    ]
    _write_overlay(data)
```

---

## Task 4 — Verify Task 1 tests PASS

Run the same command as Task 2. All three tests must now pass.

```
uv run pytest tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming -v
```

**Expected output:**
```
PASSED tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming::test_session_naming_uri_absent_from_fresh_overlay
PASSED tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming::test_fresh_overlay_still_contains_start_uri
PASSED tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming::test_fresh_overlay_still_contains_provider_uri
3 passed
```

---

## Task 5 — Commit

```
git add distro-server/tests/test_overlay.py \
        distro-server/src/amplifier_distro/overlay.py
git commit -m "fix: remove SESSION_NAMING_URI injection from overlay.py

hooks-session-naming is a Python module package, not a bundle.
Injecting it via overlay includes caused 'Not a valid bundle'
errors on every session.

- Delete SESSION_NAMING_URI constant
- Remove it from fresh overlay includes
- Remove the idempotency guard that would re-add it to existing overlays
- Add _STALE_SESSION_NAMING_URI private constant for migration use

Ref: docs/plans/2026-02-27-distro-remaining-issues.md Issue 6"
```

---

## Task 6 — Write the failing test: stale overlay migration

**File to modify:** `distro-server/tests/test_overlay.py`

**Append** this second test class to the bottom of the file you created in Task 1. Do not touch the first class.

```python
class TestStaleOverlayMigration:
    """ensure_overlay() on an existing overlay with the stale URI must remove it."""

    def test_stale_session_naming_uri_removed_on_update(self, overlay_path):
        """An existing overlay that contains the stale URI must have it stripped."""
        # Arrange: write a stale overlay with the bad URI already present
        stale_data = {
            "bundle": {
                "name": "amplifier-distro",
                "version": "0.1.0",
                "description": "Local Amplifier Distro environment",
            },
            "includes": [
                {"bundle": AMPLIFIER_START_URI},
                {"bundle": provider_bundle_uri(_ANTHROPIC)},
                {"bundle": _STALE_URI},  # the bad entry we need to migrate away
            ],
        }
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text(yaml.dump(stale_data, default_flow_style=False))

        # Act
        overlay.ensure_overlay(_ANTHROPIC)

        # Assert
        data = yaml.safe_load(overlay_path.read_text())
        uris = [
            entry["bundle"] if isinstance(entry, dict) else entry
            for entry in data.get("includes", [])
        ]
        assert _STALE_URI not in uris, (
            f"Migration should have removed the stale session naming URI, "
            f"but it is still present in includes: {uris}"
        )

    def test_migration_preserves_valid_includes(self, overlay_path):
        """Migration must not remove valid includes — only the stale URI."""
        stale_data = {
            "bundle": {
                "name": "amplifier-distro",
                "version": "0.1.0",
                "description": "Local Amplifier Distro environment",
            },
            "includes": [
                {"bundle": AMPLIFIER_START_URI},
                {"bundle": provider_bundle_uri(_ANTHROPIC)},
                {"bundle": _STALE_URI},
            ],
        }
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text(yaml.dump(stale_data, default_flow_style=False))

        overlay.ensure_overlay(_ANTHROPIC)

        data = yaml.safe_load(overlay_path.read_text())
        uris = [
            entry["bundle"] if isinstance(entry, dict) else entry
            for entry in data.get("includes", [])
        ]
        assert AMPLIFIER_START_URI in uris, "start URI must survive migration"
        assert provider_bundle_uri(_ANTHROPIC) in uris, "provider URI must survive migration"

    def test_clean_overlay_unaffected_by_migration(self, overlay_path):
        """Overlays that never had the stale URI should not be modified."""
        clean_data = {
            "bundle": {
                "name": "amplifier-distro",
                "version": "0.1.0",
                "description": "Local Amplifier Distro environment",
            },
            "includes": [
                {"bundle": AMPLIFIER_START_URI},
                {"bundle": provider_bundle_uri(_ANTHROPIC)},
            ],
        }
        overlay_path.parent.mkdir(parents=True, exist_ok=True)
        overlay_path.write_text(yaml.dump(clean_data, default_flow_style=False))

        overlay.ensure_overlay(_ANTHROPIC)

        data = yaml.safe_load(overlay_path.read_text())
        uris = [
            entry["bundle"] if isinstance(entry, dict) else entry
            for entry in data.get("includes", [])
        ]
        assert _STALE_URI not in uris
        assert AMPLIFIER_START_URI in uris
        assert provider_bundle_uri(_ANTHROPIC) in uris
```

---

## Task 7 — Verify Task 6 tests FAIL

Run only the new class to confirm you have real failing tests before implementing.

```
uv run pytest tests/test_overlay.py::TestStaleOverlayMigration -v
```

**Expected output:**
```
FAILED tests/test_overlay.py::TestStaleOverlayMigration::test_stale_session_naming_uri_removed_on_update
FAILED tests/test_overlay.py::TestStaleOverlayMigration::test_migration_preserves_valid_includes
FAILED tests/test_overlay.py::TestStaleOverlayMigration::test_clean_overlay_unaffected_by_migration
3 failed
```

If Task 3 is already done and overlay.py already has the migration filter, these tests will pass — in that case skip to Task 9. The migration logic you added in Task 3 may already cover this; run the tests first to confirm.

> **Note:** If you followed the plan in order, you already implemented the migration filter in Task 3 as part of the full `ensure_overlay()` replacement. In that case, Task 7 should show these tests **passing**, not failing — skip straight to Task 9.

---

## Task 8 — Implement migration logic (only if Task 7 tests failed)

> **Skip this task** if Task 7 tests passed. The implementation from Task 3 already covers it.

If for some reason the migration filter is missing from `overlay.py`, add it now. In the `else` branch of `ensure_overlay()`, **before** the line `current_uris = set(get_includes(data))`, insert:

```python
        # Migration: strip the stale session-naming URI if present from old installs.
        data["includes"] = [
            entry
            for entry in data.get("includes", [])
            if (entry.get("bundle") if isinstance(entry, dict) else entry)
            != _STALE_SESSION_NAMING_URI
        ]
```

---

## Task 9 — Verify all overlay tests pass

Run the complete test file to confirm both classes are green.

```
uv run pytest tests/test_overlay.py -v
```

**Expected output:**
```
PASSED tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming::test_session_naming_uri_absent_from_fresh_overlay
PASSED tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming::test_fresh_overlay_still_contains_start_uri
PASSED tests/test_overlay.py::TestFreshOverlayDoesNotInjectSessionNaming::test_fresh_overlay_still_contains_provider_uri
PASSED tests/test_overlay.py::TestStaleOverlayMigration::test_stale_session_naming_uri_removed_on_update
PASSED tests/test_overlay.py::TestStaleOverlayMigration::test_migration_preserves_valid_includes
PASSED tests/test_overlay.py::TestStaleOverlayMigration::test_clean_overlay_unaffected_by_migration
6 passed
```

---

## Task 10 — Commit

```
git add distro-server/tests/test_overlay.py
git commit -m "test: add overlay migration tests for stale SESSION_NAMING_URI removal

TestStaleOverlayMigration verifies that ensure_overlay() strips the
stale hooks-session-naming bundle URI from existing user overlays,
while preserving all valid includes.

Ref: docs/plans/2026-02-27-distro-remaining-issues.md Issue 6"
```

---

## Task 11 — Add `hooks-session-naming` to `bundle/behaviors/start.yaml`

**File to modify:** `bundle/behaviors/start.yaml`

The current file ends at line 18 with:
```yaml
    config:
      enabled: true
      blocking: false
```

Append the new hook entry so the full `hooks:` section reads:

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

The complete new `bundle/behaviors/start.yaml` (replace the file):

```yaml
bundle:
  name: start-behavior
  version: 0.1.0
  description: >
    Core behavior for amplifier-start. Provides environment conventions,
    session handoffs, health checks, and friction detection as composable
    capabilities.

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

agents:
  include:
    - start:health-checker
    - start:friction-detector
    - start:session-handoff

context:
  include:
    - start:context/start-awareness.md
```

---

## Task 12 — Verify full test suite passes

Run the complete test suite. You should see at least 945 tests passing (the baseline), plus 6 new overlay tests = 951+.

```
uv run pytest tests/ -v
```

**Expected output (summary line):**
```
951 passed (or more)
```

If any tests fail that were passing before, stop and debug before continuing. The overlay tests and the existing tests must all be green. Do not proceed to Task 13 with a red suite.

---

## Task 13 — Final commit

```
git add bundle/behaviors/start.yaml
git commit -m "fix: declare hooks-session-naming in bundle/behaviors/start.yaml

hooks-session-naming is a Python module, not a bundle. It belongs
as a first-class hooks: entry in the distro's default bundle, not
as a bundle include in the user overlay.

This completes Issue 6:
  - overlay.py no longer injects the stale URI (previous commit)
  - start.yaml now properly declares the hook with correct config
  - Existing user overlays are migrated on next ensure_overlay() call

Ref: docs/plans/2026-02-27-distro-remaining-issues.md Issue 6"
```

---

## Summary of All Changes

| File | Change |
|------|--------|
| `distro-server/tests/test_overlay.py` | **Created** — 6 tests across 2 classes |
| `distro-server/src/amplifier_distro/overlay.py` | **Modified** — deleted `SESSION_NAMING_URI`, added `_STALE_SESSION_NAMING_URI`, removed 3 injection sites, added migration filter |
| `bundle/behaviors/start.yaml` | **Modified** — added `hooks-session-naming` entry under `hooks:` |

## Diff Summary for overlay.py

```diff
-SESSION_NAMING_URI = (
-    "git+https://github.com/microsoft/amplifier-foundation@main"
-    "#subdirectory=modules/hooks-session-naming"
-)
+# URI that was incorrectly injected by overlay.py in older installations.
+# Kept here ONLY for migration — it is never added to new overlays.
+_STALE_SESSION_NAMING_URI = (
+    "git+https://github.com/microsoft/amplifier-foundation@main"
+    "#subdirectory=modules/hooks-session-naming"
+)

 def ensure_overlay(provider: Provider) -> Path:
     ...
     if not data:
         data = {
             ...
             "includes": [
                 {"bundle": AMPLIFIER_START_URI},
                 {"bundle": provider_bundle_uri(provider)},
-                {"bundle": SESSION_NAMING_URI},
             ],
         }
     else:
+        # Migration: strip the stale session-naming URI if present
+        data["includes"] = [
+            entry
+            for entry in data.get("includes", [])
+            if (entry.get("bundle") if isinstance(entry, dict) else entry)
+            != _STALE_SESSION_NAMING_URI
+        ]
+
         current_uris = set(get_includes(data))
         includes = data.setdefault("includes", [])
         if AMPLIFIER_START_URI not in current_uris:
             includes.insert(0, {"bundle": AMPLIFIER_START_URI})
         prov_uri = provider_bundle_uri(provider)
         if prov_uri not in current_uris:
             includes.append({"bundle": prov_uri})
-        if SESSION_NAMING_URI not in current_uris:
-            includes.append({"bundle": SESSION_NAMING_URI})
```
