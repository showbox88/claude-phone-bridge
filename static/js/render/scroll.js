// Scroll management for the messages container.
//
// Tracks whether the user has scrolled away from the bottom. Once true, the
// stream stops auto-scrolling for chunks until they come back near the
// bottom or a forced scroll happens (turn_done, new user message).
//
// Extracted from legacy app.js IIFE during Phase 4 modularization.

import { messagesScroll } from '../dom.js';

let stickToBottom = true;

if (messagesScroll) {
  messagesScroll.addEventListener('scroll', () => {
    const sc = messagesScroll;
    stickToBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 120;
  }, { passive: true });

  // When images inside messages finish loading they grow the content; if the
  // user is still pinned to the bottom, follow.
  messagesScroll.addEventListener('load', (e) => {
    if (e.target && e.target.tagName === 'IMG') scrollToBottom(false);
  }, true);
}

export function scrollToBottom(force) {
  const sc = messagesScroll;
  if (!sc) return;
  if (!force && !stickToBottom) return;
  // Run after layout (rAF) and again on the next frame to catch late
  // re-flow from images, fonts, code-block highlighting, etc.
  requestAnimationFrame(() => {
    sc.scrollTop = sc.scrollHeight;
    requestAnimationFrame(() => { sc.scrollTop = sc.scrollHeight; });
  });
  if (force) stickToBottom = true;
}
