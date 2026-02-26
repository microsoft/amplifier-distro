# amplifier-distro

Monorepo for Amplifier distro components.

## Projects

## distro server

The [Amplifier Experience Server](distro-server/) — web chat, Slack, voice, and more.

A server that hosts multiple interfaces to Amplifier sessions. It connects
browsers, Slack workspaces, and voice clients to the same Amplifier runtime,
with shared memory across all of them.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/microsoft/amplifier-distro/main/install.sh | bash
```

### Developer

```bash
git clone https://github.com/microsoft/amplifier-distro && cd amplifier-distro
cd distro-server
uv tool install -e .
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
| Web Chat | `/apps/chat/` | Browser-based chat with session persistence |
| Slack | `/apps/slack/` | Full Slack bridge via Socket Mode |
| Voice | `/apps/voice/` | WebRTC voice via OpenAI Realtime API |

## Documents

| File | Description |
|------|-------------|
| [distro-server/docs/SLACK_SETUP.md](docs/SLACK_SETUP.md) | Slack bridge setup guide |
