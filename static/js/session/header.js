/**
 * Header / mode-picker / model-picker / YOLO-toggle UI updates.
 *
 * Ported from legacy app.js (setHeader / setMode / setModel /
 * refreshModelPill / setAutoApprove around lines 711-762,
 * renderModelMenu around lines 1535-1574).
 *
 * Pure UI updates + WS commands for user-initiated changes. Reads/writes
 * currentMode / currentModel / autoApprove / currentSessionTitle via the
 * state store. Click handlers in renderModelMenu emit cmd:set_model and
 * cmd:set_auto_approve through ws/socket.sendWs (porting legacy
 * behavior); the boot module (Task 14) wires up the mode toggle.
 */
import {
  cwdLabel,
  sessionTitle,
  input,
  modelBtn,
  modelLabel,
  modelMenu,
  workspaceIndicator,
} from '../dom.js';
import { get, set } from '../state.js';
import { sendWs } from '../ws/socket.js';

export function setHeader(title, cwd) {
  set('currentSessionTitle', title || '');
  if (sessionTitle) sessionTitle.textContent = title || 'Claude';
  if (cwdLabel) cwdLabel.textContent = cwd ? cwd : '/';
}

export function setMode(mode) {
  const next = mode || 'code';
  set('currentMode', next);
  document.querySelectorAll('#workspace-toggle .seg-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.workspace === next);
  });
  const ind = workspaceIndicator;
  if (ind) {
    const ic = (window.icon && window.icon(next === 'chat' ? 'chat' : 'code', 13)) || '';
    ind.innerHTML = `<span class="wi-ic">${ic}</span><span>${next === 'chat' ? 'Chat' : 'Code'}</span>`;
    ind.classList.toggle('chat', next === 'chat');
    ind.classList.toggle('code', next === 'code');
  }
  document.body.classList.toggle('mode-chat', next === 'chat');
  document.body.classList.toggle('mode-code', next === 'code');
  if (input) input.placeholder = '';
  // Adapt empty-state hint
  const hint = document.querySelector('#empty-state .hint');
  if (hint) {
    hint.innerHTML = next === 'chat'
      ? '和 Claude 聊天<br><small>支持发图片让我看，纯对话不操作文件</small>'
      : '开始和 Claude 对话<br><small>添加图片，或选择电脑端文件</small>';
  }
}

export function setModel(model) {
  set('currentModel', model || '');
  refreshModelPill();
}

export function refreshModelPill() {
  if (!modelLabel) return;
  const META = get('META') || {};
  const currentModel = get('currentModel');
  const autoApprove = get('autoApprove');
  const m = (META.models || []).find((x) => x.id === currentModel);
  const base = (m && m.label) || '默认';
  modelLabel.textContent = autoApprove ? `🚀 ${base}` : base;
  if (modelBtn) modelBtn.classList.toggle('yolo', !!autoApprove);
}

export function setAutoApprove(value) {
  set('autoApprove', !!value);
  refreshModelPill();
  // Re-render menu if it's open so the toggle row reflects current state.
  if (modelMenu && !modelMenu.classList.contains('hidden')) renderModelMenu();
}

export function renderModelMenu() {
  if (!modelMenu) return;
  modelMenu.innerHTML = '';
  const META = get('META') || {};
  const autoApprove = get('autoApprove');
  const currentModel = get('currentModel');

  // YOLO toggle — sits above the model list. Clicking does not close the
  // menu so the user can confirm the visual state change before dismissing.
  const yolo = document.createElement('button');
  yolo.type = 'button';
  yolo.className = 'model-item yolo-toggle' + (autoApprove ? ' on' : '');
  yolo.innerHTML = `
    <span class="label">🚀 自动批准请求</span>
    <span class="desc">${autoApprove ? '已开启 · 点击关闭' : '关闭 · 点击开启'}</span>
    <span class="yolo-dot${autoApprove ? ' on' : ''}"></span>`;
  yolo.addEventListener('click', (e) => {
    e.stopPropagation();
    const next = !get('autoApprove');
    setAutoApprove(next);
    sendWs({ type: 'cmd', name: 'set_auto_approve', value: next });
  });
  modelMenu.appendChild(yolo);

  const sep = document.createElement('div');
  sep.className = 'model-sep';
  modelMenu.appendChild(sep);

  for (const m of META.models || []) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'model-item' + (m.id === currentModel ? ' active' : '');
    item.innerHTML = `<span class="label"></span><span class="desc"></span>`;
    item.querySelector('.label').textContent = m.label;
    item.querySelector('.desc').textContent = m.desc || '';
    item.addEventListener('click', () => {
      modelMenu.classList.add('hidden');
      if (m.id === get('currentModel')) return;
      setModel(m.id);
      sendWs({ type: 'cmd', name: 'set_model', model: m.id });
    });
    modelMenu.appendChild(item);
  }
}
