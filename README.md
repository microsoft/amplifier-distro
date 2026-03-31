# amplifier-distro

The complete distribution layer for [Amplifier](https://github.com/microsoft/amplifier). It handles the install and onboarding experience to get Amplifier working correctly, bundles a curated set of experience apps, and ships a set of optional capabilities users can enable as they go.

Everything in amplifier-distro is built around the [`AMPLIFIER_HOME_CONTRACT`](AMPLIFIER_HOME_CONTRACT.md) — the shared filesystem spec that lets any interface share sessions, memory, and state.

## Install

**Prerequisites:** `git`, [`gh`](https://cli.github.com) (authenticated), [`uv`](https://docs.astral.sh/uv/) (auto-installed if missing)

```bash
curl -fsSL https://raw.githubusercontent.com/microsoft/amplifier-distro/main/install.sh | bash
```

The installer verifies your environment, installs the `amp-distro` tool, and walks you through provider setup so Amplifier works immediately.

Then start the server:

```bash
amp-distro serve
```

Open [http://localhost:8410](http://localhost:8410) to begin.

## Update

```bash
uv tool upgrade amp-distro
```

Restart `amp-distro` after upgrading to use the new version.

## Experience Apps

Multiple front-ends into the same Amplifier runtime — sessions, memory, and context are shared across all of them.

| App | Description |
|-----|-------------|
| **Web Chat** | Browser-based chat with full session persistence |
| **Slack** | Full Slack bridge via Socket Mode |
| **Voice** | WebRTC voice via OpenAI Realtime API |
| **Routines** | Scheduled recipe execution |

Apps included in amplifier-distro conform to the [AMPLIFIER_HOME_CONTRACT](AMPLIFIER_HOME_CONTRACT.md). The set will grow over time.

## Capabilities

amplifier-distro ships an `amplifier-start` bundle with a set of conventions and capabilities users can opt into:

**Providers** — configure any combination:

| Provider | Key |
|----------|-----|
| Anthropic | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Google | `GOOGLE_API_KEY` |
| xAI | `XAI_API_KEY` |
| Azure OpenAI | `AZURE_OPENAI_API_KEY` |
| Ollama | `OLLAMA_HOST` (local) |

**Features** — opt-in capabilities:
- Persistent memory, planning mode
- Vector search, recipes, content studio, session discovery, routines

The onboarding experience guides setup. Capabilities can be enabled or changed at any time via `amp-distro serve`.

## Developer Install

```bash
git clone https://github.com/microsoft/amplifier-distro && cd amplifier-distro
cd distro-server
uv tool install -e .
```

## Commands

### `amp-distro serve` — Start the experience server

```bash
amp-distro serve                 # Foreground on http://localhost:8410
amp-distro serve --reload        # Auto-reload for development
```

### `amp-distro backup` / `restore` — State backup

```bash
amp-distro backup                # Back up ~/.amplifier/ state to GitHub
amp-distro restore               # Restore from backup
amp-distro backup --name my-bak  # Custom backup repo name
```

Uses a private GitHub repo (created automatically via `gh`). API keys are never backed up.

### `amp-distro service` — Auto-start on boot

```bash
amp-distro service install       # Register systemd/launchd service
amp-distro service uninstall     # Remove the service
amp-distro service status        # Check service status
```

## Docs

| File | Description |
|------|-------------|
| [AMPLIFIER_HOME_CONTRACT.md](AMPLIFIER_HOME_CONTRACT.md) | Filesystem contract all apps must conform to |
| [distro-server/docs/SLACK_SETUP.md](distro-server/docs/SLACK_SETUP.md) | Slack bridge setup guide |

## License

MIT — see [LICENSE](LICENSE).

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
