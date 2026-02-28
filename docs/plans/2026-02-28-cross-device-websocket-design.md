# Cross-Device WebSocket Connectivity Fix

## Goal
Fix WebSocket disconnections when accessing the amp-distro chat app from other devices on the network via `amp-distro serve --host 0.0.0.0`.

## Background
When a user starts the distro server with `--host 0.0.0.0` to listen on all network interfaces, browsers on other LAN devices connect with an Origin header like `http://192.168.x.x:8000`. The WebSocket auth handshake in `connection.py` (lines 97-117) has a hardcoded localhost-only origin allowlist, so these connections are rejected with close code `4003` ("Forbidden origin"). This makes cross-device usage impossible despite the server explicitly binding to all interfaces.

## Scope
Chat app WebSocket only. The voice app has the same origin-check issue but is out of scope for this change.

## Approach
Thread the `--host` value from the CLI through the server object chain to the WebSocket connection handler. When `host == "0.0.0.0"`, relax the origin check to allow any origin. This was chosen over environment variables (implicit coupling) and a configurable allowed-origins list (user burden) because the host value already exists at the CLI layer and threading it is a small, traceable change with no new config surface.

## Architecture
The host value flows through the existing object chain:

```
CLI (cli.py)
  → DistroServer / app construction (app.py)
    → Chat app setup
      → ChatConnection (connection.py)
        → _auth_handshake() origin check
```

No new modules or abstractions are introduced. The change piggybacks on the existing wiring between these components.

## Components

### 1. Host threading to origin check

**Files:** `cli.py`, `app.py`, `connection.py`

1. **CLI** (`cli.py`) passes the `host` value to `DistroServer` (or whatever constructs the app).
2. **DistroServer** (`app.py`) stores the host and makes it available to the chat app setup.
3. **Chat app** passes host into `ChatConnection` on each new WebSocket connection.
4. **`_auth_handshake()`** (`connection.py`) checks: if `host == "0.0.0.0"`, skip the origin restriction entirely. Otherwise, keep the existing localhost-only check unchanged.

**Rationale:** `--host 0.0.0.0` is an explicit user decision to accept LAN connections. Enforcing localhost-only origins after that contradicts the user's intent. When running the default `127.0.0.1`, the strict check stays in place -- no security regression.

### 2. Client-side reconnect fix

**File:** `index.html`

In `index.html`, the `ws.onclose` handler currently only skips reconnect for code `4001` (unauthorized). Code `4003` (forbidden origin) causes an infinite retry loop every 3 seconds.

**Fix:** Add `4003` to the skip-reconnect guard in the `onclose` callback. When the server rejects the origin, the client surfaces the disconnection and stops instead of hammering the server forever. This is a one-line change.

### 3. WebSocket keepalive via uvicorn ping

**File:** `cli.py`

Currently uvicorn is started with no `ws_ping_interval` or `ws_ping_timeout`. Over LAN, routers and NAT tables can silently drop idle connections.

**Fix:** Add `ws_ping_interval=20` and `ws_ping_timeout=20` to the `uvicorn.run()` call. This makes uvicorn send protocol-level WebSocket pings every 20 seconds. If no pong comes back within 20 seconds, the connection is considered dead and cleaned up.

This is transparent to application code -- no changes needed in the chat connection handler or client. It complements the existing application-level `ping`/`pong` JSON messages, which are client-initiated, while the uvicorn pings are server-initiated and operate at the WebSocket protocol layer.

## Data Flow

### Connection with `--host 0.0.0.0`
1. User starts server: `amp-distro serve --host 0.0.0.0`
2. CLI passes `host="0.0.0.0"` through DistroServer to ChatConnection
3. Browser on LAN device connects with `Origin: http://192.168.x.x:8000`
4. `_auth_handshake()` sees `host == "0.0.0.0"` and skips origin restriction
5. Connection proceeds normally

### Connection with default host
1. User starts server: `amp-distro serve` (defaults to `127.0.0.1`)
2. CLI passes `host="127.0.0.1"` through DistroServer to ChatConnection
3. Browser connects with `Origin: http://127.0.0.1:8000`
4. `_auth_handshake()` enforces localhost-only origin check as before
5. No behavior change from current code

## Error Handling
- **Forbidden origin with default host:** Server closes WebSocket with code `4003`. Client receives the close, sees `4003` in the skip-reconnect list, and stops retrying (new behavior -- previously it retried forever).
- **Dead LAN connections:** Uvicorn's ping/pong mechanism detects unresponsive clients within 40 seconds (20s interval + 20s timeout) and cleans up the connection server-side.

## Testing Strategy
- Manual test: start with `--host 0.0.0.0`, connect from another device on the LAN, verify WebSocket connects and chat works.
- Manual test: start with default host, verify localhost connections still work and non-localhost origins are still rejected.
- Manual test: connect from LAN device, kill the client, verify server cleans up the connection within ~40 seconds.
- Verify the client stops reconnecting on code `4003` by connecting with a spoofed origin to a localhost-bound server.

## Key Files
| File | Change |
|------|--------|
| `distro-server/src/amplifier_distro/server/server/cli.py` | Pass host to app, add uvicorn ping params |
| `distro-server/src/amplifier_distro/server/app.py` | Store and propagate host value |
| `distro-server/src/amplifier_distro/server/apps/chat/connection.py` | Conditional origin check in `_auth_handshake()` |
| `distro-server/src/amplifier_distro/server/apps/chat/static/index.html` | Add `4003` to skip-reconnect codes |

## Alternatives Considered
- **Environment variable:** Set `AMPLIFIER_SERVE_HOST=0.0.0.0` in CLI, read in connection handler. Simpler wiring but introduces implicit coupling between modules with no compile-time guarantee they agree on the variable name.
- **Configurable allowed-origins list:** Add `--allowed-origins` CLI flag for explicit origin URLs. Most flexible but puts burden on the user to know their device's IP -- defeats the simplicity of `--host 0.0.0.0`.

## Future Work
- Apply the same origin-expansion fix to the voice app (`voice/__init__.py:133-139`).
