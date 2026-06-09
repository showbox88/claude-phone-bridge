/**
 * Session list (drawer body) — fetch + render + search.
 *
 * Ported from legacy app.js lines ~783-892 (loadSessionList + applySearch).
 * highlightMatch was already extracted to util/format.js in Task 1.
 *
 * Session-item clicks emit cmd:load_session via ws/socket.sendWs, mirroring
 * legacy behavior. Rename / delete actions still go through prompt/confirm
 * + cmd:rename_session / cmd:delete_session.
 *
 * The search input event-wiring (debounce) lives here; the boot module
 * (Task 14) just needs to call applySearch(true) at startup or leave the
 * search box untouched.
 */
import {
  sessionListEl,
  sessionSearch,
  sessionSearchClear,
} from '../dom.js';
import { apiGet } from '../api.js';
import { get } from '../state.js';
import { highlightMatch } from '../util/format.js';
import { sendWs } from '../ws/socket.js';
import { isDesktopDrawer, closeDrawer } from './drawer.js';

export async function loadSessionList(query) {
  // No arg → reuse the current search box value so refreshes preserve filter.
  if (query === undefined) query = sessionSearch ? sessionSearch.value : '';
  const q = (query || '').trim();
  let data;
  try {
    const url = q ? `/api/sessions?q=${encodeURIComponent(q)}` : '/api/sessions';
    data = await apiGet(url);
  } catch (_) { return; }
  if (!sessionListEl) return;
  sessionListEl.innerHTML = '';
  const currentMode = get('currentMode');
  const currentSessionId = get('currentSessionId');
  const filtered = data.sessions.filter((s) => (s.mode || 'code') === currentMode);
  if (filtered.length === 0) {
    const empty = document.createElement('div');
    empty.style.cssText = 'padding: 20px 12px; text-align: center; color: var(--text-3); font-size: 13px;';
    empty.textContent = q
      ? `没有匹配「${q}」的会话`
      : (currentMode === 'chat'
          ? '没有 Chat 会话，点 ＋ 新建一个'
          : '没有 Code 会话，点 ＋ 新建一个');
    sessionListEl.appendChild(empty);
    return;
  }
  for (const s of filtered) {
    const item = document.createElement('div');
    const mode = s.mode || 'code';
    item.className = 'session-item ' + 'mode-' + mode + (s.id === data.current ? ' active' : '');
    if (!s.title) item.classList.add('empty-title');
    const t = s.title || '(未命名)';
    const date = new Date((s.updated_at || s.created_at) * 1000);
    const meta = `${date.toLocaleString('zh-CN', { hour12: false })} · ${s.msg_count}条`;
    const badgeIcon = (window.icon && window.icon(mode === 'chat' ? 'chat' : 'code', 13)) || '';
    const editIcon = (window.icon && window.icon('edit', 14)) || '✎';
    const trashIcon = (window.icon && window.icon('trash', 16)) || '×';
    item.innerHTML = `
      <span class="si-badge"></span>
      <div class="si-main">
        <div class="si-title"></div>
        <div class="si-meta"></div>
        <div class="si-snippet hidden"></div>
      </div>
      <button class="si-edit" type="button" title="编辑标题"></button>
      <button class="si-del" type="button" title="删除"></button>
    `;
    item.querySelector('.si-badge').innerHTML = badgeIcon;
    item.querySelector('.si-edit').innerHTML = editIcon;
    item.querySelector('.si-del').innerHTML = trashIcon;
    // Highlight matches in title when searching; snippet shows matched body text
    item.querySelector('.si-title').innerHTML = highlightMatch(t, q);
    item.querySelector('.si-meta').textContent = meta;
    const snipEl = item.querySelector('.si-snippet');
    if (q && s.match_snippet) {
      snipEl.innerHTML = highlightMatch(s.match_snippet, q);
      snipEl.classList.remove('hidden');
    }
    item.addEventListener('click', () => {
      if (s.id !== currentSessionId) {
        sendWs({ type: 'cmd', name: 'load_session', id: s.id });
      }
      if (!isDesktopDrawer()) closeDrawer();
    });
    item.querySelector('.si-edit').addEventListener('click', (e) => {
      e.stopPropagation();
      const next = prompt('会话标题:', s.title || '');
      if (next === null) return;
      const trimmed = next.trim();
      if (trimmed === (s.title || '').trim()) return;
      sendWs({ type: 'cmd', name: 'rename_session', id: s.id, title: trimmed });
    });
    item.querySelector('.si-del').addEventListener('click', (e) => {
      e.stopPropagation();
      if (!confirm(`删除「${t}」？此操作不可恢复。`)) return;
      sendWs({ type: 'cmd', name: 'delete_session', id: s.id });
    });
    sessionListEl.appendChild(item);
  }
}

// Search box → debounced reload of session list with `q`.
let searchDebounce = null;
let lastSearchQuery = '';

export function applySearch(immediate = false) {
  const q = sessionSearch ? sessionSearch.value : '';
  if (sessionSearchClear) sessionSearchClear.classList.toggle('hidden', !q);
  const run = () => {
    if (q === lastSearchQuery) return;
    lastSearchQuery = q;
    loadSessionList(q);
  };
  if (immediate) { clearTimeout(searchDebounce); run(); return; }
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(run, 180);
}

if (sessionSearch) {
  sessionSearch.addEventListener('input', () => applySearch(false));
  sessionSearch.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sessionSearch.value) {
      sessionSearch.value = '';
      applySearch(true);
    }
  });
}
if (sessionSearchClear) {
  sessionSearchClear.addEventListener('click', () => {
    if (sessionSearch) {
      sessionSearch.value = '';
      sessionSearch.focus();
    }
    applySearch(true);
  });
}
