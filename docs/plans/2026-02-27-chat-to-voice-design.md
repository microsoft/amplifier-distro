# Chat-to-Voice Session Handoff Design

## Overview

Implement cross-interface session continuity so users can resume a text chat (or CLI) session in the voice app without losing conversational context.

The AMPLIFIER_HOME_CONTRACT declares: *"Cross-interface — CLI session can be resumed in TUI, voice, or web chat."* The voice app already supports voice-to-voice reconnect. Chat-to-voice is the missing direction of this same promise. The mechanism is identical to voice-to-voice resume: continue the same Amplifier session, inject context into a new Realtime session. The challenge is **what** context to inject — the full chat transcript is explicitly the wrong answer.

---

## Requirements

### Functional

- **FR-1:** A user can open `SessionPicker` in the voice app and see non-voice sessions (chat, CLI, Slack) alongside voice sessions, each labeled by source app.
- **FR-2:** Selecting a non-voice session calls the same `handleResume(sessionId)` path as voice resume — no new client code path.
- **FR-3:** `POST /sessions/{id}/resume` returns a `context_to_inject` payload shaped correctly for OpenAI Realtime `conversation.item.create` injection, regardless of the source session type.
- **FR-4:** For non-voice sessions, `context_to_inject` is built from: (1) the session's `handoff.md` summary, formatted as a single assistant-role context item, and (2) the last 8 user/assistant text turns from the session transcript.
- **FR-5:** Tool calls, tool outputs, code blocks, and structured data are excluded from the injected context.
- **FR-6:** If `handoff.md` is absent, the resume falls back to recent turns with a synthetic preamble. It never blocks.
- **FR-7:** Voice-to-voice resume behavior is unchanged. The `created_by_app == "voice"` code path is a pure refactor (extract to helper, no behavioral change).
- **FR-8:** Once resumed from a chat session, the voice app header indicates "Continued from chat" (derived from injected context metadata, no new server state).

### Non-Functional

- **NFR-1:** No new endpoints, no new database, no new infrastructure. The filesystem already has what's needed.
- **NFR-2:** Context injection must not block the WebRTC connection. All failures are logged and skipped, never surfaced as hard errors.
- **NFR-3:** The server response shape (`{ client_secret, context_to_inject }`) is identical across session types — zero client deserialization changes.
- **NFR-4:** Legacy sessions missing `created_by_app` in `metadata.json` are treated as non-voice (safe default, recent turns only).
- **NFR-5:** The `recent_turns` count (default: 8) must be tunable without a code change.

---

## Design

### Architecture

```
SessionPicker (widened filter: all sessions, labeled by created_by_app)
    │
    └─ handleResume(sessionId)          [unchanged]
         │
         └─ POST /sessions/{id}/resume
               │
               ├─ read metadata.json → created_by_app
               │
               ├─ [voice]     existing path: voice_transcript → conversation.item[]
               │
               └─ [non-voice] new path: _build_chat_context()
                     ├─ read handoff.md   → summary item
                     └─ read last N turns → recent exchange items
                         │
                         └─ context_to_inject: [summary_item, ...recent_items]
```

Both paths return the same response shape. The Realtime session initialization — `handleResume` → WebRTC setup → `conversation.item.create` injection — is completely unchanged.

### Context Format

Injected items conform to the OpenAI Realtime `conversation.item` object shape:

```json
{
  "type": "message",
  "role": "assistant",
  "content": [{ "type": "text", "text": "Context from prior text session: ..." }]
}
```

The handoff summary occupies one item. Each recent turn occupies one item (role `user` or `assistant`). Tool calls are stripped at read time.

### Discriminator

`metadata.json`'s `created_by_app` field is the branching key — already declared load-bearing in `docs/voice-architecture.md`. No new metadata fields are introduced.

---

## Components

### `resume_session()` — `apps/voice/__init__.py`

The existing route handler at `POST /sessions/{id}/resume`. Two changes:

1. Read `metadata.json` to extract `created_by_app` before delegating to the backend.
2. Call `_build_context_for_resume(session_id, project_id, created_by_app)` to produce `context_to_inject`.

Route signature and response shape are unchanged.

---

### `_build_context_for_resume()` — new helper

```python
async def _build_context_for_resume(
    session_id: str,
    project_id: str,
    created_by_app: str,
    recent_turns: int = 8,
) -> list[dict]:
```

Single branching function. Routes `created_by_app == "voice"` to the extracted voice transcript helper (existing logic, no behavior change). Routes everything else to `_build_chat_context()`. Lives in `apps/voice/__init__.py` or extracted to `transcript/context.py` if the file grows unwieldy.

---

### `_build_chat_context()` — new helper

Reads from `~/.amplifier/projects/{project_id}/sessions/{session_id}/`:

**Source 1 — `handoff.md`**
If present: formats as a single `conversation.item` (type `message`, role `assistant`) with preamble `"Context from prior text session:"` followed by the handoff content verbatim.

If absent: emits a synthetic preamble item: `"Resuming a text chat session. Here are the most recent exchanges:"` — resume continues without blocking.

**Source 2 — Session transcript (last N turns)**
Reads the session transcript JSON. Extracts the tail of the conversation. Filters to `role: user` and `role: assistant` text content only. Strips all tool calls, tool outputs, code blocks, and structured data. Formats each as a `conversation.item`.

Returns `list[dict]` ready for `conversation.item.create` injection.

---

### `SessionPicker` — `static/index.html`

**Current behavior:** Filters sessions to `created_by_app == "voice"` only.

**New behavior:** Shows all sessions (or all sessions with a `handoff.md`). Each list item labeled by `created_by_app` — e.g., "Voice conversation", "Chat conversation". No backend change required; `GET /sessions` already returns `created_by_app` in session metadata.

**No change to** `handleResume()` — it receives a session ID and fires the resume request identically regardless of session type.

---

## Implementation Approach

This is a server-side-first change with a minimal frontend delta.

**Phase 1 — Server refactor (pure refactor, no behavior change)**
Extract the existing inline voice transcript logic from `resume_session()` into a `_build_voice_context()` helper. Add the `_build_context_for_resume()` dispatcher. All existing tests pass unchanged. This is the safety baseline.

**Phase 2 — Chat context builder**
Implement `_build_chat_context()` with fixture-backed unit tests in place before the implementation (TDD). Cover: normal case (handoff + turns), absent handoff, empty transcript, legacy session missing `created_by_app`.

**Phase 3 — Route wiring**
Wire `_build_context_for_resume()` into `resume_session()`. Run the integration test against a seeded session fixture.

**Phase 4 — Frontend label**
Widen `SessionPicker` filter. Add `created_by_app` label to session list items. Manual smoke test.

**Transcript format audit** must happen before Phase 2. Chat, CLI, and Slack transcripts may use slightly different JSON structures. `_build_chat_context()` must normalize across all three.

---

## Testing Strategy

### Unit — `_build_chat_context()`
Fixture session directory with `handoff.md` + mock transcript JSON. Assert:
- Returned list starts with the handoff item (correct preamble text, correct role/type shape)
- Correct number of turn items follow (default: 8)
- Tool call entries are absent from the result

### Unit — `_build_context_for_resume()` branching
Mock both sub-functions. Assert:
- `created_by_app == "voice"` routes to voice path
- `created_by_app == "chat"` routes to chat path
- `created_by_app` absent (legacy) routes to chat path

### Unit — absent `handoff.md` fallback
Run `_build_chat_context()` against a fixture with no `handoff.md`. Assert: result is non-empty (recent turns present), no exception raised, synthetic preamble item present.

### Integration — `POST /sessions/{id}/resume` for a chat session
TestClient with a seeded session fixture on disk (`created_by_app == "chat"`, `handoff.md` present). Assert:
- Response shape: `{ client_secret: ..., context_to_inject: [...] }`
- `context_to_inject` is non-empty
- First item contains handoff preamble text

### Regression — voice-to-voice resume
All existing voice resume tests pass unchanged. The `created_by_app == "voice"` path is a pure refactor — behavior is identical.

### Manual Verification Checklist
- Select a chat session from SessionPicker → voice model opens with context summary referencing prior work
- Select a session where `handoff.md` is absent → graceful fallback, no crash, recent turns narrated
- Select a voice session → existing voice-to-voice resume behavior unchanged
- Let voice session hit proactive reconnect → same Amplifier session ID, context preserved

---

## Open Questions

**`recent_turns` default (8):** Proposed default. Needs validation against a real substantive chat session — does 8 turns give sufficient coherence without overwhelming the Realtime context budget? May need to be configurable per `created_by_app`.

**Transcript format variance:** Chat, CLI, and Slack apps may write transcripts with slightly different JSON structures. `_build_chat_context()` must normalize across all three. An audit of the three formats is a prerequisite for Phase 2.

**`SessionPicker` filter criteria:** "Has `handoff.md`" vs "any session" vs whitelist of `created_by_app` values. Start permissive (show all); narrow if the list becomes unwieldy in practice.

**Voice model system prompt:** The Realtime session's system prompt doesn't currently tell the model to expect injected prior-session context. A one-sentence addition — *"If context items from a prior session are present, briefly acknowledge what was being worked on before asking how to continue"* — would significantly improve the opening exchange. Decide whether this belongs in the system prompt template or as a synthetic `conversation.item` injected by the server.
