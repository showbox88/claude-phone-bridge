/**
 * Table-driven dispatch for incoming WS frames.
 *
 * Replaces the legacy app.js `handleEvent(msg)` switch chain. Each
 * frame type has a single handler function. Unknown types are logged
 * and ignored.
 *
 * endStream() helper consolidates the spots in legacy that did
 * currentAssistantBubble = null + closeToolGroup() (with finalizeStream
 * to flush the streaming markdown buffer to a final parse — the legacy
 * code didn't do this because it re-parsed on every chunk; the new
 * streaming renderer only does cheap text appends until the end).
 *
 * Frame types covered (every `case` in legacy handleEvent at app.js:946):
 *   hello, session_loaded, session_deleted, sessions_changed,
 *   session_renamed, session_mode_changed, session_model_changed,
 *   auto_approve_changed, system, error, user_echo, assistant_text,
 *   tool_use, tool_result, permission_request, permission_resolved,
 *   turn_done, pong.
 *
 * Side-effects that today go through stubs (setHeader/setMode/setModel/
 * setAutoApprove) become real in Task 8. Side-effects that legacy did
 * but the rest of the new module set hasn't extracted yet (loadSessionList,
 * setResponding, clearAttachments, clearFiles, appendUser) are NOT
 * called from here — they'll be re-wired when those modules land
 * (Tasks 8-13). The PWA continues to load legacy app.js as entry until
 * Task 15 swap, so behavior is preserved during the in-between phases.
 */
import { get, set } from '../state.js';
import {
  appendAssistantText, appendSystem, appendError,
  renderHistory, clearMessages,
} from '../render/message.js';
import { finalizeStream } from '../render/markdown.js';
import { appendToolUse, appendToolResult, closeToolGroup } from '../render/tool.js';
import { appendPermissionCard, markPermResolved } from '../render/perm.js';
import { hideTyping } from '../render/typing.js';
import { scrollToBottom } from '../render/scroll.js';
import { setHeader, setMode, setModel, setAutoApprove } from '../session/header.js';

function endStream() {
  const bubble = get('currentAssistantBubble');
  const buf = get('currentAssistantBuffer');
  if (bubble && buf) finalizeStream(bubble, buf);
  set('currentAssistantBubble', null);
  set('currentAssistantBuffer', '');
  closeToolGroup();
  hideTyping();
}

const HANDLERS = {
  hello: (m) => {
    if (m.cwd !== undefined) setHeader(null, m.cwd);
    if (m.auto_approve !== undefined) setAutoApprove(!!m.auto_approve);
    if (m.session) {
      set('currentSessionId', m.session.id);
      set('currentSessionTitle', m.session.title || '');
      setHeader(m.session.title || '', m.session.cwd || '');
      if (m.session.mode) setMode(m.session.mode);
      if (m.session.model !== undefined) setModel(m.session.model);
      if (Array.isArray(m.session.messages)) {
        clearMessages();
        renderHistory(m.session.messages);
      }
    }
    if (Array.isArray(m.pending_perms)) {
      for (const p of m.pending_perms) appendPermissionCard(p.id, p.tool, p.input);
    }
  },
  user_echo: () => {},  // PWA renders user message optimistically on send
  assistant_text: (m) => { hideTyping(); appendAssistantText(m.text || ''); },
  tool_use: (m) => { endStream(); appendToolUse(m.tool, m.input); },
  tool_result: (m) => appendToolResult(m.ok, m.content),
  permission_request: (m) => {
    appendPermissionCard(m.id, m.tool, m.input);
    scrollToBottom();
  },
  permission_resolved: (m) => markPermResolved(m.id, m.decision),
  turn_done: () => { endStream(); scrollToBottom(true); },
  system: (m) => appendSystem(m.msg || ''),
  error: (m) => appendError(m.msg || 'error'),
  pong: () => {},
  session_loaded: (m) => {
    if (m.session) {
      set('currentSessionId', m.session.id);
      setHeader(m.session.title || '', m.session.cwd || '');
      if (m.session.mode) setMode(m.session.mode);
      if (m.session.model !== undefined) setModel(m.session.model);
      clearMessages();
      if (Array.isArray(m.session.messages)) renderHistory(m.session.messages);
    }
  },
  session_deleted: () => {},
  session_renamed: (m) => {
    if (m.id === get('currentSessionId')) {
      set('currentSessionTitle', m.title || '');
      setHeader(m.title || '', null);
    }
  },
  // Legacy had this; the plan-template missed it. Ported verbatim.
  session_mode_changed: (m) => {
    if (m.id === get('currentSessionId')) setMode(m.mode);
  },
  session_model_changed: (m) => {
    if (m.id === get('currentSessionId')) setModel(m.model || '');
  },
  auto_approve_changed: (m) => setAutoApprove(!!m.value),
  sessions_changed: () => {},
};

export function dispatch(msg) {
  const t = msg && msg.type;
  const fn = HANDLERS[t];
  if (fn) {
    try { fn(msg); }
    catch (e) { console.error('WS handler', t, 'threw:', e); }
  } else if (t) {
    console.warn('WS unknown frame type:', t, msg);
  }
}
