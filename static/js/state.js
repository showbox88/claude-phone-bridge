/**
 * Minimal pub-sub state store.
 *
 * Replaces the ~14 module-scoped `let`s in the legacy IIFE app.js for
 * app-wide state (currentSessionId, currentMode, autoApprove, ws, etc.).
 *
 * Usage:
 *   import { get, set, subscribe } from './state.js';
 *   const sid = get('currentSessionId');
 *   set('currentSessionId', newSid);          // notifies subscribers
 *   const unsub = subscribe('currentMode', (m) => updateUI(m));
 */

const _state = {
  // session
  currentSessionId: null,
  currentSessionTitle: '',
  currentMode: 'code',
  currentModel: '',
  autoApprove: false,
  META: { modes: [], models: [] },
  isResponding: false,

  // ws
  ws: null,
  reconnectDelay: 500,
  pingTimer: null,
  reconnectTimer: null,

  // render
  currentAssistantBubble: null,
  currentToolGroup: null,
  typingEl: null,
  currentAssistantBuffer: '',

  // source picker
  currentSource: null,
};

const _subs = new Map();  // key -> Set<callback>

export function get(key) { return _state[key]; }

export function set(key, value) {
  if (_state[key] === value) return;
  _state[key] = value;
  const subs = _subs.get(key);
  if (subs) {
    for (const cb of subs) {
      try { cb(value); }
      catch (e) { console.error(`state subscriber for ${key} threw:`, e); }
    }
  }
}

export function subscribe(key, cb) {
  if (!_subs.has(key)) _subs.set(key, new Set());
  _subs.get(key).add(cb);
  return () => _subs.get(key).delete(cb);
}

// For tests / debugging only — do not use in app code.
export function _all() { return { ..._state }; }
