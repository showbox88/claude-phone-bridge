/**
 * Weekly-report settings modal + ad-hoc generation trigger.
 *
 * GET/PUT /api/settings/weekly-report → load/save schedule (enabled, hour,
 * minute, weekday, timezone, last_period_start_iso).
 * POST /api/settings/weekly-report/run-now → trigger one immediate
 * generation (window='current' for "this week so far" or 'previous' for
 * "last week complete"). The server spawns a new chat session; we refresh
 * the session list so the new entry shows up.
 *
 * Ported verbatim from legacy app.js lines 1700-1839 (modal + helpers) and
 * 1842-1855 (toast helper). Behavioral changes:
 *   - raw fetch → apiGet/apiPut/apiPost
 *   - loadSessionList now imported from session/list.js
 *
 * The toast() helper lives here (rather than util/) because it's small and
 * only used by two feature modules today; sync-settings.js re-imports it.
 * Promote to util/ if a third caller materializes.
 */
import { apiGet, apiPut, apiPost } from '../api.js';
import { loadSessionList } from '../session/list.js';

const WEEKDAY_LABELS = ['一', '二', '三', '四', '五', '六', '日']; // 1..7

export async function openWeeklyReportModal() {
  let m = document.getElementById('wr-modal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'wr-modal';
    m.className = 'modal-bg wr-modal hidden';
    m.innerHTML = `
      <div class="modal">
        <div class="modal-head">
          <span>📊 周报设置</span>
          <button class="icon-btn modal-close" type="button">✕</button>
        </div>
        <div class="wr-body">
          <label class="wr-row wr-toggle">
            <span class="wr-label">启用周报</span>
            <input type="checkbox" id="wr-enabled">
          </label>
          <div class="wr-row">
            <span class="wr-label">每周几发送</span>
            <div class="wr-weekday" id="wr-weekday"></div>
          </div>
          <div class="wr-row">
            <span class="wr-label">时间</span>
            <div class="wr-time">
              <input type="number" id="wr-hour" min="0" max="23" step="1">
              <span>:</span>
              <input type="number" id="wr-minute" min="0" max="59" step="1">
            </div>
          </div>
          <div class="wr-row wr-info">
            <span class="wr-label">时区</span>
            <span class="wr-tz" id="wr-tz">—</span>
          </div>
          <div class="wr-row wr-info">
            <span class="wr-label">上次生成</span>
            <span id="wr-last">—</span>
          </div>
          <div class="wr-actions">
            <button id="wr-run-prev" type="button">生成上周完整</button>
            <button id="wr-run" type="button">生成本周至今</button>
            <button id="wr-save" type="button" class="primary">保存</button>
          </div>
          <div class="wr-status" id="wr-status"></div>
        </div>
      </div>
    `;
    document.body.appendChild(m);
    m.addEventListener('click', () => m.classList.add('hidden'));
    m.querySelector('.modal').addEventListener('click', (e) => e.stopPropagation());
    m.querySelector('.modal-close').addEventListener('click', () => m.classList.add('hidden'));
    // render weekday picker once
    const wd = m.querySelector('#wr-weekday');
    WEEKDAY_LABELS.forEach((lbl, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.dataset.day = String(i + 1);
      b.textContent = lbl;
      b.addEventListener('click', () => {
        wd.querySelectorAll('button').forEach((x) => x.classList.remove('active'));
        b.classList.add('active');
      });
      wd.appendChild(b);
    });
    m.querySelector('#wr-save').addEventListener('click', saveWeeklyReport);
    m.querySelector('#wr-run').addEventListener('click', () => runWeeklyReportNow('current'));
    m.querySelector('#wr-run-prev').addEventListener('click', () => runWeeklyReportNow('previous'));
  }
  m.classList.remove('hidden');
  await loadWeeklyReportConfig();
}

export function setWRStatus(text, isError) {
  const el = document.getElementById('wr-status');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = isError ? 'var(--error, #e55)' : 'var(--text-3)';
}

export async function loadWeeklyReportConfig() {
  setWRStatus('加载中…');
  try {
    const cfg = await apiGet('/api/settings/weekly-report');
    document.getElementById('wr-enabled').checked = !!cfg.enabled;
    document.getElementById('wr-hour').value = cfg.hour ?? 9;
    document.getElementById('wr-minute').value = cfg.minute ?? 0;
    document.getElementById('wr-tz').textContent = cfg.timezone || '—';
    document.getElementById('wr-last').textContent =
      cfg.last_period_start_iso ? `${cfg.last_period_start_iso} 起的那一周` : '尚未生成';
    const wd = document.getElementById('wr-weekday');
    wd.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('active', Number(b.dataset.day) === Number(cfg.weekday || 1));
    });
    setWRStatus('');
  } catch (e) {
    setWRStatus('加载失败: ' + e.message, true);
  }
}

export async function saveWeeklyReport() {
  const enabled = document.getElementById('wr-enabled').checked;
  const hour = Number(document.getElementById('wr-hour').value);
  const minute = Number(document.getElementById('wr-minute').value);
  const activeWd = document.querySelector('#wr-weekday button.active');
  const weekday = activeWd ? Number(activeWd.dataset.day) : 1;
  setWRStatus('保存中…');
  try {
    await apiPut('/api/settings/weekly-report', { enabled, hour, minute, weekday });
    setWRStatus('已保存 ✓');
  } catch (e) {
    setWRStatus('保存失败: ' + e.message, true);
  }
}

export async function runWeeklyReportNow(window) {
  const w = window === 'previous' ? 'previous' : 'current';
  const desc = w === 'previous' ? '上周完整' : '本周至今';
  if (!confirm(`立即生成 ${desc} 的周报？会新开一个会话。`)) return;
  setWRStatus('生成中…');
  try {
    const data = await apiPost('/api/settings/weekly-report/run-now', { window: w });
    setWRStatus(`已生成: ${data.label}`);
    loadSessionList();
  } catch (e) {
    setWRStatus('生成失败: ' + e.message, true);
  }
}

/**
 * Lightweight toast — shared with sync-settings.js. The single #toast
 * element is created on first call and reused; classList animates it.
 * Auto-hide at 4.5s; subsequent calls reset the timer.
 */
export function toast(msg, isError) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.toggle('toast-error', !!isError);
  t.classList.add('show');
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove('show'), 4500);
}
