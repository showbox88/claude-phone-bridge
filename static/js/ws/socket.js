/**
 * WebSocket lifecycle: connect, reconnect with backoff, ping, send.
 *
 * Replaces the 4-line stub from Task 6. Reads/writes ws/reconnectDelay/
 * pingTimer/reconnectTimer via state store; dispatches incoming frames
 * to handlers.js.
 *
 * Ported from legacy app.js (lines 88-175). Preserves:
 *   - wsUrl() derived from currentSource (api.js) — returns null when no
 *     source, in which case setConn('disconnected') and bail.
 *   - 25 s ping interval (matches server timeout).
 *   - 1.6x exponential backoff capped at 8 s (matches legacy semantics).
 *   - Stale-socket guard (`sock !== ws`) on open/message/close — prevents
 *     a socket whose .close() is still pending from racing the new one.
 *   - On error: close THIS socket only, not the module-level ws.
 *   - visibilitychange listener: when the page returns to foreground,
 *     force-close any existing ws and reconnect — iOS keeps half-dead
 *     sockets reporting readyState=1 that no longer deliver messages.
 *   - sendWs() appends a "未连接，消息未发送" system message on failure.
 */
import { connDot } from '../dom.js';
import { get, set } from '../state.js';
import { wsUrl } from '../api.js';
import { appendSystem } from '../render/message.js';
import { dispatch } from './handlers.js';

export function setConn(state) {
  if (!connDot) return;
  connDot.className = 'dot ' + state;
  connDot.title = state;
}

function _clearPing() {
  const t = get('pingTimer');
  if (t) { clearInterval(t); set('pingTimer', null); }
}

function _startPing(sock) {
  _clearPing();
  set('pingTimer', setInterval(() => {
    if (sock.readyState === 1) sock.send(JSON.stringify({ type: 'ping' }));
  }, 25000));
}

export function connect() {
  // Skip if already connecting/open — prevents stacking multiple WS on
  // mobile when visibility changes fire repeated reconnect attempts.
  const existing = get('ws');
  if (existing && (existing.readyState === 0 || existing.readyState === 1)) return;

  const rt = get('reconnectTimer');
  if (rt) { clearTimeout(rt); set('reconnectTimer', null); }

  const url = wsUrl();
  if (!url) { setConn('disconnected'); return; }
  setConn('connecting');
  const sock = new WebSocket(url);
  set('ws', sock);

  sock.addEventListener('open', () => {
    if (sock !== get('ws')) { try { sock.close(); } catch (_) {} return; }
    setConn('connected');
    set('reconnectDelay', 500);
    _startPing(sock);
  });

  sock.addEventListener('message', (ev) => {
    // Drop messages from any stale socket that hasn't fully closed yet.
    if (sock !== get('ws')) return;
    let msg = null;
    try { msg = JSON.parse(ev.data); }
    catch (err) { console.warn('bad ws message', err); return; }
    dispatch(msg);
  });

  sock.addEventListener('close', () => {
    if (sock !== get('ws')) return; // a newer socket has already taken over
    setConn('disconnected');
    _clearPing();
    const delay = get('reconnectDelay') || 500;
    set('reconnectTimer', setTimeout(connect, delay));
    set('reconnectDelay', Math.min(delay * 1.6, 8000));
  });

  sock.addEventListener('error', () => {
    // Close THIS socket — never the module-level ws, which may already
    // point at a newer connection.
    try { sock.close(); } catch (_) { /* noop */ }
  });
}

// Force-reconnect (and drop stale sockets) when the page comes back to the
// foreground on mobile. iOS especially likes to keep a half-dead WS around
// that says readyState=1 but no longer delivers messages — which means we
// miss the permission_request that fired while we were backgrounded.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  if (!get('currentSource')) return;
  const sock = get('ws');
  if (sock) { try { sock.close(); } catch (_) {} set('ws', null); }
  connect();
});

export function sendWs(obj) {
  const sock = get('ws');
  if (!sock || sock.readyState !== 1) {
    appendSystem('未连接，消息未发送');
    return false;
  }
  sock.send(JSON.stringify(obj));
  return true;
}
