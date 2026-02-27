# amplifier-distro

Run [Amplifier](https://github.com/microsoft/amplifier) across web, Slack, and voice — all sharing the same agent runtime and memory.

`amplifier-distro` is a hosting layer that connects multiple front-end experiences to a single Amplifier session backend. Chat in a browser, pick up the conversation in Slack, or switch to a voice call — same context, same agent, everywhere.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/microsoft/amplifier-distro/main/install.sh | bash
```

Then start the server:

```bash
amp-distro serve
```

Open [http://localhost:8400](http://localhost:8400) to start chatting.

## Developer Install

```bash
git clone https://github.com/microsoft/amplifier-distro && cd amplifier-distro
cd distro-server
uv tool install -e .
```

## Experience Apps

| App | Path | Description |
|-----|------|-------------|
| Web Chat | `/apps/chat/` | Browser-based chat with session persistence |
| Slack | `/apps/slack/` | Full Slack bridge via Socket Mode |
| Voice | `/apps/voice/` | WebRTC voice via OpenAI Realtime API |

## Commands

### `amp-distro serve` — Start the experience server

```bash
amp-distro serve                 # Foreground on http://localhost:8400
amp-distro serve --reload        # Auto-reload for development
```

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

## Docs

| File | Description |
|------|-------------|
| [distro-server/docs/SLACK_SETUP.md](distro-server/docs/SLACK_SETUP.md) | Slack bridge setup guide |

## License

MIT — see [LICENSE](LICENSE).
