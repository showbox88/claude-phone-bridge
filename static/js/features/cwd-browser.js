/**
 * CWD / file browser modal.
 *
 * Two modes:
 *   - 'cwd':  navigate the filesystem and pick a directory; clicking
 *             "✓ 切到此处" sends a {type:'cmd', name:'cwd', path} frame
 *             which the server applies to the current session.
 *   - 'file': navigate the filesystem and pick one or more files; clicking
 *             a file appends its absolute path to pendingFiles so it ships
 *             with the next user_message frame.
 *
 * Ported verbatim from legacy app.js lines 2385-2519. Behavioral changes:
 *   - raw fetch → apiGet/apiPost from api.js
 *   - send(...) → sendWs(...) from ws/socket.js
 *   - browseState was a module-scoped `let` in legacy; remains module-local
 *
 * DOM/wiring (e.g. filePickBtn.addEventListener) is intentionally NOT done
 * here — Task 14 (boot.js) wires the entry points.
 */
import { apiGet, apiPost } from '../api.js';
import { sendWs } from '../ws/socket.js';
import { escapeHtml } from '../util/escape.js';
import { pendingFiles, renderAttachBar } from '../composer/attachments.js';

// Module-local browse state (legacy `let browseState = {...}`).
const browseState = { path: '', root: '', abs: '', mode: 'cwd' };

export function ensureCwdModal() {
  let m = document.getElementById('cwd-modal');
  if (m) return m;
  m = document.createElement('div');
  m.id = 'cwd-modal';
  m.className = 'modal-bg hidden';
  m.innerHTML = `
    <div class="modal">
      <div class="modal-head">
        <span id="cwd-modal-title">选择</span>
        <button class="icon-btn modal-close" type="button">✕</button>
      </div>
      <div class="modal-breadcrumb" id="cwd-breadcrumb"></div>
      <div class="modal-list" id="cwd-list"></div>
      <div class="modal-foot" id="cwd-foot"></div>
    </div>
  `;
  document.body.appendChild(m);
  m.addEventListener('click', () => m.classList.add('hidden'));
  m.querySelector('.modal').addEventListener('click', (e) => e.stopPropagation());
  m.querySelector('.modal-close').addEventListener('click', () => m.classList.add('hidden'));
  return m;
}

export function openCwdBrowser(mode) {
  browseState.mode = mode || 'cwd';
  const m = ensureCwdModal();
  m.classList.remove('hidden');
  const title = m.querySelector('#cwd-modal-title');
  const foot = m.querySelector('#cwd-foot');
  title.textContent = mode === 'file' ? '选择附加文件' : '切换工作目录';
  foot.innerHTML = '';
  if (mode === 'cwd') {
    const mkdir = document.createElement('button');
    mkdir.className = 'mkdir'; mkdir.type = 'button'; mkdir.textContent = '+ 新建文件夹';
    mkdir.addEventListener('click', mkdirHere);
    const pick = document.createElement('button');
    pick.className = 'pick'; pick.type = 'button'; pick.textContent = '✓ 切到此处';
    pick.addEventListener('click', pickHere);
    foot.appendChild(mkdir); foot.appendChild(pick);
  } else {
    const cancel = document.createElement('button');
    cancel.type = 'button'; cancel.textContent = '取消';
    cancel.addEventListener('click', () => m.classList.add('hidden'));
    foot.appendChild(cancel);
  }
  loadBrowse('');
}

export async function loadBrowse(path) {
  const list = document.getElementById('cwd-list');
  const crumb = document.getElementById('cwd-breadcrumb');
  list.innerHTML = '<div class="empty">加载中…</div>';
  let data;
  try {
    data = await apiGet('/api/browse?path=' + encodeURIComponent(path));
  } catch (e) {
    if (e && e.status) {
      list.innerHTML = `<div class="empty">加载失败 (${e.status})</div>`;
    } else {
      list.innerHTML = `<div class="empty">网络错误: ${escapeHtml(e.message)}</div>`;
    }
    return;
  }
  browseState.path = data.path; browseState.root = data.root; browseState.abs = data.abs;

  crumb.innerHTML =
    `<span class="root-label">主文件夹: ${escapeHtml(data.root)}</span>` +
    `<span class="current">${escapeHtml(data.abs)}</span>`;

  list.innerHTML = '';
  if (data.parent !== null) {
    const up = document.createElement('div');
    up.className = 'entry';
    up.innerHTML = `<span class="icon">↑</span><span class="name">上一级</span>`;
    up.addEventListener('click', () => loadBrowse(data.parent));
    list.appendChild(up);
  }
  if (data.entries.length === 0 && data.parent === null) {
    const e = document.createElement('div'); e.className = 'empty'; e.textContent = '空目录';
    list.appendChild(e);
  }
  for (const e of data.entries) {
    const el = document.createElement('div');
    el.className = 'entry' + (e.is_dir ? '' : ' file');
    if (!e.is_dir && browseState.mode === 'file') el.classList.add('pickable');
    el.innerHTML =
      `<span class="icon">${e.is_dir ? '📁' : '📄'}</span>` +
      `<span class="name">${escapeHtml(e.name)}</span>`;
    if (e.is_dir) {
      const childPath = data.path ? `${data.path}/${e.name}` : e.name;
      el.addEventListener('click', () => loadBrowse(childPath));
    } else if (browseState.mode === 'file') {
      const childPath = data.path ? `${data.path}/${e.name}` : e.name;
      el.addEventListener('click', () => {
        // build absolute path: root + / + childPath
        const abs = (data.root + '/' + childPath).replace(/\/+/g, '/');
        if (!pendingFiles.includes(abs)) pendingFiles.push(abs);
        renderAttachBar();
        document.getElementById('cwd-modal').classList.add('hidden');
      });
    }
    list.appendChild(el);
  }
}

export async function mkdirHere() {
  const name = prompt('新文件夹名:', '');
  if (!name) return;
  try {
    await apiPost('/api/mkdir', { path: browseState.path, name: name.trim() });
    await loadBrowse(browseState.path);
  } catch (e) {
    if (e && e.status) {
      const detail = (e.body && e.body.detail) || e.status;
      alert('创建失败: ' + detail);
    } else {
      alert('创建出错: ' + e.message);
    }
  }
}

export function pickHere() {
  sendWs({ type: 'cmd', name: 'cwd', path: browseState.path });
  document.getElementById('cwd-modal').classList.add('hidden');
}
