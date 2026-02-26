/**
 * Tests for ConnectionHealthManager (Task 5.5)
 * Run with: node --test src/amplifier_distro/server/apps/voice/static/connection-health.test.mjs
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

// Import the class under test
import { ConnectionHealthManager } from './connection-health.mjs';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Returns a manager with startSession() already called */
function freshManager(strategy = 'manual') {
  const m = new ConnectionHealthManager({ strategy });
  m.startSession();
  return m;
}

/** Overwrite the internal timestamp so we can simulate time passing */
function rewindTime(manager, msAgo) {
  manager._sessionStart = Date.now() - msAgo;
  manager._lastEventTime = Date.now() - msAgo;
  manager._lastSpeechTime = Date.now() - msAgo;
}

// ---------------------------------------------------------------------------
// ConnectionHealthManager — construction & initial state
// ---------------------------------------------------------------------------

describe('ConnectionHealthManager — construction', () => {
  test('default strategy is manual', () => {
    const m = new ConnectionHealthManager();
    assert.equal(m.strategy, 'manual');
  });

  test('accepts strategy in constructor', () => {
    const m = new ConnectionHealthManager({ strategy: 'proactive' });
    assert.equal(m.strategy, 'proactive');
  });

  test('initial state has no session', () => {
    const m = new ConnectionHealthManager();
    assert.equal(m._sessionStart, null);
  });
});

// ---------------------------------------------------------------------------
// startSession / recordEvent / recordSpeech
// ---------------------------------------------------------------------------

describe('ConnectionHealthManager — recording', () => {
  test('startSession sets sessionStart, lastEventTime, lastSpeechTime', () => {
    const before = Date.now();
    const m = new ConnectionHealthManager();
    m.startSession();
    const after = Date.now();
    assert.ok(m._sessionStart >= before);
    assert.ok(m._sessionStart <= after);
    assert.ok(m._lastEventTime >= before);
    assert.ok(m._lastSpeechTime >= before);
  });

  test('recordEvent updates lastEventTime', async () => {
    const m = freshManager();
    const t0 = m._lastEventTime;
    await new Promise(r => setTimeout(r, 10));
    m.recordEvent();
    assert.ok(m._lastEventTime > t0);
  });

  test('recordSpeech updates lastSpeechTime', async () => {
    const m = freshManager();
    const t0 = m._lastSpeechTime;
    await new Promise(r => setTimeout(r, 10));
    m.recordSpeech();
    assert.ok(m._lastSpeechTime > t0);
  });
});

// ---------------------------------------------------------------------------
// getStatus()
// ---------------------------------------------------------------------------

describe('ConnectionHealthManager — getStatus()', () => {
  test('returns sessionAge in seconds', () => {
    const m = freshManager();
    // Set session start to 10 seconds ago
    m._sessionStart = Date.now() - 10_000;
    const { sessionAge } = m.getStatus();
    assert.ok(sessionAge >= 9 && sessionAge <= 11, `Expected ~10s, got ${sessionAge}`);
  });

  test('returns timeSinceEvent in seconds', () => {
    const m = freshManager();
    m._lastEventTime = Date.now() - 35_000;
    const { timeSinceEvent } = m.getStatus();
    assert.ok(timeSinceEvent >= 34 && timeSinceEvent <= 36, `Expected ~35s, got ${timeSinceEvent}`);
  });

  test('returns timeSinceSpeech in seconds', () => {
    const m = freshManager();
    m._lastSpeechTime = Date.now() - 125_000;
    const { timeSinceSpeech } = m.getStatus();
    assert.ok(timeSinceSpeech >= 124 && timeSinceSpeech <= 126, `Expected ~125s, got ${timeSinceSpeech}`);
  });

  test('returns reconnectCount', () => {
    const m = freshManager();
    m.reconnectCount = 3;
    const { reconnectCount } = m.getStatus();
    assert.equal(reconnectCount, 3);
  });

  test('no warnings when everything is fresh', () => {
    const m = freshManager();
    const { warnings } = m.getStatus();
    assert.deepEqual(warnings, []);
  });

  test('warns stale when timeSinceEvent > 30s', () => {
    const m = freshManager();
    m._lastEventTime = Date.now() - 31_000;
    const { warnings } = m.getStatus();
    assert.ok(warnings.includes('stale'), `Expected stale warning, got: ${warnings}`);
  });

  test('no stale warning when timeSinceEvent exactly 30s', () => {
    const m = freshManager();
    m._lastEventTime = Date.now() - 30_000;
    const { warnings } = m.getStatus();
    assert.ok(!warnings.includes('stale'), `Should not warn stale at exactly 30s`);
  });

  test('warns idle when timeSinceSpeech > 120s', () => {
    const m = freshManager();
    m._lastSpeechTime = Date.now() - 121_000;
    const { warnings } = m.getStatus();
    assert.ok(warnings.includes('idle'), `Expected idle warning, got: ${warnings}`);
  });

  test('no idle warning when timeSinceSpeech exactly 120s', () => {
    const m = freshManager();
    m._lastSpeechTime = Date.now() - 120_000;
    const { warnings } = m.getStatus();
    assert.ok(!warnings.includes('idle'), `Should not warn idle at exactly 120s`);
  });

  test('warns session_limit when sessionAge > 55 minutes', () => {
    const m = freshManager();
    m._sessionStart = Date.now() - (55 * 60 * 1000 + 1000); // 55min + 1s ago
    const { warnings } = m.getStatus();
    assert.ok(warnings.includes('session_limit'), `Expected session_limit warning, got: ${warnings}`);
  });

  test('no session_limit warning when sessionAge exactly 55 minutes', () => {
    const m = freshManager();
    m._sessionStart = Date.now() - (55 * 60 * 1000);
    const { warnings } = m.getStatus();
    assert.ok(!warnings.includes('session_limit'), `Should not warn at exactly 55min`);
  });

  test('can have multiple warnings at once', () => {
    const m = freshManager();
    m._lastEventTime = Date.now() - 31_000;    // stale
    m._lastSpeechTime = Date.now() - 121_000;  // idle
    const { warnings } = m.getStatus();
    assert.ok(warnings.includes('stale'));
    assert.ok(warnings.includes('idle'));
  });

  test('returns null sessionAge when session not started', () => {
    const m = new ConnectionHealthManager();
    const { sessionAge } = m.getStatus();
    assert.equal(sessionAge, null);
  });
});

// ---------------------------------------------------------------------------
// inferDisconnectReason()
// ---------------------------------------------------------------------------

describe('ConnectionHealthManager — inferDisconnectReason()', () => {
  test('returns session_limit when sessionAge >= 58 minutes', () => {
    const m = freshManager();
    const reason = m.inferDisconnectReason(58 * 60);
    assert.equal(reason, 'session_limit');
  });

  test('returns session_limit at exactly 58 minutes', () => {
    const m = freshManager();
    const reason = m.inferDisconnectReason(58 * 60);
    assert.equal(reason, 'session_limit');
  });

  test('returns idle_timeout when no speech for 2+ minutes (120s)', () => {
    const m = freshManager();
    m._lastSpeechTime = Date.now() - 121_000; // 121s ago
    const reason = m.inferDisconnectReason(10 * 60); // 10 min session, not at limit
    assert.equal(reason, 'idle_timeout');
  });

  test('returns network_error when not session_limit and not idle', () => {
    const m = freshManager();
    m._lastSpeechTime = Date.now() - 30_000; // only 30s, not idle
    const reason = m.inferDisconnectReason(5 * 60); // 5 min session
    assert.equal(reason, 'network_error');
  });

  test('session_limit takes priority over idle', () => {
    const m = freshManager();
    m._lastSpeechTime = Date.now() - 121_000; // would be idle
    const reason = m.inferDisconnectReason(59 * 60); // but session is >= 58min
    assert.equal(reason, 'session_limit');
  });
});

// ---------------------------------------------------------------------------
// strategy management
// ---------------------------------------------------------------------------

describe('ConnectionHealthManager — strategy', () => {
  test('strategy can be updated', () => {
    const m = new ConnectionHealthManager({ strategy: 'manual' });
    m.strategy = 'proactive';
    assert.equal(m.strategy, 'proactive');
  });

  test('valid strategy values: manual, auto_immediate, auto_delayed, proactive', () => {
    const valid = ['manual', 'auto_immediate', 'auto_delayed', 'proactive'];
    for (const s of valid) {
      const m = new ConnectionHealthManager({ strategy: s });
      assert.equal(m.strategy, s);
    }
  });
});
