// Composer textarea behavior: auto-resize, send-button responding state,
// global paste handler.
//
// `isResponding` previously lived as a module-level `let` in the legacy IIFE;
// it now lives in the state store so ws/handlers (turn_done / error) and
// composer/send (sendCurrent) can coordinate without circular imports.
//
// Extracted from legacy app.js (lines 1051-1067, 2363-2376) during Phase 4
// modularization. Behavior preserved verbatim.

import { input, sendBtn } from '../dom.js';
import { set } from '../state.js';
import { showTyping, hideTyping } from '../render/typing.js';
// Forward references — ES modules tolerate this circular shape because the
// imported names are accessed at call-time, not at module evaluation.
import { uploadFiles, extractClipboardImages } from './attachments.js';

export function autoresize() {
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
}

export function setResponding(flag) {
  const isResponding = !!flag;
  set('isResponding', isResponding);
  if (!sendBtn) return;
  sendBtn.classList.toggle('stopping', isResponding);
  if (window.icon) {
    sendBtn.innerHTML = window.icon(isResponding ? 'stop' : 'send', 18);
  } else {
    sendBtn.textContent = isResponding ? '■' : '↑';
  }
  sendBtn.setAttribute('aria-label', isResponding ? '停止' : '发送');
  sendBtn.title = isResponding ? '停止当前回复' : '发送';
  if (isResponding) showTyping(); else hideTyping();
}

// Document-level paste handler: a single listener on document covers both
// "focused in input" and "focused elsewhere" (paste events bubble up from
// the textarea). Prevents image data-URLs from landing as literal text in
// the textarea — files are uploaded via /api/upload instead.
export function onPaste(e) {
  const tgt = e.target;
  const inEditable = tgt && (
    tgt === input ||
    tgt.isContentEditable ||
    (tgt.tagName === 'INPUT' && tgt.type === 'text')
  );
  const files = extractClipboardImages(e.clipboardData);
  if (!files.length) return;
  // Prevent the image from being pasted as a literal data URL into the textarea.
  e.preventDefault();
  uploadFiles(files);
  if (!inEditable && input) input.focus();
}
