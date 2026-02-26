# Amplifier Distro: Opinions

These are the shared conventions that every tool, interface, agent,
and workflow in the distro agrees to. They are deliberately opinionated.
The guiding principle: **a choice is better than many, even if you
don't agree.** Every opinion here exists to eliminate a decision that
would otherwise cost human attention.

---

## The One Rule

**Minimize human attentional load.**

Every opinion below derives from this. If an opinion doesn't reduce
the attention a human spends on plumbing, configuration, debugging,
or context-switching, it shouldn't be here.

---

## 1. Workspace

### The Opinion

All projects live under a single root directory.

```
~/dev/                          # The workspace root
  project-a/                    # A git repo
  project-b/                    # Another git repo
  amplifier-distro/             # This repo
  amplifier-tui/                # TUI interface
  amplifier-voice/              # Voice interface
  ...
```

### What This Means

- **Every tool knows where to find projects.** The voice interface,
  the TUI, the CLI, session discovery, team tracking - they all
  read `workspace_root` from one place and find everything.
- **Session-to-project mapping is automatic.** If you start a
  session in `~/dev/my-project/`, the distro knows it belongs
  to `my-project`. No manual tagging.
- **The default is `~/dev/`.** Configurable once at setup time.
  After that, never think about it again.

### The Convention

```yaml
# ~/.amplifier/distro.yaml
workspace_root: ~/dev
```

Every distro-aware tool reads this. Period.

---

## 2. Identity

### The Opinion

Your GitHub handle is your identity. One handle, everywhere.

### What This Means

- **Team tracking uses it.** Sessions are indexed by GitHub handle.
- **Commits use it.** Git config is set up with your GitHub email.
- **Memory uses it.** Your memory store is yours.
- **The distro detects it.** `gh auth status` tells us who you are.

### The Convention

```yaml
# ~/.amplifier/distro.yaml
identity:
  github_handle: samschillace
  git_email: sam@example.com
```

Set once at `amp distro init`. Never prompted again.

---

## 3. Memory

### The Opinion

One memory system, one location, one format. Every tool reads
and writes the same store.

```
~/.amplifier/memory/
  memory-store.yaml     # Facts, preferences, learnings
  work-log.yaml         # Active work, pending decisions
```

### What This Means

- **The CLI remembers what the TUI learned.** Session handoffs,
  preferences, project notes - all in one place.
- **Voice can query your memory.** "What was I working on?" works
  from any interface.
- **Agents share context.** The morning brief reads the same memory
  the friction detector writes to.
- **YAML, not a database.** Human-readable, git-trackable, grep-able.
  No daemon, no service, no migration.

### The Convention

Tools that want to remember things use the memory store.
Tools that want to recall things query the memory store.
The format is documented. The location is fixed.

---

## 4. Sessions

### The Opinion

Sessions are files. They live where Amplifier puts them. Every
interface creates sessions the same way.

```
~/.amplifier/projects/
  <project-slug>/
    <session-id>/
      transcript.jsonl
      events.jsonl
      session-info.json
      handoff.md            # Auto-generated at session end
```

### What This Means

- **Any interface can resume any session.** Start in CLI, continue
  in TUI, review in voice. Same session files, same state.
- **Handoffs are automatic.** When a session ends, a summary is
  written. When a session starts in the same project, the summary
  is injected.
- **Session discovery is filesystem-based.** No database, no service.
  `ls ~/.amplifier/projects/my-project/` shows all sessions.

### The Convention

All interfaces use the Interface Adapter to create sessions. The
adapter handles:
- Session creation with correct project mapping
- Event emission (SESSION_START, SESSION_END)
- Handoff generation on end
- Handoff injection on start

No interface implements session lifecycle itself.

---

## 5. Bundle Configuration

### The Opinion

One bundle per user. Validated before every session. Errors are
loud, not silent.

### What This Means

- **`amp distro init` creates a personal bundle** that inherits
  from the distro base. You customize by adding behaviors.
- **Pre-flight checks run before session start.** Missing API keys,
  broken includes, unresolvable sources - caught immediately, with
  clear error messages.
- **No silent failures.** A broken include is an error, not a
  warning you never see in a log.

### The Convention

```yaml
# ~/.amplifier/distro.yaml
bundle:
  active: my-amplifier
  validate_on_start: true
  strict: true
```

The distro base bundle provides: agents, memory, team tracking,
session handoffs, health checks. Your personal bundle adds:
your provider keys, your preferred models, your custom behaviors.

---

## 6. Interfaces

### The Opinion

Interfaces are viewports into the same system. They share state,
sessions, memory, and configuration. They do NOT have their own
isolated worlds.

### What Exists Today

| Interface | Repo | Status |
|-----------|------|--------|
| CLI | amplifier-app-cli | Production (reference) |
| TUI | amplifier-tui | In development |
| Voice | amplifier-voice | In development |
| GUI | amplifier-app-gui | Early |
| CarPlay | carplay | Prototype |

### What This Means

- **Start a session in CLI, continue in TUI.** Same project, same
  session files, same handoff.
- **Voice knows your workspace.** It reads `workspace_root`, not
  a hardcoded path.
- **Installing an interface is one command.** `amp distro install voice`
  clones the repo, installs deps, links config.
- **Every interface uses the Interface Adapter.** No sys.path hacks,
  no CLI subprocess spawning, no hardcoded provider configs.

### The Convention

Interface authors: use the Interface Adapter. Read `distro.yaml`
for workspace, identity, and bundle config. Write sessions to
the standard location. Emit standard events. That's it.

---

## 7. Providers

### The Opinion

API keys in environment variables. Provider config in your bundle.
The distro doesn't pick your LLM - but it ensures your keys work.

### What This Means

- **Pre-flight checks verify keys are set.** Not just "is
  ANTHROPIC_API_KEY set?" but "is it non-empty and plausible?"
- **Model names are in the bundle.** Centralized, not scattered
  across hardcoded Python files.
- **Multi-provider is assumed.** The distro base bundle supports
  anthropic + openai. You add others.

### The Convention

```bash
# In your shell profile
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

```yaml
# In your personal bundle
providers:
  - module: provider-anthropic
    config:
      default_model: claude-sonnet-4-20250514
  - module: provider-openai
    config:
      default_model: gpt-4o
```

Pre-flight validates at session start.

---

## 8. Updates and Cache

### The Opinion

Git-based updates. TTL-based cache. Auto-refresh on error.
You never manually clear cache.

### What This Means

- **Bundle sources are git repos.** When you include a behavior
  from a git URL, it's cloned to cache. The cache has a TTL.
- **Stale cache auto-refreshes.** If a cached source is older
  than the TTL, it re-clones on next session start. Silently.
- **Failed loads trigger cache invalidation.** If a module fails
  to load from cache, the cache entry is deleted and re-cloned.
  No "have you tried clearing your cache?" debugging.
- **`amplifier reset --remove cache`** exists for nuclear option.
  But you shouldn't need it.

### The Convention

```yaml
# ~/.amplifier/distro.yaml
cache:
  max_age_hours: 168
  auto_refresh_on_error: true
  auto_refresh_on_stale: true
```

---

## 9. Health and Diagnostics

### The Opinion

The distro monitors itself. Problems are surfaced before they
waste human attention.

### What This Means

- **Pre-flight on every session start.** Fast (<5 seconds). Checks
  keys, bundle, cache. Blocks if critical failure.
- **`amp distro status` anytime.** Shows environment health in
  one screen.
- **Friction detection (future).** Weekly analysis of sessions to
  identify attention drains. Surfaces fixes automatically.

### The Convention

Health is not optional. If you're using the distro, pre-flight
runs. You can make it non-blocking (`preflight: warn`), but you
can't disable it entirely. The whole point of the distro is that
things work reliably.

---

## 10. The Setup Website

### The Opinion

A static page at a known URL that any agent can read. Contains
machine-parseable setup instructions.

### What This Means

- **"Point an agent at this URL."** A new team member's first
  experience: open Amplifier, say "set me up", agent reads the
  URL and executes the setup.
- **The page is the source of truth.** What to install, what
  keys to get, what bundle to start with, what workspace to use.
- **Human-readable AND machine-readable.** A person can follow
  it manually. An agent can parse and execute it.

### The Convention

```
https://amplifier.dev/distro/setup
  (or a GitHub Pages equivalent)
```

Contains:
- Prerequisites (Python, Node, git, gh CLI)
- Provider key instructions
- `amp distro init` instructions
- Interface install instructions
- Troubleshooting

Structured so an agent can extract steps programmatically
(fenced code blocks with shell commands, clearly labeled
prerequisites, success criteria for each step).

---

## 11. Integration Credentials

### The Opinion

Integration secrets (Slack tokens, webhook URLs, service API keys) go
in `keys.yaml` alongside provider keys. Integration *config* (channel
names, modes, behavior settings) goes in `distro.yaml`. One pattern
for all integrations, no per-integration config files.

### What This Means

- **All secrets in one place.** `keys.yaml` is chmod 600, excluded
  from backup, and is the single file a user needs to protect. Slack
  bot tokens sit next to Anthropic API keys. One file to audit.
- **All config in one place.** `distro.yaml` already has identity,
  bundle, interfaces. Adding `slack:` (and future `teams:`, `discord:`)
  keeps everything in the same file that `amp distro status` reads.
- **No per-integration config files.** No `slack.yaml`, no `teams.yaml`.
  These fragment the config surface and force users to remember which
  file holds what. One secrets file, one config file.
- **Setup pages follow the quickstart pattern.** Each integration gets
  a setup page at `/static/<name>-setup.html` that guides token
  collection, validates against the service API, and persists to the
  standard locations. Same design tokens as quickstart.html.
- **Minimize user steps.** Provide a Slack App Manifest so app
  creation is paste-and-click. Auto-detect channels. Validate tokens
  live. The user should go from zero to working bridge in under 3
  minutes.

### The Convention

Secrets in `~/.amplifier/keys.yaml`:
```yaml
ANTHROPIC_API_KEY: sk-ant-...
SLACK_BOT_TOKEN: xoxb-...
SLACK_APP_TOKEN: xapp-...
```

Config in `~/.amplifier/distro.yaml`:
```yaml
slack:
  hub_channel_id: C07XXXXXXXX
  hub_channel_name: amplifier
  socket_mode: true
```

Setup page at: `/static/slack-setup.html`
Setup API at: `/apps/slack/setup/*`

---

## What These Opinions Replace

| Before (Many Choices) | After (One Choice) |
|------------------------|---------------------|
| Projects anywhere on disk | Projects in workspace_root |
| Memory in various locations | Memory in ~/.amplifier/memory/ |
| Each interface creates sessions differently | Interface Adapter, one way |
| Bundle errors are silent warnings | Bundle errors are loud failures |
| Cache cleared manually when things break | Cache auto-refreshes |
| Identity scattered across configs | github_handle is identity |
| Health checked when something breaks | Health checked every session |
| Setup is tribal knowledge | Setup is one URL, one command |

---

## Non-Opinions (Deliberately Left Open)

These are explicitly NOT standardized by the distro:

- **Which LLM provider you prefer.** Your choice.
- **Which interface you use day-to-day.** Your choice.
- **Which agents you compose.** Your bundle, your agents.
- **What workflows you run.** Your environment.yaml.
- **Whether you use team tracking.** Opt-in.
- **Whether you use cloud sync.** Opt-in (not built yet).
- **Your editor/IDE.** Not the distro's business.
- **Your shell.** bash, zsh, fish - all work.
