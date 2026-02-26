# Slack Bridge Setup Guide

Connect your Amplifier distro server to Slack so you can interact with
Amplifier sessions from any Slack channel.

## Architecture

```
Slack workspace                    Your server (Tailscale / LAN)
 +--------------+                  +-------------------------+
 | #amplifier   | --- events --->  | Distro Server (FastAPI)  |
 | (hub channel)|                  |   /apps/slack/events     |
 |              | <-- messages --  |   SlackSessionManager    |
 | amp-proj-x   |                  |   -> Bridge API          |
 | (breakout)   |                  |   -> amplifier-foundation|
 +--------------+                  +-------------------------+
```

Two connection modes:

| Mode | How it works | When to use |
|------|-------------|-------------|
| **Events API** | Slack POSTs to your server | Server has a public/Tailscale URL |
| **Socket Mode** | Your server opens WebSocket to Slack | No public URL (dev/testing) |

---

## Step 1: Create a Slack App

1. Go to <https://api.slack.com/apps>
2. Click **Create New App** -> **From scratch**
3. Name it `Amplifier` (or whatever you like)
4. Select your workspace
5. Click **Create App**

You are now on the app's **Basic Information** page. Keep this tab open.

## Step 2: Configure Bot Scopes

1. In the left sidebar, click **OAuth & Permissions**
2. Scroll to **Scopes** -> **Bot Token Scopes**
3. Add these scopes:

| Scope | Purpose |
|-------|---------|
| `app_mentions:read` | Detect @mentions of the bot |
| `channels:history` | Read messages in public channels |
| `channels:manage` | Create breakout channels |
| `channels:read` | List channels |
| `chat:write` | Send messages and replies |
| `groups:history` | Read messages in private channels (optional) |
| `groups:write` | Create private breakout channels (optional) |
| `reactions:write` | Add reaction emojis (thinking indicator) |
| `users:read` | Look up user display names |

## Step 3: Install App to Workspace

1. Still on **OAuth & Permissions**, scroll up
2. Click **Install to Workspace**
3. Review the permissions and click **Allow**
4. Copy the **Bot User OAuth Token** (`xoxb-...`) -- you will need this

## Step 4: Get the Signing Secret

1. Go back to **Basic Information** (left sidebar)
2. Under **App Credentials**, find **Signing Secret**
3. Click **Show** and copy it

## Step 5: Enable Events API (if using Events API mode)

> Skip this step if you plan to use Socket Mode (Step 5b).

1. In the left sidebar, click **Event Subscriptions**
2. Toggle **Enable Events** to ON
3. In **Request URL**, enter:
   ```
   https://<your-server>/apps/slack/events
   ```
   Replace `<your-server>` with your Tailscale hostname or public URL,
   e.g. `https://my-machine.tail12345.ts.net:8400/apps/slack/events`
4. Slack will send a challenge request -- your server must be running
   (see Step 7) for verification to succeed
5. Under **Subscribe to bot events**, add:

| Event | Purpose |
|-------|---------|
| `app_mention` | When someone @mentions the bot |
| `message.channels` | Messages in public channels |
| `message.groups` | Messages in private channels (optional) |

6. Click **Save Changes**

## Step 5b: Enable Socket Mode (alternative, no public URL)

> Use this if your server is not publicly reachable.

1. In the left sidebar, click **Socket Mode**
2. Toggle **Enable Socket Mode** to ON
3. You will be prompted to create an **App-Level Token**
4. Name it `amplifier-socket` and add the scope `connections:write`
5. Click **Generate**
6. Copy the token (`xapp-...`) -- you will need this
7. Still enable the bot events from Step 5, point 5 (the events
   themselves are the same, they just arrive via WebSocket instead of HTTP)

## Step 6: Create the Hub Channel

1. In your Slack workspace, create a channel called `#amplifier`
   (or whatever name you prefer)
2. Invite the bot to the channel: `/invite @Amplifier`
3. Get the channel ID:
   - Right-click the channel name -> **View channel details**
   - The ID is at the bottom of the details pane (starts with `C`)

## Step 7: Configure Environment Variables

Create a `.env` file in your distro project root (or export directly):

```bash
# Required
export SLACK_BOT_TOKEN="xoxb-your-bot-token-here"
export SLACK_SIGNING_SECRET="your-signing-secret-here"
export SLACK_HUB_CHANNEL_ID="C0123456789"

# Optional
export SLACK_HUB_CHANNEL_NAME="amplifier"

# For Socket Mode (instead of Events API)
# export SLACK_SOCKET_MODE="true"
# export SLACK_APP_TOKEN="xapp-your-app-token-here"
```

## Step 8: Start the Server

```bash
cd amplifier-distro

# Install dependencies
pip install -e .

# Source your env vars
source .env

# Start the server
amp-distro-server --port 8400
```

The server will log:
```
Slack bridge initialized (mode: events-api)
```
or `(mode: socket)` if using Socket Mode.

## Step 9: Verify

### From the server

```bash
# Health check
curl http://localhost:8400/api/health

# Bridge status
curl http://localhost:8400/apps/slack/status
```

Expected response:
```json
{
  "status": "ok",
  "mode": "events-api",
  "hub_channel": "amplifier",
  "active_sessions": 0,
  "is_configured": true
}
```

### From Slack

In the `#amplifier` channel, type:
```
@Amplifier help
```

The bot should respond with the command list.

### Other commands to try

```
@Amplifier list          # List recent local sessions
@Amplifier projects      # List known projects
@Amplifier new           # Start a new session (creates a thread)
@Amplifier status        # Show bridge status
```

---

## Simulator Mode (No Slack Account Needed)

For development and testing without a real Slack workspace:

```bash
export SLACK_SIMULATOR_MODE="true"
amp-distro-server --port 8400
```

Then open `http://localhost:8400/apps/slack/simulator` in your browser
for a simulated Slack UI, or use the API directly:

```bash
# Send a message as if from Slack
curl -X POST http://localhost:8400/apps/slack/events \
  -H "Content-Type: application/json" \
  -d '{
    "type": "event_callback",
    "event": {
      "type": "app_mention",
      "text": "<@U_BOT> help",
      "user": "U_TEST_USER",
      "channel": "C_HUB",
      "ts": "1234567890.000001"
    }
  }'
```

---

## Environment Variable Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SLACK_BOT_TOKEN` | Yes | -- | Bot OAuth token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Events API | -- | Request signature verification |
| `SLACK_APP_TOKEN` | Socket Mode | -- | App-level token (`xapp-...`) |
| `SLACK_HUB_CHANNEL_ID` | Yes | -- | Channel ID for the hub |
| `SLACK_HUB_CHANNEL_NAME` | No | `amplifier` | Hub channel display name |
| `SLACK_SIMULATOR_MODE` | No | `false` | Run without real Slack |
| `SLACK_SOCKET_MODE` | No | `false` | Use Socket Mode |

## Tailscale Setup (Recommended for Production)

If your distro server runs on a machine in your Tailscale network:

1. Install Tailscale on the server machine
2. The server is reachable at `https://<hostname>.<tailnet>.ts.net:8400`
3. Use that as your Events API Request URL
4. Tailscale handles TLS and authentication -- no port forwarding needed

For HTTPS (required by Slack Events API), either:
- Use Tailscale's built-in HTTPS: `tailscale cert <hostname>`
- Put a reverse proxy (caddy, nginx) in front of the distro server

## Troubleshooting

### "Invalid signature" errors on events

- Verify `SLACK_SIGNING_SECRET` matches the value in your app's Basic Information
- Check that your server clock is accurate (Slack rejects requests >5 min old)
- In simulator mode, signature verification is skipped

### Bot does not respond to messages

1. Check the bot is invited to the channel (`/invite @Amplifier`)
2. Verify the event subscriptions include `app_mention`
3. Check server logs for incoming event payloads
4. Ensure `SLACK_HUB_CHANNEL_ID` matches the actual channel ID

### "url_verification" failure when setting Request URL

- Your server must be running and reachable from the internet
- Slack sends a POST with `{"type": "url_verification", "challenge": "..."}`
- The server must respond with the challenge value (this is handled automatically)

### Socket Mode connection issues

- Ensure `SLACK_APP_TOKEN` starts with `xapp-`
- The app-level token needs the `connections:write` scope
- Check that Socket Mode is enabled in the app settings

### Breakout channels not created

- The bot needs `channels:manage` scope
- Channel names must be lowercase, no spaces, max 80 chars
- Check for name conflicts (Slack rejects duplicate channel names)
