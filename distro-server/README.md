# amplifier-distro

The Amplifier Experience Server — web chat, Slack, voice, and more.

## What This Is

A server that hosts multiple interfaces to Amplifier sessions. It connects
browsers, Slack workspaces, and voice clients to the same Amplifier runtime,
with shared memory across all of them.

amplifier-distro is one part of a three-part setup:

| Component | Role |
|-----------|------|
| `amplifier` CLI | The tool — commands, doctor, init, sessions |
| `amplifier-start` bundle | The opinions — conventions, context, agents, hooks |
| `amplifier-distro` | The experiences — web chat, Slack, voice, routines |

## Install

```bash
uv tool install git+https://github.com/ramparte/amplifier-distro
```

With Slack support:

```bash
uv tool install "amplifier-distro[slack] @ git+https://github.com/ramparte/amplifier-distro"
```

### Developer

```bash
git clone https://github.com/ramparte/amplifier-distro && cd amplifier-distro
uv venv && uv pip install -e ".[dev,slack]"
```

## Usage

### `amp-distro serve` — Start the experience server

```bash
amp-distro serve                 # Foreground on http://localhost:8400
amp-distro serve --dev           # Dev mode (mock sessions, no LLM needed)
amp-distro serve --stub          # Stub mode (canned data, fast UI iteration)
amp-distro serve --reload        # Auto-reload for development
```

The server hosts web chat, Slack bridge, voice interface, and routines
scheduler. Visit http://localhost:8400/.

### `amp-distro backup` / `restore` — State backup

```bash
amp-distro backup                # Back up ~/.amplifier/ state to GitHub
amp-distro restore               # Restore from backup
amp-distro backup --name my-bak  # Custom backup repo name
```

Uses a private GitHub repo (created automatically via `gh` CLI).
API keys are never backed up.

### `amp-distro service` — Auto-start on boot

```bash
amp-distro service install       # Register systemd/launchd service
amp-distro service uninstall     # Remove the service
amp-distro service status        # Check service status
```

## Experience Apps

| App | Path | Description |
|-----|------|-------------|
| Web Chat | `/apps/web-chat/` | Browser-based chat with session persistence |
| Slack | `/apps/slack/` | Full Slack bridge via Socket Mode |
| Voice | `/apps/voice/` | WebRTC voice via OpenAI Realtime API |
| Routines | `/apps/routines/` | Scheduled routine execution |

Apps are auto-discovered from the `server/apps/` directory. Each is a FastAPI
router that registers with the server at startup.

## Configuration

The experience server reads configuration from the environment and
`~/.amplifier/keys.yaml`:

| Setting | Source |
|---------|--------|
| Provider API keys | `keys.yaml` (exported to env at server startup) |
| Voice model/voice | `AMPLIFIER_VOICE_MODEL`, `AMPLIFIER_VOICE_VOICE` env vars |
| Server API key | `AMPLIFIER_SERVER_API_KEY` env var |
| Workspace root | `AMPLIFIER_WORKSPACE_ROOT` env var |
| Slack tokens | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` env vars |

## Architecture

```
amp-distro serve
  │
  ├─ FastAPI core (/api/health, /api/sessions, /api/bridge, /api/memory)
  │
  ├─ FoundationBackend
  │    └─ amplifier-foundation: load_bundle() → prepare() → create_session()
  │
  └─ Apps (auto-discovered)
       ├─ web-chat   (browser sessions via HTTP)
       ├─ slack      (Slack workspace via Socket Mode)
       ├─ voice      (WebRTC via OpenAI Realtime API)
       └─ routines   (scheduled YAML-driven execution)
```

The server creates sessions through `amplifier-foundation` directly. Each
experience app adapts the session protocol for its transport (HTTP, WebSocket,
Slack events, WebRTC). A shared `FoundationBackend` manages the session pool
with per-session FIFO queues for safe concurrent access.

## Documents

| File | Description |
|------|-------------|
| [docs/plans/](docs/plans/) | Implementation plans for server features |
| [docs/SLACK_SETUP.md](docs/SLACK_SETUP.md) | Slack bridge setup guide |
| [planning/](planning/) | Historical research and architecture notes |
