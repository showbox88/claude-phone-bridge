// Permission-request cards (Claude asks to run a tool, user allows/denies).
//
// Extracted from legacy app.js IIFE during Phase 4 modularization.
// `pendingPerms` (id -> card element) is module-local; no other module
// needs to read it.

import { messages } from '../dom.js';
import { escapeHtml } from '../util/escape.js';
import { sendWs } from '../ws/socket.js';
import { scrollToBottom } from './scroll.js';
import { bumpTyping } from './typing.js';

const pendingPerms = new Map(); // id -> card element

export function appendPermissionCard(id, tool, inp) {
  // hideEmptyState equivalent (avoid import cycle with message.js).
  const emptyState = document.getElementById('empty-state');
  if (emptyState && emptyState.parentNode) emptyState.remove();
  const el = document.createElement('div');
  el.className = 'perm-card';
  el.dataset.id = id;
  const inputJson = typeof inp === 'string' ? inp : JSON.stringify(inp, null, 2);
  const toolIcon = (window.icon && window.icon('tool', 16)) || '';
  const copyIcon = (window.icon && window.icon('copy', 14)) || '⧉';
  el.innerHTML = `
    <div class="perm-head">
      <span class="ph-icon">${toolIcon}</span>
      <span>Claude 想运行 <span class="tool">${escapeHtml(tool)}</span></span>
      <button class="copy-btn inline" type="button" title="复制">${copyIcon}</button>
    </div>
    <pre>${escapeHtml(inputJson)}</pre>
    <div class="perm-actions">
      <button type="button" class="deny">拒绝</button>
      <button type="button" class="allow">允许</button>
    </div>
  `;
  el.querySelector('.allow').addEventListener('click', () => respondPerm(id, 'allow'));
  el.querySelector('.deny').addEventListener('click', () => respondPerm(id, 'deny'));
  messages.appendChild(el);
  bumpTyping();
  pendingPerms.set(id, el);
  scrollToBottom(true);
  if (navigator.vibrate) navigator.vibrate([100, 50, 100]);
}

export function markPermResolved(id, decision) {
  const el = pendingPerms.get(id);
  if (!el || el.classList.contains('resolved')) return;
  el.classList.add('resolved');
  const head = el.querySelector('.perm-head');
  if (head) {
    const tag = document.createElement('span');
    tag.className = 'perm-tag perm-tag-' + (decision || 'unknown');
    tag.textContent = decision === 'allow'   ? '已允许'
                    : decision === 'deny'    ? '已拒绝'
                    : decision === 'timeout' ? '已超时'
                    :                          '已处理';
    head.appendChild(tag);
  }
  el.querySelectorAll('.perm-actions button').forEach((b) => { b.disabled = true; });
  pendingPerms.delete(id);
}

export function respondPerm(id, decision) {
  if (!sendWs({ type: 'permission_response', id, decision })) return;
  // Local snappy feedback; the server's broadcast will reach back here too
  // but markPermResolved is idempotent so it's a no-op the second time.
  markPermResolved(id, decision);
}

// Used by message.clearMessages() on session switch to wipe stale references.
export function clearPendingPerms() { pendingPerms.clear(); }
