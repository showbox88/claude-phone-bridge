/**
 * Today's-todos bell (notification button in the header).
 *
 * Polls GET /api/today-todos every 60s (poll wired in boot.js) and lights
 * up `notifBtn` with `.alert` when count > 0 && !acked. Clicking the bell
 * opens a panel listing today's todos and POSTs /api/today-todos/ack so
 * the alert dims (until a new todo materializes with a new signature).
 *
 * Ported verbatim from legacy app.js lines 2568-2654. Behavioral changes:
 *   - raw fetch → apiGet/apiPost
 *   - inline fmtDue (legacy 2601-2608) → re-uses identical fmtDue from
 *     util/format.js (verified character-for-character)
 *   - notifBtn imported from dom.js (was a module-scoped const in legacy)
 *
 * NOTE: setupPush() from legacy app.js (~lines 2529-2569, 46 lines) is
 * INTENTIONALLY DELETED. It pre-dated the server-side push.py implementation
 * and was already commented in legacy as "kept for future use — bell click
 * now drives today's-todos panel." Confirmed no caller references setupPush
 * anywhere in the new modules; the legacy service-worker register stays in
 * legacy app.js (line 2563) and will be either retained or moved in Task 14.
 */
import { apiGet, apiPost } from '../api.js';
import { fmtDue } from '../util/format.js';
import { escapeHtml } from '../util/escape.js';
import { notifBtn } from '../dom.js';

// Module-local bell state (legacy `let bellState = {...}` at line 2568).
let bellState = { count: 0, items: [], signature: '', acked: true };

export function applyBellUI() {
  const needAlert = bellState.count > 0 && !bellState.acked;
  notifBtn.classList.toggle('alert', needAlert);
  if (bellState.count === 0) {
    notifBtn.title = '今天没有待办';
  } else if (bellState.acked) {
    notifBtn.title = `今天有 ${bellState.count} 件待办（已查看）`;
  } else {
    notifBtn.title = `今天有 ${bellState.count} 件待办`;
  }
}

export async function checkBell() {
  try {
    const data = await apiGet('/api/today-todos');
    // Server is up but PocketBase isn't. Keep the bell's last-known state
    // instead of flashing back to "no todos today" — an outage shouldn't
    // look the same as an empty list.
    if (data.ok === false) {
      if (notifBtn.title.indexOf('待办') < 0) {
        notifBtn.title = '待办数据暂不可用';
      }
      return;
    }
    bellState = data;
    applyBellUI();
  } catch (e) { /* offline ok */ }
}

export async function openBellPanel() {
  await checkBell();
  const overlay = document.createElement('div');
  overlay.className = 'bell-modal';
  const rows = bellState.items.map((t) => {
    const prio = (t.priority || 'Normal').toLowerCase();
    return `
      <div class="bell-row prio-${prio}">
        <div class="bell-pri"></div>
        <div class="bell-body">
          <div class="bell-text">${escapeHtml(t.title || '(无标题)')}</div>
          <div class="bell-meta">${escapeHtml(fmtDue(t.due_date))} · ${escapeHtml(t.priority || 'Normal')}</div>
        </div>
      </div>`;
  }).join('');
  overlay.innerHTML = `
    <div class="bell-card" role="dialog" aria-modal="true" aria-label="今天的待办">
      <div class="bell-head">
        <div class="bell-title">今天的待办 (${bellState.count})</div>
        <button class="icon-btn close-bell" type="button" data-icon="close" data-icon-size="18" aria-label="关闭"></button>
      </div>
      <div class="bell-list">${rows || '<div class="bell-empty">今天没什么要做的 ✨</div>'}</div>
    </div>`;
  document.body.appendChild(overlay);
  if (window.hydrateIcons) window.hydrateIcons(overlay);
  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('.close-bell').addEventListener('click', close);

  if (bellState.signature && bellState.count > 0 && !bellState.acked) {
    try {
      await apiPost('/api/today-todos/ack', { signature: bellState.signature });
    } catch (_) { /* ignore */ }
    bellState.acked = true;
    applyBellUI();
  }
}
