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

See [Distro README](../README.md) for the normal install.

For **development**, you can install in "editable" mode:

```bash
git clone https://github.com/microsoft/amplifier-distro && cd amplifier-distro
uv venv && uv pip install -e .
```

## Usage

### `amp-distro serve` — Start the experience server

```bash
amp-distro serve                 # Foreground on http://localhost:8400
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

## Documents

| File | Description |
|------|-------------|
| [docs/SLACK_SETUP.md](docs/SLACK_SETUP.md) | Slack bridge setup guide |
