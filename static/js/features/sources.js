/**
 * Source picker — startup screen + add/edit form + status probe.
 *
 * Lets the user manage multiple Phone Bridge backends (each device on the
 * tailnet that runs phone-bridge.service is a "source"). The picker is the
 * first screen on cold start; selecting an online source hides the picker
 * and brings up the main chat shell.
 *
 * localStorage keys:
 *   bridge.sources           — JSON array of {id, name, url, added_at}
 *   bridge.current_source_id — selected source id
 *
 * On startup boot.js (Task 14) reads getCurrentSourceId + loadSources; if a
 * stored id resolves to a known source it calls enterSource(id), otherwise
 * showPicker() (and if there are zero sources, also openSourceForm(null)).
 *
 * Ported verbatim from legacy app.js lines 63-78 (storage helpers) and
 * 2662-2853 (UI). One behavioral change: `currentSource` lives in the
 * state store now (api.js reads it from there), so we use set('currentSource', src)
 * instead of mutating a module-scoped variable.
 *
 * Note on loadMeta(): legacy enterSource called loadMeta() after connect();
 * loadMeta is still in legacy app.js (not yet extracted), so the boot
 * module (Task 14) is responsible for orchestrating loadMeta around
 * connect() when wiring the new entry point. This module deliberately
 * does NOT call loadMeta — Task 14 will.
 */
import {
  sourcePicker, sourceName,
  spList,
  spForm, spFormTitle, spName, spUrl,
} from '../dom.js';
import { get, set } from '../state.js';
import { setConn, connect } from '../ws/socket.js';
import { clearMessages } from '../render/message.js';
import { setHeader, setMode, setModel } from '../session/header.js';
import { clearAttachments, clearFiles } from '../composer/attachments.js';
import { setResponding } from '../composer/input.js';

const SOURCES_KEY = 'bridge.sources';
const CURRENT_KEY = 'bridge.current_source_id';

// Module-local picker state (legacy module-scoped lets).
let pickerPollTimer = null;
let editingSourceId = null;

// ---------- storage helpers ----------

export function loadSources() {
  try { return JSON.parse(localStorage.getItem(SOURCES_KEY) || '[]'); }
  catch (_) { return []; }
}

export function saveSources(arr) {
  localStorage.setItem(SOURCES_KEY, JSON.stringify(arr));
}

export function getCurrentSourceId() {
  return localStorage.getItem(CURRENT_KEY) || '';
}

export function setCurrentSourceId(id) {
  if (id) localStorage.setItem(CURRENT_KEY, id);
  else localStorage.removeItem(CURRENT_KEY);
}

export function findSource(id) {
  return loadSources().find((s) => s.id === id) || null;
}

// ---------- picker UI ----------

export function renderPicker() {
  const sources = loadSources();
  const list = spList;
  list.innerHTML = '';
  if (sources.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'sp-empty';
    empty.textContent = '还没有添加电脑，点下方"＋ 添加电脑"开始';
    list.appendChild(empty);
    return;
  }
  for (const s of sources) {
    const item = document.createElement('div');
    item.className = 'sp-item checking';
    item.dataset.id = s.id;
    const monitorIcon = (window.icon && window.icon('monitor', 22)) || '';
    const editIcon = (window.icon && window.icon('edit', 16)) || '✎';
    const trashIcon = (window.icon && window.icon('trash', 16)) || '×';
    item.innerHTML = `
      <span class="sp-mark">${monitorIcon}</span>
      <span class="sp-status"></span>
      <div class="sp-meta">
        <span class="sp-name"></span>
        <span class="sp-url"></span>
        <span class="sp-state">检测中…</span>
      </div>
      <div class="sp-actions-inline">
        <button data-act="edit" title="编辑">${editIcon}</button>
        <button data-act="del" title="删除">${trashIcon}</button>
      </div>
    `;
    item.querySelector('.sp-name').textContent = s.name;
    item.querySelector('.sp-url').textContent = s.url;
    item.addEventListener('click', (e) => {
      if (e.target.closest('.sp-actions-inline')) return;
      if (!item.classList.contains('online')) return;
      enterSource(s.id);
    });
    item.querySelector('[data-act="edit"]').addEventListener('click', (e) => {
      e.stopPropagation();
      openSourceForm(s.id);
    });
    item.querySelector('[data-act="del"]').addEventListener('click', (e) => {
      e.stopPropagation();
      if (!confirm(`删除「${s.name}」？`)) return;
      const left = loadSources().filter((x) => x.id !== s.id);
      saveSources(left);
      if (getCurrentSourceId() === s.id) setCurrentSourceId('');
      renderPicker();
      checkAllStatuses();
    });
    list.appendChild(item);
  }
}

// Raw fetch (not apiGet) — we're probing OTHER sources, not currentSource.
// apiGet routes through currentSource via api.js which is the opposite of
// what we want when listing/checking the picker.
export async function checkSourceStatus(src) {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 4000);
    const url = src.url.replace(/\/$/, '') + '/api/health';
    const r = await fetch(url, { signal: ctrl.signal, mode: 'cors' });
    clearTimeout(t);
    if (r.ok) {
      const data = await r.json().catch(() => ({}));
      return { online: true, info: data };
    }
  } catch (_) { /* offline */ }
  return { online: false };
}

export async function checkAllStatuses() {
  const sources = loadSources();
  await Promise.all(sources.map(async (s) => {
    const res = await checkSourceStatus(s);
    const item = document.querySelector(`.sp-item[data-id="${s.id}"]`);
    if (!item) return;
    item.classList.remove('checking', 'online', 'offline');
    item.classList.add(res.online ? 'online' : 'offline');
    const stateEl = item.querySelector('.sp-state');
    if (res.online) {
      const remoteName = res.info && res.info.name ? res.info.name : '';
      stateEl.textContent = remoteName ? `在线 · ${remoteName}` : '在线';
    } else {
      stateEl.textContent = '离线 / 不可达';
    }
  }));
}

export function showPicker() {
  sourcePicker.classList.remove('hidden');
  renderPicker();
  checkAllStatuses();
  if (pickerPollTimer) clearInterval(pickerPollTimer);
  pickerPollTimer = setInterval(checkAllStatuses, 8000);
}

export function hidePicker() {
  sourcePicker.classList.add('hidden');
  if (pickerPollTimer) { clearInterval(pickerPollTimer); pickerPollTimer = null; }
}

// enterSource transitions from the picker into the main app shell.
// Legacy verbatim, with two adaptations for the modular world:
//   1. currentSource lives in the state store (api.js reads it from there)
//   2. loadMeta() is NOT called here — the boot module (Task 14) will
//      orchestrate loadMeta around connect() once that function is also
//      extracted. Today's legacy app.js still owns loadMeta.
export function enterSource(id) {
  const src = findSource(id);
  if (!src) return;
  setCurrentSourceId(id);
  set('currentSource', src);
  sourceName.textContent = src.name;
  setConn('connecting');
  // close any existing WS
  const ws = get('ws');
  if (ws) { try { ws.close(); } catch (_) {} set('ws', null); }
  // reset chat-side state
  clearMessages();
  setHeader('Claude', '');
  setMode('code');
  setModel('');
  clearAttachments();
  clearFiles();
  setResponding(false);
  hidePicker();
  // NOTE: loadMeta() — Task 14 boot module wires this.
  connect();
}

export function exitToSourcePicker() {
  const ws = get('ws');
  if (ws) { try { ws.close(); } catch (_) {} set('ws', null); }
  setCurrentSourceId('');
  set('currentSource', null);
  setConn('disconnected');
  showPicker();
}

// ---------- add / edit form ----------

export function openSourceForm(idOrNull) {
  editingSourceId = idOrNull || null;
  if (editingSourceId) {
    const s = findSource(editingSourceId);
    spFormTitle.textContent = '编辑电脑';
    spName.value = s ? s.name : '';
    spUrl.value = s ? s.url : '';
  } else {
    spFormTitle.textContent = '添加电脑';
    spName.value = '';
    // Pre-fill with current page origin to make first-time setup easy.
    spUrl.value = location.origin || 'https://';
  }
  spForm.classList.remove('hidden');
  setTimeout(() => spName.focus(), 50);
}

export function closeSourceForm() {
  spForm.classList.add('hidden');
  editingSourceId = null;
}

export function saveSourceForm() {
  const name = (spName.value || '').trim();
  let url = (spUrl.value || '').trim();
  if (!name) { alert('请输入名称'); return; }
  if (!url) { alert('请输入地址'); return; }
  if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
  url = url.replace(/\/$/, '');
  const sources = loadSources();
  if (editingSourceId) {
    const s = sources.find((x) => x.id === editingSourceId);
    if (s) { s.name = name; s.url = url; }
  } else {
    const id = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : ('s' + Date.now() + '_' + Math.random().toString(36).slice(2, 8));
    sources.push({ id, name, url, added_at: Date.now() });
  }
  saveSources(sources);
  closeSourceForm();
  renderPicker();
  checkAllStatuses();
}
