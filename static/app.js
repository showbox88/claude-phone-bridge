// Claude Bridge web client
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const messages = $('messages');
  const input = $('input');
  const sendBtn = $('send-btn');
  const connDot = $('conn-dot');
  const cwdLabel = $('cwd-label');
  const menu = $('menu');
  const menuBtn = $('menu-btn');
  const notifBtn = $('notif-btn');

  // ---------- WebSocket with auto-reconnect ----------
  let ws = null;
  let reconnectDelay = 500;
  let pingTimer = null;

  function setConn(state) {
    connDot.className = 'dot ' + state;
    connDot.title = state;
  }

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    setConn('connecting');
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.addEventListener('open', () => {
      setConn('connected');
      reconnectDelay = 500;
      pingTimer = setInterval(() => {
        if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'ping' }));
      }, 25000);
    });

    ws.addEventListener('message', (e) => {
      try {
        const msg = JSON.parse(e.data);
        handleEvent(msg);
      } catch (err) {
        console.warn('bad ws message', err);
      }
    });

    ws.addEventListener('close', () => {
      setConn('disconnected');
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 1.6, 8000);
    });

    ws.addEventListener('error', () => {
      try { ws.close(); } catch (_) { /* noop */ }
    });
  }

  function send(obj) {
    if (!ws || ws.readyState !== 1) {
      sysMsg('未连接，消息未发送');
      return false;
    }
    ws.send(JSON.stringify(obj));
    return true;
  }

  // ---------- rendering ----------
  let currentAssistantBubble = null;
  let currentAssistantBuffer = '';
  const pendingPerms = new Map(); // id -> card element

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // Minimal markdown: ```code``` blocks, `inline code`, preserves newlines via white-space:pre-wrap.
  function renderMarkdown(text) {
    const parts = [];
    let i = 0;
    const re = /```([a-zA-Z0-9_+-]*)?\n?([\s\S]*?)```/g;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > i) parts.push({ kind: 'text', value: text.slice(i, m.index) });
      parts.push({ kind: 'code', lang: m[1] || '', value: m[2] });
      i = re.lastIndex;
    }
    if (i < text.length) parts.push({ kind: 'text', value: text.slice(i) });

    return parts.map((p) => {
      if (p.kind === 'code') {
        return `<pre><code>${escapeHtml(p.value)}</code></pre>`;
      }
      // inline code only inside text portions
      return escapeHtml(p.value).replace(/`([^`\n]+)`/g, (_, c) => `<code>${c}</code>`);
    }).join('');
  }

  function appendUser(text) {
    const el = document.createElement('div');
    el.className = 'msg user';
    el.textContent = text;
    messages.appendChild(el);
    scrollToBottom(true);
  }

  function appendAssistantText(text) {
    if (!currentAssistantBubble) {
      currentAssistantBubble = document.createElement('div');
      currentAssistantBubble.className = 'msg assistant';
      currentAssistantBuffer = '';
      messages.appendChild(currentAssistantBubble);
    }
    currentAssistantBuffer += text;
    currentAssistantBubble.innerHTML = renderMarkdown(currentAssistantBuffer);
    scrollToBottom(false);
  }

  function appendToolUse(tool, inp) {
    const el = document.createElement('div');
    el.className = 'tool-block';
    const inputJson = typeof inp === 'string' ? inp : JSON.stringify(inp, null, 2);
    el.innerHTML = `<div class="tool-name">▶ ${escapeHtml(tool)}</div><pre>${escapeHtml(inputJson)}</pre>`;
    messages.appendChild(el);
    scrollToBottom(true);
  }

  function appendToolResult(ok, content) {
    const el = document.createElement('div');
    el.className = 'tool-block result' + (ok ? '' : ' error');
    const label = ok ? '✓ result' : '✗ error';
    el.innerHTML = `<div class="tool-name">${label}</div><pre>${escapeHtml(content || '')}</pre>`;
    messages.appendChild(el);
    scrollToBottom(false);
  }

  function appendSystem(text) {
    const el = document.createElement('div');
    el.className = 'msg system';
    el.textContent = text;
    messages.appendChild(el);
    scrollToBottom(false);
  }
  const sysMsg = appendSystem;

  function appendError(text) {
    const el = document.createElement('div');
    el.className = 'msg error';
    el.textContent = text;
    messages.appendChild(el);
    scrollToBottom(true);
  }

  function appendPermissionCard(id, tool, inp) {
    const el = document.createElement('div');
    el.className = 'perm-card';
    el.dataset.id = id;
    const inputJson = typeof inp === 'string' ? inp : JSON.stringify(inp, null, 2);
    el.innerHTML = `
      <div class="perm-head">🔧 Claude 想运行 <span class="tool">${escapeHtml(tool)}</span></div>
      <pre>${escapeHtml(inputJson)}</pre>
      <div class="perm-actions">
        <button class="allow">✅ 允许</button>
        <button class="deny">❌ 拒绝</button>
      </div>
    `;
    el.querySelector('.allow').addEventListener('click', () => respondPerm(id, 'allow'));
    el.querySelector('.deny').addEventListener('click', () => respondPerm(id, 'deny'));
    messages.appendChild(el);
    pendingPerms.set(id, el);
    scrollToBottom(true);

    // also vibrate the device if supported
    if (navigator.vibrate) navigator.vibrate([100, 50, 100]);
  }

  function respondPerm(id, decision) {
    if (!send({ type: 'permission_response', id, decision })) return;
    const el = pendingPerms.get(id);
    if (el) {
      el.classList.add('resolved');
      const head = el.querySelector('.perm-head');
      head.innerHTML += ` · <span style="opacity:.7">${decision === 'allow' ? '已允许' : '已拒绝'}</span>`;
      pendingPerms.delete(id);
    }
  }

  function scrollToBottom(force) {
    const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 120;
    if (force || nearBottom) {
      messages.scrollTop = messages.scrollHeight;
    }
  }

  // ---------- event router ----------
  function handleEvent(msg) {
    switch (msg.type) {
      case 'system':
        if (msg.msg && msg.msg.startsWith('connected')) {
          // extract cwd
          const m = msg.msg.match(/cwd=(.+)$/);
          if (m) cwdLabel.textContent = m[1];
        }
        appendSystem(msg.msg || '');
        break;
      case 'error':
        appendError(msg.msg || 'error');
        currentAssistantBubble = null;
        break;
      case 'user_echo':
        // Render here (not on local send), so all connected devices stay in sync.
        appendUser(msg.text || '');
        break;
      case 'assistant_text':
        appendAssistantText(msg.text || '');
        break;
      case 'tool_use':
        currentAssistantBubble = null; // start fresh bubble after tool
        appendToolUse(msg.tool, msg.input);
        break;
      case 'tool_result':
        appendToolResult(msg.ok, msg.content);
        break;
      case 'permission_request':
        currentAssistantBubble = null;
        appendPermissionCard(msg.id, msg.tool, msg.input);
        break;
      case 'turn_done':
        currentAssistantBubble = null;
        if (msg.cost_usd != null) {
          appendSystem(`done · $${msg.cost_usd.toFixed(4)}`);
        }
        break;
      case 'pong':
        break;
      default:
        console.warn('unknown event', msg);
    }
  }

  // ---------- input ----------
  function autoresize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';
  }

  function sendCurrent() {
    const text = input.value.trim();
    if (!text) return;

    // slash commands handled client-side
    if (text === '/new') {
      send({ type: 'cmd', name: 'new' });
    } else if (text === '/cancel') {
      send({ type: 'cmd', name: 'cancel' });
    } else if (text.startsWith('/cwd ')) {
      const path = text.slice(5).trim();
      send({ type: 'cmd', name: 'cwd', path });
    } else {
      if (!send({ type: 'user_message', text })) return;  // keep input if WS down
    }
    input.value = '';
    autoresize();
  }

  input.addEventListener('input', autoresize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      sendCurrent();
    }
  });
  sendBtn.addEventListener('click', sendCurrent);

  // ---------- menu ----------
  menuBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    menu.classList.toggle('hidden');
  });
  document.addEventListener('click', () => menu.classList.add('hidden'));
  menu.addEventListener('click', (e) => e.stopPropagation());
  menu.querySelectorAll('button').forEach((b) => {
    b.addEventListener('click', () => {
      const cmd = b.dataset.cmd;
      menu.classList.add('hidden');
      if (cmd === 'new') send({ type: 'cmd', name: 'new' });
      else if (cmd === 'cancel') send({ type: 'cmd', name: 'cancel' });
      else if (cmd === 'cwd-prompt') openCwdBrowser();
    });
  });

  // ---------- directory picker ----------
  let browseState = { path: '', root: '', abs: '' };

  function ensureCwdModal() {
    let m = document.getElementById('cwd-modal');
    if (m) return m;
    m = document.createElement('div');
    m.id = 'cwd-modal';
    m.className = 'modal-bg hidden';
    m.innerHTML = `
      <div class="modal">
        <div class="modal-head">
          <span>切换工作目录</span>
          <button class="icon-btn modal-close" type="button">✕</button>
        </div>
        <div class="modal-breadcrumb" id="cwd-breadcrumb"></div>
        <div class="modal-list" id="cwd-list"></div>
        <div class="modal-foot">
          <button class="mkdir" type="button">+ 新建文件夹</button>
          <button class="pick" type="button">✓ 切到此处</button>
        </div>
      </div>
    `;
    document.body.appendChild(m);
    m.addEventListener('click', () => m.classList.add('hidden'));
    m.querySelector('.modal').addEventListener('click', (e) => e.stopPropagation());
    m.querySelector('.modal-close').addEventListener('click', () => m.classList.add('hidden'));
    m.querySelector('.mkdir').addEventListener('click', mkdirHere);
    m.querySelector('.pick').addEventListener('click', pickHere);
    return m;
  }

  async function openCwdBrowser() {
    ensureCwdModal().classList.remove('hidden');
    await loadBrowse('');
  }

  async function loadBrowse(path) {
    const list = document.getElementById('cwd-list');
    const crumb = document.getElementById('cwd-breadcrumb');
    list.innerHTML = '<div class="empty">加载中…</div>';
    let data;
    try {
      const resp = await fetch('/api/browse?path=' + encodeURIComponent(path));
      if (!resp.ok) {
        list.innerHTML = `<div class="empty">加载失败 (${resp.status})</div>`;
        return;
      }
      data = await resp.json();
    } catch (e) {
      list.innerHTML = `<div class="empty">网络错误: ${escapeHtml(e.message)}</div>`;
      return;
    }
    browseState = { path: data.path, root: data.root, abs: data.abs };

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
      const e = document.createElement('div');
      e.className = 'empty';
      e.textContent = '空目录';
      list.appendChild(e);
    }
    for (const e of data.entries) {
      const el = document.createElement('div');
      el.className = 'entry' + (e.is_dir ? '' : ' file');
      el.innerHTML =
        `<span class="icon">${e.is_dir ? '📁' : '📄'}</span>` +
        `<span class="name">${escapeHtml(e.name)}</span>`;
      if (e.is_dir) {
        const childPath = data.path ? `${data.path}/${e.name}` : e.name;
        el.addEventListener('click', () => loadBrowse(childPath));
      }
      list.appendChild(el);
    }
  }

  async function mkdirHere() {
    const name = prompt('新文件夹名:', '');
    if (!name) return;
    try {
      const resp = await fetch('/api/mkdir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: browseState.path, name: name.trim() }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        alert('创建失败: ' + (err.detail || resp.status));
        return;
      }
      await loadBrowse(browseState.path);
    } catch (e) {
      alert('创建出错: ' + e.message);
    }
  }

  function pickHere() {
    send({ type: 'cmd', name: 'cwd', path: browseState.path });
    document.getElementById('cwd-modal').classList.add('hidden');
  }

  // ---------- push notifications ----------
  function urlBase64ToUint8Array(b64) {
    const padding = '='.repeat((4 - b64.length % 4) % 4);
    const base64 = (b64 + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; ++i) out[i] = raw.charCodeAt(i);
    return out;
  }

  async function setupPush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      sysMsg('当前浏览器不支持推送');
      return;
    }
    try {
      const reg = await navigator.serviceWorker.register('/sw.js');
      await navigator.serviceWorker.ready;

      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        sysMsg('未授权通知');
        return;
      }

      const keyResp = await fetch('/api/vapid-public-key').then((r) => r.json());
      if (!keyResp.key) {
        sysMsg('服务端未配置 VAPID_PUBLIC_KEY');
        return;
      }

      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(keyResp.key),
        });
      }

      await fetch('/api/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub),
      });

      notifBtn.classList.add('active');
      notifBtn.title = '推送已启用';
      sysMsg('推送已启用');
    } catch (e) {
      console.error(e);
      sysMsg('推送启用失败: ' + e.message);
    }
  }

  notifBtn.addEventListener('click', setupPush);

  // Auto-register SW (without subscribing) so push events work after user enables later.
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* ignore */ });
  }

  // ---------- visibility re-focus ----------
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && (!ws || ws.readyState !== 1)) {
      connect();
    }
  });

  // ---------- go ----------
  connect();
})();
