// "Claude is working" three-dot typing indicator.
//
// Extracted from legacy app.js IIFE during Phase 4 modularization.
// `typingEl` lives in the state store so other render modules can observe
// turn lifecycle (bumpTyping moves the indicator to the bottom whenever
// new content lands).

import { messages, emptyState } from '../dom.js';
import { get, set } from '../state.js';
import { scrollToBottom } from './scroll.js';

export function showTyping() {
  let typingEl = get('typingEl');
  if (typingEl && typingEl.parentNode === messages) {
    messages.appendChild(typingEl); // keep it last
    return;
  }
  // hideEmptyState equivalent: emptyState is removed-from-DOM on first content.
  // message.js owns the canonical hideEmptyState; we mirror the side-effect
  // here to avoid an import cycle when the typing indicator appears first.
  if (emptyState && emptyState.parentNode) emptyState.remove();
  typingEl = document.createElement('div');
  typingEl.className = 'typing';
  typingEl.setAttribute('aria-label', 'Claude 正在工作');
  typingEl.innerHTML = '<span></span><span></span><span></span>';
  messages.appendChild(typingEl);
  set('typingEl', typingEl);
  scrollToBottom(false);
}

export function hideTyping() {
  const typingEl = get('typingEl');
  if (typingEl && typingEl.parentNode) typingEl.parentNode.removeChild(typingEl);
  set('typingEl', null);
}

export function bumpTyping() {
  const typingEl = get('typingEl');
  if (typingEl && messages.lastElementChild !== typingEl) messages.appendChild(typingEl);
}
