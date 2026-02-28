/**
 * ConnectionHealthManager — pure logic, no UI (Task 5.5)
 *
 * Tracks WebRTC session health: session age, event recency, speech
 * recency, disconnect history, reconnect count, and reconnect strategy.
 */
class ConnectionHealthManager {
  /**
   * @param {{ strategy?: 'manual'|'auto_immediate'|'auto_delayed'|'proactive' }} options
   */
  constructor({ strategy = 'manual' } = {}) {
    this.strategy = strategy;
    this.reconnectCount = 0;
    this.disconnectHistory = [];

    // Internal timestamps (null until startSession is called)
    this._sessionStart = null;
    this._lastEventTime = null;
    this._lastSpeechTime = null;
  }

  // -------------------------------------------------------------------------
  // Lifecycle
  // -------------------------------------------------------------------------

  /** Call when a new session starts (or reconnects). */
  startSession() {
    const now = Date.now();
    this._sessionStart = now;
    this._lastEventTime = now;
    this._lastSpeechTime = now;
  }

  // -------------------------------------------------------------------------
  // Recording
  // -------------------------------------------------------------------------

  /** Record that a realtime event was received (heartbeat for staleness check). */
  recordEvent() {
    this._lastEventTime = Date.now();
  }

  /** Record that the user or assistant produced speech. */
  recordSpeech() {
    this._lastSpeechTime = Date.now();
  }

  // -------------------------------------------------------------------------
  // Status
  // -------------------------------------------------------------------------

  /**
   * Returns a snapshot of current session health.
   * @returns {{
   *   sessionAge: number|null,   // seconds, null if no session
   *   timeSinceEvent: number,    // seconds
   *   timeSinceSpeech: number,   // seconds
   *   warnings: string[],        // 'stale'|'idle'|'session_limit'
   *   reconnectCount: number,
   * }}
   */
  getStatus() {
    const now = Date.now();

    const sessionAge =
      this._sessionStart !== null ? (now - this._sessionStart) / 1000 : null;
    const timeSinceEvent =
      this._lastEventTime !== null ? (now - this._lastEventTime) / 1000 : 0;
    const timeSinceSpeech =
      this._lastSpeechTime !== null ? (now - this._lastSpeechTime) / 1000 : 0;

    const warnings = [];
    if (timeSinceEvent > 30) warnings.push('stale');
    if (timeSinceSpeech > 120) warnings.push('idle');
    if (sessionAge !== null && sessionAge > 55 * 60) warnings.push('session_limit');

    return {
      sessionAge,
      timeSinceEvent,
      timeSinceSpeech,
      warnings,
      reconnectCount: this.reconnectCount,
    };
  }

  // -------------------------------------------------------------------------
  // Disconnect reason inference
  // -------------------------------------------------------------------------

  /**
   * Infer why the session disconnected.
   * @param {number} sessionAge  Session age in seconds at time of disconnect.
   * @returns {'session_limit'|'idle_timeout'|'network_error'}
   */
  inferDisconnectReason(sessionAge) {
    if (sessionAge >= 58 * 60) return 'session_limit';

    const timeSinceSpeech =
      this._lastSpeechTime !== null
        ? (Date.now() - this._lastSpeechTime) / 1000
        : Infinity;
    if (timeSinceSpeech >= 120) return 'idle_timeout';

    return 'network_error';
  }
}
