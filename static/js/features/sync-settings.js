/**
 * Notion-sync settings modal — globals (paused / timezone / sync hours /
 * last-run) plus per-target table CRUD (enable/auto/title/date fields,
 * delete) plus the "+ 新增同步表" add form (which lives in #sync-add-dialog,
 * a <dialog> shipped in index.html).
 *
 * Ported verbatim from legacy app.js lines 1897-2168. Behavioral changes:
 *   - raw fetch → apiGet/apiPut/apiPost/apiPatch/apiDelete
 *   - toast/checkBell imported from sibling feature modules
 *   - `window.__sync_available` global is preserved (cross-module
 *     coordination with the inline add-dialog handler)
 *
 * Note: the global `syncBtn` click handler (legacy 1857-1895) is NOT
 * extracted here — that's a one-shot button wiring that belongs in boot.js
 * (Task 14) since it touches three modules (toast, checkBell, apiPost).
 *
 * The `toast` import is here for future use by sibling modules; this file
 * currently doesn't call toast directly (sync-settings uses #ss-status
 * for in-modal feedback and only the syncBtn handler — outside this
 * module — calls toast).
 */
import { apiGet, apiPut, apiPost, apiPatch, apiDelete } from '../api.js';
import { toast } from './weekly-report.js'; // eslint-disable-line no-unused-vars
import { checkBell } from './bell.js';
import { escapeHtml } from '../util/escape.js';

export async function openSyncSettingsModal() {
  let m = document.getElementById('ss-modal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'ss-modal';
    m.className = 'modal-bg wr-modal hidden';
    m.innerHTML = `
      <div class="modal">
        <div class="modal-head">
          <span>🔄 同步设置</span>
          <button class="icon-btn modal-close" type="button">✕</button>
        </div>
        <div class="wr-body">
          <label class="wr-row wr-toggle">
            <span class="wr-label">暂停同步</span>
            <input type="checkbox" id="ss-paused">
          </label>
          <div class="wr-row">
            <span class="wr-label">时区</span>
            <input type="text" id="ss-tz" placeholder="America/New_York" style="flex:1; min-width:0;">
          </div>
          <div class="wr-row">
            <span class="wr-label">同步时刻 1</span>
            <div class="wr-time">
              <input type="number" id="ss-h1" min="0" max="23" step="1" style="width:5em;">
              <span>:00</span>
            </div>
          </div>
          <div class="wr-row">
            <span class="wr-label">同步时刻 2</span>
            <div class="wr-time">
              <input type="number" id="ss-h2" min="0" max="23" step="1" style="width:5em;" placeholder="留空=禁用">
              <span>:00</span>
            </div>
          </div>
          <div class="wr-row wr-info">
            <span class="wr-label">上次跑</span>
            <span id="ss-last">—</span>
          </div>
          <div class="wr-actions">
            <button id="ss-run-now" type="button">立即同步</button>
            <button id="ss-save" type="button" class="primary">保存</button>
          </div>
          <div class="wr-status" id="ss-status"></div>
          <div id="sync-targets-section" class="sync-targets-section">
            <h4>同步表</h4>
            <div id="sync-targets-tbody" class="sync-targets-tbody">
              <div class="sync-targets-loading">加载中…</div>
            </div>
            <button id="sync-targets-add" type="button" class="sync-targets-add">
              + 新增同步表
            </button>
          </div>
        </div>
      </div>
    `;
    document.body.appendChild(m);
    m.addEventListener('click', () => m.classList.add('hidden'));
    m.querySelector('.modal').addEventListener('click', (e) => e.stopPropagation());
    m.querySelector('.modal-close').addEventListener('click', () => m.classList.add('hidden'));
    const addBtn = m.querySelector('#sync-targets-add');
    if (addBtn) addBtn.addEventListener('click', openAddSyncTarget);

    m.querySelector('#ss-save').addEventListener('click', async () => {
      const body = {
        paused:            !!m.querySelector('#ss-paused').checked,
        timezone:          (m.querySelector('#ss-tz').value || 'America/New_York').trim(),
        sync_hour_local:   m.querySelector('#ss-h1').value === '' ? null : parseInt(m.querySelector('#ss-h1').value, 10),
        sync_hour_local_2: m.querySelector('#ss-h2').value === '' ? null : parseInt(m.querySelector('#ss-h2').value, 10),
      };
      setSSStatus('保存中…');
      try {
        const data = await apiPut('/api/settings/notion-sync', body);
        fillSyncSettings(data);
        setSSStatus('已保存 ✓');
      } catch (e) {
        setSSStatus('保存失败: ' + e.message, true);
      }
    });

    m.querySelector('#ss-run-now').addEventListener('click', async () => {
      setSSStatus('同步中…');
      try {
        const data = await apiPost('/api/sync/now', {});
        if (!data.ok) throw new Error(data.stderr || data.detail || 'sync failed');
        const s = data.summary || {};
        const parts = [];
        for (const k of ['applied','conflicts','deletes','pending','decisions_applied','archived_resolved']) {
          if (s[k]) parts.push(`${k}=${s[k]}`);
        }
        setSSStatus('同步完成: ' + (parts.join(' / ') || '无变化'));
        try { checkBell(); } catch (_) {}
        // refresh last_run_at
        try {
          const fresh = await apiGet('/api/settings/notion-sync');
          if (fresh) fillSyncSettings(fresh);
        } catch (_) { /* ignore */ }
      } catch (e) {
        setSSStatus('同步失败: ' + e.message, true);
      }
    });
  }
  m.classList.remove('hidden');
  setSSStatus('加载中…');
  try {
    const data = await apiGet('/api/settings/notion-sync');
    fillSyncSettings(data);
    setSSStatus('');
  } catch (e) {
    setSSStatus('加载失败: ' + e.message, true);
  }
  loadSyncTargets();
}

export async function loadSyncTargets() {
  const tbody = document.getElementById('sync-targets-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<div class="sync-targets-loading">加载中…</div>';
  try {
    const data = await apiGet('/api/sync/targets');
    renderSyncTargets(data);
    window.__sync_available = data.available || [];
  } catch (e) {
    tbody.innerHTML = '<div class="sync-targets-error">同步配置读取失败: '
                     + escapeHtml(String(e)) + '</div>';
  }
}

export function renderSyncTargets(data) {
  const tbody = document.getElementById('sync-targets-tbody');
  if (!tbody) return;
  const rows = (data.configured || []).map(t => `
    <div class="st-row" data-collection="${escapeHtml(t.collection)}">
      <span class="st-name">${escapeHtml(t.collection)}</span>
      <label class="st-check">
        <input type="checkbox" data-key="enabled" ${t.enabled ? 'checked' : ''}>启用
      </label>
      <label class="st-check">
        <input type="checkbox" data-key="auto_sync" ${t.auto_sync ? 'checked' : ''}>自动
      </label>
      <input class="st-field" data-key="title_field"
             value="${escapeHtml(t.title_field || '')}" placeholder="title_field">
      <input class="st-field" data-key="date_field"
             value="${escapeHtml(t.date_field || '')}" placeholder="date_field">
      <button class="st-del" type="button" aria-label="删除">✕</button>
    </div>
  `).join('');
  tbody.innerHTML = rows || '<div class="sync-targets-empty">还没有同步表</div>';
  tbody.querySelectorAll('.st-row').forEach(rowEl => {
    const col = rowEl.dataset.collection;
    rowEl.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.addEventListener('change', () =>
        patchSyncTarget(col, { [cb.dataset.key]: cb.checked }));
    });
    rowEl.querySelectorAll('input.st-field').forEach(inp => {
      inp.addEventListener('change', () =>
        patchSyncTarget(col, { [inp.dataset.key]: inp.value.trim() }));
    });
    rowEl.querySelector('.st-del').addEventListener('click', () =>
      confirmDeleteSyncTarget(col));
  });
}

export async function patchSyncTarget(collection, patch) {
  try {
    await apiPatch('/api/sync/targets/' + encodeURIComponent(collection), patch);
  } catch (e) {
    alert('保存失败: ' + e);
    loadSyncTargets();        // re-render with server's actual state
  }
}

export async function confirmDeleteSyncTarget(collection) {
  if (!confirm('停止同步 `' + collection + '`?\nNotion DB 将保留(不会删除)。')) return;
  try {
    await apiDelete('/api/sync/targets/' + encodeURIComponent(collection));
    loadSyncTargets();
  } catch (e) {
    alert('删除失败: ' + e);
  }
}

export function openAddSyncTarget() {
  const dlg = document.getElementById('sync-add-dialog');
  const selColl = document.getElementById('sa-collection');
  const selTitle = document.getElementById('sa-title-field');
  const selDate  = document.getElementById('sa-date-field');
  const cbAuto   = document.getElementById('sa-auto-sync');
  selColl.innerHTML = '';
  (window.__sync_available || []).forEach(av => {
    const opt = document.createElement('option');
    opt.value = av.collection;
    opt.textContent = av.collection;
    selColl.appendChild(opt);
  });
  function refreshFieldDropdowns() {
    const sel = (window.__sync_available || []).find(a => a.collection === selColl.value);
    selTitle.innerHTML = '';
    selDate.innerHTML  = '<option value="">— (不用日期)</option>';
    (sel ? sel.fields : []).forEach(f => {
      const ot = document.createElement('option');
      ot.value = f.name; ot.textContent = f.name + ' (' + f.type + ')';
      if (f.type === 'text' && (f.name === 'title' || f.name === 'name')) {
        ot.selected = true;
      }
      selTitle.appendChild(ot);
      if (f.type === 'date') {
        const od = document.createElement('option');
        od.value = f.name; od.textContent = f.name; selDate.appendChild(od);
      }
    });
  }
  selColl.onchange = refreshFieldDropdowns;
  refreshFieldDropdowns();
  cbAuto.checked = true;
  dlg.showModal();
  document.getElementById('sa-submit').onclick = async () => {
    const payload = {
      collection: selColl.value,
      title_field: selTitle.value,
      date_field: selDate.value,
      auto_sync: cbAuto.checked,
    };
    try {
      await apiPost('/api/sync/targets', payload);
      dlg.close();
      alert('已创建,后台正在做首次对齐');
      loadSyncTargets();
    } catch (e) {
      alert('创建失败: ' + e);
    }
  };
}

export function fillSyncSettings(data) {
  document.getElementById('ss-paused').checked = !!data.paused;
  document.getElementById('ss-tz').value = data.timezone || 'America/New_York';
  document.getElementById('ss-h1').value = (data.sync_hour_local == null || data.sync_hour_local === '') ? '' : data.sync_hour_local;
  document.getElementById('ss-h2').value = (data.sync_hour_local_2 == null || data.sync_hour_local_2 === '') ? '' : data.sync_hour_local_2;
  document.getElementById('ss-last').textContent = data.last_run_at || '—';
}

export function setSSStatus(msg, isError) {
  const el = document.getElementById('ss-status');
  if (!el) return;
  el.textContent = msg || '';
  el.classList.toggle('error', !!isError);
}
