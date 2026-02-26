#!/bin/bash
# Amplifier Distro - Install Script
#
# Single script used by all install paths:
#
#   Codespaces / devcontainer:
#     postCreateCommand: bash scripts/install.sh
#
#   Dockerfile:
#     RUN bash scripts/install.sh
#
#   curl | bash (standalone):
#     curl -fsSL https://raw.githubusercontent.com/ramparte/amplifier-distro/main/scripts/install.sh | bash
#
#   Local developer:
#     git clone ... && cd amplifier-distro && bash scripts/install.sh
#
# Behavior:
#   - If pyproject.toml exists in cwd → editable install (developer / devcontainer)
#   - Otherwise → clone repo and install as uv tools (standalone)

set -e

REPO_URL="https://github.com/ramparte/amplifier-distro"
AMPLIFIER_URL="https://github.com/microsoft/amplifier"
TUI_URL="https://github.com/ramparte/amplifier-tui"

# ── Ensure git is available ─────────────────────────────────────
ensure_git() {
    if command -v git &>/dev/null; then
        return
    fi
    echo "[install] ERROR: git is required but not installed."
    echo "  Install git and try again: https://git-scm.com/downloads"
    exit 1
}

# ── Ensure gh CLI is available ─────────────────────────────────
ensure_gh() {
    if command -v gh &>/dev/null; then
        return
    fi
    echo "[install] ERROR: GitHub CLI (gh) is required but not installed."
    echo "  Install it and try again: https://cli.github.com"
    exit 1
}

# ── Ensure uv is available ───────────────────────────────────────
ensure_uv() {
    if command -v uv &>/dev/null; then
        echo "[install] uv: $(uv --version)"
        return
    fi
    echo "[install] Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
}

# ── Editable install (source checkout) ───────────────────────────
install_editable() {
    echo "[install] Source checkout detected — editable install"
    echo ""

    echo "[1/3] Installing amplifier-distro (editable)..."
    if [ ! -d ".venv" ]; then
        uv venv
    fi
    export VIRTUAL_ENV="$PWD/.venv"
    export PATH="$PWD/.venv/bin:$PATH"
    uv pip install -e ".[all,dev]"

    # Symlink entry points into ~/.local/bin so they're on PATH after the script exits
    mkdir -p "$HOME/.local/bin"
    for cmd in amp-distro amp-distro-server; do
        if [ -f "$PWD/.venv/bin/$cmd" ]; then
            ln -sf "$PWD/.venv/bin/$cmd" "$HOME/.local/bin/$cmd"
            echo "[install] Linked $cmd → ~/.local/bin/$cmd"
        fi
    done

    echo ""
    if command -v amplifier &>/dev/null; then
        echo "[2/3] Amplifier CLI already installed — skipping (use 'amplifier update' to upgrade)"
    else
        echo "[2/3] Installing Amplifier CLI..."
        uv tool install --force "git+${AMPLIFIER_URL}"
    fi

    echo ""
    if command -v amplifier-tui &>/dev/null; then
        echo "[3/3] amplifier-tui already installed — skipping (reinstall with: uv tool install --force git+${TUI_URL})"
    else
        echo "[3/3] Installing amplifier-tui..."
        uv tool install --force "git+${TUI_URL}"
    fi
}

# ── Standalone install (no source checkout) ──────────────────────
install_standalone() {
    echo "[install] Standalone install — uv tools"
    echo ""

    echo "[1/3] Installing amplifier-distro..."
    uv tool install "git+${REPO_URL}"

    echo ""
    if command -v amplifier &>/dev/null; then
        echo "[2/3] Amplifier CLI already installed — skipping (use 'amplifier update' to upgrade)"
    else
        echo "[2/3] Installing Amplifier CLI..."
        uv tool install "git+${AMPLIFIER_URL}"
    fi

    echo ""
    if command -v amplifier-tui &>/dev/null; then
        echo "[3/3] amplifier-tui already installed — skipping (reinstall with: uv tool install --force git+${TUI_URL})"
    else
        echo "[3/3] Installing amplifier-tui..."
        uv tool install "git+${TUI_URL}"
    fi
}

# ── Main ─────────────────────────────────────────────────────────
echo "=== Amplifier Distro - Install ==="
echo ""

ensure_git
ensure_gh
ensure_uv
echo ""

if [ -f "pyproject.toml" ]; then
    install_editable
else
    install_standalone
fi

echo ""
echo "=== Install complete ==="
echo ""
echo "Commands available:"
echo "  amp-distro          Distro management CLI"
echo "  amp-distro-server   Web server (localhost:8400)"
echo "  amplifier           Amplifier CLI agent"
echo "  amplifier-tui       Terminal UI"
echo ""
echo "Quick start:"
echo "  amp-distro init       # first-time setup"
echo "  amp-distro-server     # start web UI"
echo ""
