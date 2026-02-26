# Amplifier Distro
#
# Single image for both local testing and cloud deployment.
# Includes the distro server, Amplifier CLI, and TUI.
#
# Build:  docker build -t amplifier-distro .
# Run:    docker run -p 8400:8400 amplifier-distro
# Shell:  docker run -it amplifier-distro bash
# TUI:    docker run -it amplifier-distro amplifier-tui
#
# GitHub auth (pass your local gh token into the container):
#   docker run -e GH_TOKEN=$(gh auth token) -p 8400:8400 amplifier-distro

FROM python:3.12-slim

# curl for healthcheck, git for backup/clone/install, gh for GitHub API
RUN apt-get update && apt-get install -y --no-install-recommends curl git \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# uv for fast installs (same script as devcontainer and curl|bash)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Non-root user
RUN useradd -m -s /bin/bash amplifier \
    && mkdir -p /home/amplifier/dev /app \
    && chown -R amplifier:amplifier /home/amplifier /app

# Copy source and shared install script
COPY --chown=amplifier:amplifier pyproject.toml /app/
COPY --chown=amplifier:amplifier src/ /app/src/
COPY --chown=amplifier:amplifier scripts/install.sh /app/scripts/install.sh

# Install everything as the runtime user (all lands in ~/.local)
USER amplifier
WORKDIR /app
RUN bash scripts/install.sh
ENV PATH="/app/.venv/bin:/home/amplifier/.local/bin:${PATH}"

# Entrypoint (needs root to copy, then switch back)
USER root
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER amplifier
ENV HOME=/home/amplifier

EXPOSE 8400

HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=15s \
    CMD curl -sf http://localhost:8400/api/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["amp-distro-server", "--host", "0.0.0.0", "--port", "8400"]
