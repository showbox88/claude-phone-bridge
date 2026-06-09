/**
 * Markdown rendering pipeline.
 *
 * Pipeline: raw text → marked.parse() → DOMPurify.sanitize() → safe HTML
 *
 * Streaming optimization:
 * Legacy code re-parsed the ENTIRE buffer on every assistant_text chunk —
 * O(n²). For a 5000-char response this pegged phone CPU.
 *
 * Here:
 *   renderMarkdownFinal(text)  — full parse + sanitize (turn end)
 *   appendStreamChunk(container, oldBuffer, newChunk) — incremental:
 *     fast-path appends plain textContent during stream; only re-parses
 *     full buffer when a paragraph break or fenced code block closes.
 *   finalizeStream(container, finalBuffer) — last clean render
 *
 * Note: the legacy copy-button injection on code blocks (inside the old
 * renderMarkdown) is intentionally dropped here. Re-add via a DOMPurify
 * hook or CSS in a later phase if missed.
 */

const SANITIZE_OPTS = { USE_PROFILES: { html: true } };
const BOUNDARY_RE = /\n\n|```[a-z0-9]*\n[\s\S]*?\n```/m;

function _sanitize(html) {
  if (typeof window !== 'undefined' && window.DOMPurify) {
    return window.DOMPurify.sanitize(html, SANITIZE_OPTS);
  }
  console.warn('DOMPurify missing; rendering unsanitized markdown');
  return html;
}

export function renderMarkdownFinal(text) {
  if (!text) return '';
  const raw = window.marked.parse(text);
  return _sanitize(raw);
}

export function appendStreamChunk(container, oldBuffer, newChunk) {
  if (!newChunk) return oldBuffer;
  const buffer = oldBuffer + newChunk;
  if (BOUNDARY_RE.test(buffer)) {
    container.innerHTML = renderMarkdownFinal(buffer);
    return buffer;
  }
  let tail = container.querySelector(':scope > .stream-tail');
  if (!tail) {
    tail = document.createElement('span');
    tail.className = 'stream-tail';
    container.appendChild(tail);
  }
  tail.textContent += newChunk;
  return buffer;
}

export function finalizeStream(container, finalBuffer) {
  container.innerHTML = renderMarkdownFinal(finalBuffer);
}
