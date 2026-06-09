// Composer attachments: pending image attachments + pending picked file paths.
//
// Two module-local mutable arrays hold the staged-for-next-send state:
//   - pendingAttachments: image objects uploaded via /api/upload
//     (each {path, url, name?, mime?, kind?})
//   - pendingFiles: absolute file paths picked from the CWD browser modal
//
// They're exported so composer/send.js can read them when building the
// outgoing user_message frame; no other module should mutate them
// (clearAttachments / clearFiles / renderAttachBar reset them).
//
// Extracted from legacy app.js (lines ~94-96, 2185-2362) during Phase 4
// modularization. Behavior preserved verbatim except the upload path now
// goes through apiPostForm() instead of raw fetch().

import { attachBar, albumInput, galleryInput, fileInput, cameraInput } from '../dom.js';
import { get } from '../state.js';
import { apiPostForm, ApiError } from '../api.js';
import { escapeHtml } from '../util/escape.js';
import { appendSystem } from '../render/message.js';

const MAX_ATTACH = 4;

// Module-local mutable arrays. Exported so composer/send.js can read,
// but no other module should mutate.
export const pendingAttachments = [];
export const pendingFiles = [];

export function renderAttachBar() {
  if (!attachBar) return;
  const total = pendingAttachments.length + pendingFiles.length;
  if (total === 0) {
    attachBar.classList.add('hidden');
    attachBar.innerHTML = '';
    return;
  }
  attachBar.classList.remove('hidden');
  attachBar.innerHTML = '';
  const xIcon = (window.icon && window.icon('x', 14)) || '×';
  pendingAttachments.forEach((a, idx) => {
    const chip = document.createElement('div');
    const kind = a.kind || ((a.mime || '').startsWith('image/') ? 'image' : '');
    if (kind === 'image') {
      chip.className = 'attach-chip';
      chip.innerHTML = `<img src="${a.url}" alt=""><button class="x" type="button" title="移除">${xIcon}</button>`;
    } else {
      chip.className = 'attach-chip doc';
      const ic = kind === 'pdf'   ? (window.icon ? window.icon('file_pdf',   18) : '📕')
               : kind === 'sheet' ? (window.icon ? window.icon('file_sheet', 18) : '📊')
               :                    (window.icon ? window.icon('file',        18) : '📄');
      chip.innerHTML = `<span class="doc-icon">${ic}</span><span class="doc-name"></span><button class="x" type="button" title="移除">${xIcon}</button>`;
      chip.querySelector('.doc-name').textContent = a.name || 'file';
    }
    chip.querySelector('.x').addEventListener('click', () => {
      pendingAttachments.splice(idx, 1);
      renderAttachBar();
    });
    attachBar.appendChild(chip);
  });
  pendingFiles.forEach((f, idx) => {
    const chip = document.createElement('div');
    chip.className = 'attach-chip';
    chip.style.cssText = 'width:auto; padding:6px 10px; font-size:12px; color:var(--text-2); display:inline-flex; align-items:center; gap:6px;';
    const name = f.split(/[\\/]/).pop();
    const clipIcon = (window.icon && window.icon('paperclip', 14)) || '📎';
    chip.innerHTML = `<span class="doc-icon">${clipIcon}</span><span>${escapeHtml(name)}</span><button class="x" type="button" style="position:static; background:transparent;" title="移除">${xIcon}</button>`;
    chip.querySelector('.x').addEventListener('click', () => {
      pendingFiles.splice(idx, 1);
      renderAttachBar();
    });
    attachBar.appendChild(chip);
  });
}

export function clearAttachments() { pendingAttachments.length = 0; renderAttachBar(); }
export function clearFiles()       { pendingFiles.length = 0;       renderAttachBar(); }

export async function uploadFiles(files) {
  const currentSessionId = get('currentSessionId');
  if (!currentSessionId) { appendSystem('当前无会话'); return; }
  const room = MAX_ATTACH - pendingAttachments.length;
  if (room <= 0) { appendSystem(`最多 ${MAX_ATTACH} 张图`); return; }
  const picked = Array.from(files).slice(0, room);
  const fd = new FormData();
  fd.append('session_id', currentSessionId);
  for (const f of picked) fd.append('files', f);
  try {
    const data = await apiPostForm('/api/upload', fd);
    for (const f of (data && data.files) || []) pendingAttachments.push(f);
    renderAttachBar();
  } catch (e) {
    if (e instanceof ApiError) {
      const detail = (e.body && e.body.detail) || e.status;
      appendSystem('上传失败: ' + detail);
    } else {
      appendSystem('上传出错: ' + (e && e.message ? e.message : e));
    }
  }
}

// Read image(s) directly from the system clipboard. iOS Safari (≥13.4) and
// Android Chrome both support navigator.clipboard.read(); on iOS the first
// call also surfaces the native "Paste" permission sheet.
export async function pasteFromClipboard() {
  if (!navigator.clipboard || !navigator.clipboard.read) {
    appendSystem('当前浏览器不支持读取剪贴板，长按输入框粘贴试试');
    return;
  }
  let items;
  try {
    items = await navigator.clipboard.read();
  } catch (e) {
    // User dismissed the iOS paste sheet, or clipboard access denied.
    if (e && e.name !== 'NotAllowedError' && e.name !== 'AbortError') {
      appendSystem('读取剪贴板失败: ' + (e.message || e.name));
    }
    return;
  }
  const files = [];
  for (const item of items) {
    const imgType = item.types.find((t) => t.startsWith('image/'));
    if (!imgType) continue;
    try {
      const blob = await item.getType(imgType);
      const ext = (imgType.split('/')[1] || 'png').split('+')[0];
      files.push(new File([blob], `screenshot-${Date.now()}.${ext}`, { type: imgType }));
    } catch (_) { /* skip this item */ }
  }
  if (!files.length) {
    appendSystem('剪贴板里没有图片');
    return;
  }
  await uploadFiles(files);
}

// Pull image files out of a ClipboardEvent.clipboardData payload. Different
// browsers populate `items` (DataTransferItemList) vs `files` (FileList) for
// screenshots — read both, dedupe by File identity.
export function extractClipboardImages(cd) {
  const out = [];
  if (!cd) return out;
  const seen = new Set();
  if (cd.items) {
    for (const it of cd.items) {
      if (it.kind === 'file' && it.type && it.type.startsWith('image/')) {
        const f = it.getAsFile();
        if (f) { out.push(f); seen.add(f); }
      }
    }
  }
  if (cd.files && cd.files.length) {
    for (const f of cd.files) {
      if (f && f.type && f.type.startsWith('image/') && !seen.has(f)) {
        out.push(f);
      }
    }
  }
  return out;
}
