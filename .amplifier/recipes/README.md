# Amplifier Distro — E2E Test Recipes

This directory contains Amplifier-native end-to-end tests for distro surfaces.
These complement the pytest unit tests in `tests/` — they test things pytest
cannot: real browsers, live Slack interactions, and full session flows.

## The pattern

```
recipe (.yaml)
  └── stage 1: server-check          (bash — fast fail if server is down)
  └── stage 2: chat-tests            (browser-tester:browser-operator — /apps/chat/)
        ├── step 1: chat-e2e         (16 single-surface scenarios)
        ├── step 2: chat-multi-surface-e2e  (3 cross-surface scenarios, 2 browser sessions)
        └── approval gate            (human confirms Slack login)
  └── stage 3: slack-tests           (browser-tester:browser-operator)
  └── stage 4: validate              (recipes:result-validator → PASS/FAIL table)
```

Each surface gets its own stage. Each stage uses `browser-tester:browser-operator`
with `--headed` (visible browser) and `--session <name>` (isolated context).
An approval gate handles any step that needs human action (e.g. Slack login).
`result-validator` produces the final structured verdict.

## Recipes

| File | What it tests | Scenarios |
|---|---|---|
| `e2e-browser-tests.yaml` | `/apps/chat/` + cross-surface awareness + Slack bridge | 16 chat + 3 cross-surface + 8 Slack = 27 total |

### Chat scenarios (S01–S16 + S17A–C)

| Group | Scenarios | What's covered |
|---|---|---|
| Page load & connection | S01–S02 | Title, WS status dot, draft state, CWD editor |
| Messaging & streaming | S03–S05 | User bubble, streaming cursor, markdown, Stop/cancel, Shift+Enter |
| Session metadata | S06 | sid, absolute CWD, turn count |
| Session history | S07–S09 | Transcript load, filter (text + sid:), sort time↔dir |
| New session & CWD | S10–S11 | + New draft state, CWD preference via `fill` + page reload |
| Slash commands | S12–S15 | Popup + arrow nav, /help, /clear, /workspace toggle |
| UI features | S16 | Theme toggle dark↔light + localStorage persistence |
| Cross-surface awareness | S17A–C | New badge (focus event), Updated badge (revision poll), "New messages ↓" pill |

### Slack scenarios (S1–S8)

Core messaging lifecycle, session connect/resume, thread memory recall, `--dir` custom
CWD, and default CWD from `settings.yaml`.

## Prerequisites

```bash
# 1. Server must be running
amp-distro-server start --port 8400

# 2. agent-browser must be installed
npm install -g agent-browser && agent-browser install
```

## Run the full suite

```bash
# From the amplifier-distro project directory
amplifier tool invoke recipes \
  operation=execute \
  recipe_path=".amplifier/recipes/e2e-browser-tests.yaml"
```

Or from within an Amplifier session:

```
run the e2e-browser-tests recipe
```

### Override defaults

```bash
amplifier tool invoke recipes \
  operation=execute \
  recipe_path=".amplifier/recipes/e2e-browser-tests.yaml" \
  context='{"server_url": "http://127.0.0.1:9000", "slack_channel": "my-channel"}'
```

## Approval gate (Slack login)

The recipe pauses after chat tests. When you see the approval prompt:

1. The headed browser will open `amplifiercrew.slack.com` automatically
2. If already logged in, the Slack tests run on their own
3. If a login page appears, log in through that browser window — credentials are
   saved to `~/.agent-browser/profiles/slack-distro-test` for future runs

```bash
# See pending approvals
amplifier tool invoke recipes operation=approvals

# Approve
amplifier tool invoke recipes operation=approve \
  session_id=<session-id> \
  stage_name="chat-tests"

# Resume
amplifier tool invoke recipes operation=resume \
  session_id=<session-id>
```

## Tail logs while tests run

```bash
tail -f ~/.amplifier/server/server.log \
  | jq -r '[.timestamp[11:19], .level, .message] | join(" | ")'
```

## Adding new test scenarios

**Adding a scenario to an existing surface:**
Edit the `prompt:` block in the relevant stage. Follow the existing format:

```
================================================================
SCENARIO N — Short Name
================================================================
Action  : What the browser operator should do
Verify  : What constitutes a pass
Screenshots: sc-N-name.png
PASS → evidence of success
FAIL → evidence of failure (include actual value)
```

Add the new scenario to the `FINAL REPORT` block and to the acceptance
criteria in the `validate` stage prompt.

**Adding a new surface (e.g. voice, install-wizard):**

1. Add a new stage to the recipe following the same structure
2. Add an approval gate if the surface requires any manual setup
3. Add acceptance criteria to the `validate` stage
4. Update the summary table in this README

## Known limitations

| Scenario | Status | Notes |
|---|---|---|
| S04 Stop/Cancel | SKIP-expected | LLM completes before cancellation propagates. The Stop button and WS signal are wired correctly — this is a timing ceiling, not a functional gap. |
| S17B Updated badge | FAIL on new sessions | Revision polling updates `localRevision` internally (confirmed), but the amber badge doesn't render on cards in `turn N` format (only on `N msgs` format). Likely a rendering-path bug — filed for investigation. |
