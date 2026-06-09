/**
 * Usage statistics modal — today / 30 days / cumulative cost + tokens + by-model bars.
 *
 * Single API call to GET /api/usage which the server aggregates from the
 * sessions store. Modal is created once on first open and reused.
 *
 * Ported verbatim from legacy app.js lines 1605-1698. Behavioral changes:
 *   - raw fetch → apiGet
 *   - pct() helper was local to legacy app.js (lines 1695-1698); we re-use
 *     the identical pct from util/format.js
 */
import { apiGet } from '../api.js';
import { fmtMoney, fmtTokens, pct } from '../util/format.js';
import { escapeHtml } from '../util/escape.js';

export async function openUsageModal() {
  let m = document.getElementById('usage-modal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'usage-modal';
    m.className = 'modal-bg usage-modal hidden';
    m.innerHTML = `
      <div class="modal">
        <div class="modal-head">
          <span>📊 使用量统计</span>
          <button class="icon-btn modal-close" type="button">✕</button>
        </div>
        <div id="usage-body" class="usage-body"></div>
      </div>
    `;
    document.body.appendChild(m);
    m.addEventListener('click', () => m.classList.add('hidden'));
    m.querySelector('.modal').addEventListener('click', (e) => e.stopPropagation());
    m.querySelector('.modal-close').addEventListener('click', () => m.classList.add('hidden'));
  }
  m.classList.remove('hidden');
  const body = m.querySelector('#usage-body');
  body.innerHTML = '<div class="empty" style="padding:30px;text-align:center;color:var(--text-3)">加载中…</div>';
  let data;
  try {
    data = await apiGet('/api/usage');
  } catch (e) {
    body.innerHTML = `<div class="empty" style="padding:30px;text-align:center;color:var(--error)">加载失败: ${escapeHtml(e.message)}</div>`;
    return;
  }
  renderUsage(body, data);
}

export function renderUsage(body, data) {
  const t = data.total || {};
  const today = data.today || {};
  const month = data.month || {};
  const byModel = data.by_model || [];
  const totalTokens = (t.in_tok || 0) + (t.out_tok || 0);

  const maxModelCost = byModel.reduce((a, m) => Math.max(a, m.cost || 0), 0) || 1;

  body.innerHTML = `
    <div class="usage-grid">
      <div class="stat">
        <div class="num">${escapeHtml(fmtMoney(today.cost))}</div>
        <div class="lbl">今日花销 · ${today.turns || 0} 轮</div>
      </div>
      <div class="stat">
        <div class="num">${escapeHtml(fmtMoney(month.cost))}</div>
        <div class="lbl">近30天 · ${month.turns || 0} 轮</div>
      </div>
      <div class="stat">
        <div class="num">${escapeHtml(fmtMoney(t.cost))}</div>
        <div class="lbl">累计 · ${t.turns || 0} 轮</div>
      </div>
    </div>
    <div class="usage-section">
      <h4>Token 总量</h4>
      <div class="usage-bars">
        <div class="usage-bar"><span class="name">输入</span><div class="bar-track"><div class="bar-fill" style="width:${pct(t.in_tok, totalTokens)}%"></div></div><span class="val">${fmtTokens(t.in_tok || 0)}</span></div>
        <div class="usage-bar"><span class="name">输出</span><div class="bar-track"><div class="bar-fill" style="width:${pct(t.out_tok, totalTokens)}%; background:#88c"></div></div><span class="val">${fmtTokens(t.out_tok || 0)}</span></div>
        <div class="usage-bar"><span class="name">缓存读</span><div class="bar-track"><div class="bar-fill" style="width:${pct(t.cache_read, t.cache_read + t.cache_create + 1)}%; background:#7a7"></div></div><span class="val">${fmtTokens(t.cache_read || 0)}</span></div>
        <div class="usage-bar"><span class="name">缓存写</span><div class="bar-track"><div class="bar-fill" style="width:${pct(t.cache_create, t.cache_read + t.cache_create + 1)}%; background:#aa7"></div></div><span class="val">${fmtTokens(t.cache_create || 0)}</span></div>
      </div>
    </div>
    <div class="usage-section">
      <h4>按模型分布</h4>
      <div class="usage-bars" id="usage-by-model"></div>
    </div>
  `;
  const byModelEl = body.querySelector('#usage-by-model');
  if (byModel.length === 0) {
    byModelEl.innerHTML = '<div style="color:var(--text-3); font-size:12px">暂无数据</div>';
  } else {
    for (const m of byModel) {
      const row = document.createElement('div');
      row.className = 'usage-bar';
      const lbl = m.model || '默认';
      row.innerHTML = `
        <span class="name">${escapeHtml(lbl)}</span>
        <div class="bar-track"><div class="bar-fill" style="width:${pct(m.cost, maxModelCost)}%"></div></div>
        <span class="val">${escapeHtml(fmtMoney(m.cost))} · ${m.turns}轮</span>
      `;
      byModelEl.appendChild(row);
    }
  }
}
