# Handoff Injection on Session Start

## Goal

Extend `hooks-handoff` to automatically inject the most recent project handoff into a new session's context, completing the auto-inject side of cross-session continuity.

## Background

`hooks-handoff` already generates handoffs automatically at session end. It subscribes to `session:end`, makes a lightweight LLM call, and writes a structured summary to:

```
~/.amplifier/projects/<slug>/sessions/<session-id>/handoff.md
```

This generation path is working today.

`start-awareness.md` documents that handoffs are also "auto-injected at session start" — but this injection does not exist. The `on_session_start` handler currently only captures `working_directory` and derives the project slug. It does not read or inject any prior handoff.

The `session-handoff` agent exists as a manual fallback, but requiring the user to invoke it defeats the purpose. The intended experience is that sessions start already oriented — the assistant knows what was worked on, what's in progress, and what was decided, without the user having to ask or re-explain.

This design closes that gap.

## Approach

Extend the existing `on_session_start` handler inside `hooks-handoff` to inject the most recent handoff into the new session's context. No new module, no new hook, no new event subscription — one targeted handler extension.

This is the minimal change that delivers the experience. Generation was the first half; injection is the second.

## Architecture

```
session:start fires
       │
       ▼
on_session_start() — existing handler
       │
       ├─ capture working_directory          [unchanged]
       ├─ derive project slug                [unchanged]
       │
       └─ NEW: find & inject prior handoff
              │
              ├─ glob sessions/*/handoff.md
              ├─ sort by mtime, pick newest
              ├─ read content
              └─ return HookResult(action="inject_context", ...)
```

The hook returns `HookResult(action="inject_context", context_injection=content, context_injection_role="system")` from `on_session_start`. No coordinator access or additional wiring is needed — the `HookResult` carries the injection payload back to the kernel directly.

## Components

### `hooks-handoff` — `on_session_start` handler

**File:** `bundle/modules/hooks-handoff/amplifier_module_hooks_handoff/__init__.py`

**Changes:**
1. After deriving the project slug, glob for existing handoff files:
   ```
   <projects_dir>/<slug>/sessions/*/handoff.md
   ```
2. Sort results by file modification time (newest first).
3. Read the most recent handoff file.
4. Return `HookResult(action="inject_context", context_injection=<content>, context_injection_role="system")`.
5. If no handoff exists, return without error — session starts blank (same as today).

The `projects_dir` already exists as a config path used by generation; injection reads from the same location.

### Everything else — unchanged

| Component | Status |
|---|---|
| Generation logic | Unchanged |
| Handoff file format | Unchanged |
| Project slug derivation | Unchanged |
| Hook registration / event subscriptions | Unchanged |
| Configuration surface | Unchanged |
| `session-handoff` agent | Unchanged (still available for manual use) |

## Data Flow

```
Prior session ends
  → handoff.md written to ~/.amplifier/projects/<slug>/sessions/<id>/handoff.md

New session starts in same working directory
  → session:start fires
  → on_session_start derives slug from working_directory basename
  → glob ~/.amplifier/projects/<slug>/sessions/*/handoff.md
  → sort by mtime → pick newest
  → read file content
  → return HookResult(action="inject_context", context_injection=content, context_injection_role="system")
  → assistant begins session with prior context already loaded
```

## Edge Cases

| Scenario | Behavior |
|---|---|
| No handoff exists for this project | Session starts blank — no error, same as today |
| First-ever session in a project | No sessions directory yet — glob returns empty — session starts blank |
| `working_directory` not under `workspace_root` | Slug falls back to directory basename, matching generation behavior |
| Handoff file is empty or malformed | Skip injection, log a warning, session starts blank |
| Multiple sessions exist | Most recently modified `handoff.md` wins |

## Configuration

No new config keys are required. Injection behavior is gated by the existing `enabled` flag — if `hooks-handoff` is disabled, neither generation nor injection runs.

The existing `projects_dir` override applies to injection as well (it already determines where handoffs are written; it now also determines where injection looks).

A future `inject_on_start` boolean could be added to allow disabling injection independently of generation, but this is deferred (YAGNI) until there is a demonstrated need.

## Out of Scope

- Changes to how handoffs are generated
- Changes to the handoff file format or schema
- Injecting handoffs from a *different* project
- Injecting more than one prior handoff
- Any UI or surfacing of which handoff was injected
- Modifying the `session-handoff` manual agent

## Open Questions

None. The design is fully specified. The only deferred decision is the optional `inject_on_start` config flag, which is explicitly YAGNI for now.
