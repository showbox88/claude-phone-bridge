// Top-level message rendering: user / assistant_text / system / error
// bubbles, history replay, clear.
//
// `appendAssistantText` is the streaming entry point — it appends a chunk
// to the live assistant bubble using the optimized incremental renderer
// in ./markdown.js (fast text-append during stream, full markdown re-parse
// only at paragraph/codeblock boundaries). The legacy version re-parsed
// the entire buffer on every chunk → O(n²) on phone CPUs.
//
// `finalizeStream` is NOT called from here. The ws turn_done handler
// (Task 7) imports it from ./markdown.js and runs it on the current
// bubble at the end of a turn.
//
// Extracted from legacy app.js IIFE during Phase 4 modularization.

import { messages, emptyState } from '../dom.js';
import { get, set } from '../state.js';
import { escapeHtml } from '../util/escape.js';
import { parseCheckinYaml } from '../util/yaml.js';
import { assetUrl } from '../api.js';
import { appendStreamChunk } from './markdown.js';
import { scrollToBottom } from './scroll.js';
import { renderCheckinCard } from './checkin-card.js';
import { appendToolUse, appendToolResult, closeToolGroup } from './tool.js';
import { clearPendingPerms } from './perm.js';
import { bumpTyping } from './typing.js';

export function hideEmptyState() {
  if (emptyState && emptyState.parentNode) emptyState.remove();
}

export function appendUser(text, images, files) {
  hideEmptyState();
  const el = document.createElement('div');
  el.className = 'msg user';
  if (Array.isArray(images) && images.length) {
    // Split into image + document attachments based on file extension.
    const IMG_RX = /\.(png|jpe?g|webp|gif)(\?|$)/i;
    const imgList = [], docList = [];
    for (const img of images) {
      const url = typeof img === 'string' ? assetUrl(`/uploads/${img}`) : img.url;
      const name = (typeof img === 'string' ? img.split('/').pop() : (img.name || ''));
      const isImg = IMG_RX.test(url);
      if (isImg) imgList.push({ url, name });
      else docList.push({ url, name });
    }
    if (imgList.length) {
      const grid = document.createElement('div');
      grid.className = 'img-grid';
      for (const im of imgList) {
        const a = document.createElement('a');
        a.href = im.url; a.target = '_blank'; a.rel = 'noopener';
        const i = document.createElement('img');
        i.src = im.url; i.loading = 'lazy';
        a.appendChild(i);
        grid.appendChild(a);
      }
      el.appendChild(grid);
    }
    if (docList.length) {
      const dl = document.createElement('div');
      dl.className = 'doc-list';
      for (const d of docList) {
        const ext = (d.url.match(/\.([a-z0-9]+)(\?|$)/i) || [, ''])[1].toLowerCase();
        const ic = ext === 'pdf'                     ? (window.icon ? window.icon('file_pdf',   18) : '')
                 : (ext === 'xlsx' || ext === 'xls') ? (window.icon ? window.icon('file_sheet', 18) : '')
                 :                                     (window.icon ? window.icon('file',        18) : '');
        const a = document.createElement('a');
        a.href = d.url; a.target = '_blank'; a.rel = 'noopener';
        a.className = 'doc-link';
        a.innerHTML = `<span class="doc-icon">${ic}</span><span class="doc-name"></span>`;
        a.querySelector('.doc-name').textContent = d.name || 'file';
        dl.appendChild(a);
      }
      el.appendChild(dl);
    }
  }
  if (Array.isArray(files) && files.length) {
    const fl = document.createElement('div');
    fl.className = 'file-list';
    const clipIcon = (window.icon && window.icon('paperclip', 13)) || '📎';
    fl.innerHTML = `<span class="fl-ic">${clipIcon}</span><span>${escapeHtml(files.join('  ·  '))}</span>`;
    el.appendChild(fl);
  }
  if (text) {
    // Detect a ```checkin``` block and render as a compact card instead of
    // dumping the raw YAML. Anything outside the fence renders as text.
    const m = text.match(/^([\s\S]*?)```checkin\n([\s\S]*?)\n?```([\s\S]*)$/);
    if (m) {
      const before = m[1], yaml = m[2], after = m[3];
      if (before.trim()) {
        const t = document.createElement('div');
        t.className = 'msg-text';
        t.textContent = before.trimEnd();
        el.appendChild(t);
      }
      el.appendChild(renderCheckinCard(parseCheckinYaml(yaml), yaml));
      if (after.trim()) {
        const t = document.createElement('div');
        t.className = 'msg-text';
        t.textContent = after.trimStart();
        el.appendChild(t);
      }
    } else {
      const t = document.createElement('div');
      t.className = 'msg-text';
      t.textContent = text;
      el.appendChild(t);
    }
  }
  messages.appendChild(el);
  bumpTyping();
  scrollToBottom(true);
}

export function appendAssistantText(text) {
  hideEmptyState();
  let bubble = get('currentAssistantBubble');
  let buffer = get('currentAssistantBuffer') || '';
  if (!bubble) {
    closeToolGroup();
    bubble = document.createElement('div');
    bubble.className = 'msg assistant';
    messages.appendChild(bubble);
    set('currentAssistantBubble', bubble);
    buffer = '';
    bumpTyping();
  }
  buffer = appendStreamChunk(bubble, buffer, text);
  set('currentAssistantBuffer', buffer);
  scrollToBottom(false);
}

export function appendSystem(text) {
  hideEmptyState();
  const el = document.createElement('div');
  el.className = 'msg system';
  el.textContent = text;
  messages.appendChild(el);
  bumpTyping();
  scrollToBottom(false);
}

export function appendError(text) {
  hideEmptyState();
  const el = document.createElement('div');
  el.className = 'msg error';
  el.textContent = text;
  messages.appendChild(el);
  bumpTyping();
  scrollToBottom(true);
}

export function clearMessages() {
  messages.innerHTML = '';
  if (emptyState) messages.appendChild(emptyState);
  set('currentAssistantBubble', null);
  set('currentAssistantBuffer', '');
  closeToolGroup();
  set('typingEl', null);  // wiped along with messages.innerHTML
  clearPendingPerms();
}

export function renderHistory(msgs) {
  clearMessages();
  if (!msgs || msgs.length === 0) return;
  for (const m of msgs) {
    const c = m.content || {};
    switch (m.role) {
      case 'user':
        appendUser(c.text || '', c.images || [], c.files || []);
        set('currentAssistantBubble', null);
        closeToolGroup();
        break;
      case 'assistant_text':
        appendAssistantText(c.text || '');
        break;
      case 'tool_use':
        set('currentAssistantBubble', null);
        appendToolUse(c.tool, c.input);
        break;
      case 'tool_result':
        appendToolResult(c.ok, c.content);
        break;
    }
  }
  set('currentAssistantBubble', null);
  closeToolGroup();
}
