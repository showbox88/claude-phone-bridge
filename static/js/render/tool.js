// Tool-use / tool-result rendering. Consecutive tool calls within a turn
// collapse into a single <details> group.
//
// Extracted from legacy app.js IIFE during Phase 4 modularization.
// `currentToolGroup` lives in the state store so message.js can close the
// group when an assistant_text bubble starts.

import { messages } from '../dom.js';
import { get, set } from '../state.js';
import { escapeHtml } from '../util/escape.js';
import { scrollToBottom } from './scroll.js';
import { bumpTyping } from './typing.js';

export function ensureToolGroup() {
  let currentToolGroup = get('currentToolGroup');
  if (currentToolGroup) return currentToolGroup;
  const wrap = document.createElement('details');
  wrap.className = 'tool-group';
  wrap.innerHTML = `
    <summary>
      <span class="tg-icon">▸</span>
      <span class="tg-label">工具调用</span>
      <span class="tg-count">0</span>
    </summary>
    <div class="tg-body"></div>
  `;
  messages.appendChild(wrap);
  set('currentToolGroup', wrap);
  bumpTyping();
  return wrap;
}

export function bumpToolGroupCount() {
  const currentToolGroup = get('currentToolGroup');
  if (!currentToolGroup) return;
  const body = currentToolGroup.querySelector('.tg-body');
  const cnt = currentToolGroup.querySelector('.tg-count');
  if (cnt && body) cnt.textContent = String(body.children.length);
}

export function closeToolGroup() { set('currentToolGroup', null); }

export function appendToolUse(tool, inp) {
  // hideEmptyState equivalent (avoid import cycle with message.js).
  const emptyState = document.getElementById('empty-state');
  if (emptyState && emptyState.parentNode) emptyState.remove();
  const group = ensureToolGroup();
  const body = group.querySelector('.tg-body');
  const el = document.createElement('details');
  el.className = 'tool-block';
  const inputJson = typeof inp === 'string' ? inp : JSON.stringify(inp, null, 2);
  const playIcon = (window.icon && window.icon('play', 11)) || '▶';
  const copyIcon = (window.icon && window.icon('copy', 13)) || '⧉';
  el.innerHTML = `
    <summary>
      <span class="tool-icon">${playIcon}</span>
      <span class="tool-name">${escapeHtml(tool)}</span>
      <button class="copy-btn inline" type="button" title="复制">${copyIcon}</button>
    </summary>
    <pre>${escapeHtml(inputJson)}</pre>
  `;
  body.appendChild(el);
  bumpToolGroupCount();
  scrollToBottom(false);
}

export function appendToolResult(ok, content) {
  // hideEmptyState equivalent (avoid import cycle with message.js).
  const emptyState = document.getElementById('empty-state');
  if (emptyState && emptyState.parentNode) emptyState.remove();
  const group = ensureToolGroup();
  const body = group.querySelector('.tg-body');
  const el = document.createElement('details');
  el.className = 'tool-block ' + (ok ? 'result' : 'error');
  const icon = (window.icon && window.icon(ok ? 'check' : 'x', 13)) || (ok ? '✓' : '✗');
  const copyIcon = (window.icon && window.icon('copy', 13)) || '⧉';
  const label = ok ? 'result' : 'error';
  el.innerHTML = `
    <summary>
      <span class="tool-icon">${icon}</span>
      <span class="tool-name">${label}</span>
      <button class="copy-btn inline" type="button" title="复制">${copyIcon}</button>
    </summary>
    <pre>${escapeHtml(content || '')}</pre>
  `;
  body.appendChild(el);
  bumpToolGroupCount();
  scrollToBottom(false);
}
