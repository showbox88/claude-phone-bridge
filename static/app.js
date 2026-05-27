// Claude Bridge web client
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const messagesScroll = $('messages');
  const messages = messagesScroll.querySelector('.messages-inner') || messagesScroll;
  const emptyState = $('empty-state');
  const input = $('input');
  const sendBtn = $('send-btn');
  const connDot = $('conn-dot');
  const cwdLabel = $('cwd-label');
  const sessionTitle = $('session-title');
  const menu = $('menu');
  const menuBtn = $('menu-btn');
  const notifBtn = $('notif-btn');
  const attachBtn = $('attach-btn');
  const pasteBtn = $('paste-btn');
  const cameraBtn = $('camera-btn');
  const attachMenu = $('attach-menu');
  const albumInput = $('album-input');
  const galleryInput = $('gallery-input');
  const filePickBtn = $('file-pick-btn');
  const fileInput = $('file-input');
  const cameraInput = $('camera-input');
  const attachBar = $('attach-bar');
  const drawer = $('drawer');
  const drawerMask = $('drawer-mask');
  const drawerBtn = $('drawer-btn');
  const drawerClose = $('drawer-close');
  const newSessionBtn = $('new-session-btn');
  const sessionListEl = $('session-list');

  let currentSessionId = null;
  let currentSessionTitle = '';
  let currentMode = 'code';
  let currentModel = '';
  let META = { modes: [], models: [] };
  let isResponding = false;  // true while Claude is generating a turn

  // ---------- multi-source (multiple Phone Bridge backends) ----------
  const SOURCES_KEY = 'bridge.sources';
  const CURRENT_KEY = 'bridge.current_source_id';

  function loadSources() {
    try { return JSON.parse(localStorage.getItem(SOURCES_KEY) || '[]'); }
    catch (_) { return []; }
  }
  function saveSources(arr) {
    localStorage.setItem(SOURCES_KEY, JSON.stringify(arr));
  }
  function getCurrentSourceId() { return localStorage.getItem(CURRENT_KEY) || ''; }
  function setCurrentSourceId(id) {
    if (id) localStorage.setItem(CURRENT_KEY, id);
    else localStorage.removeItem(CURRENT_KEY);
  }
  function findSource(id) { return loadSources().find((s) => s.id === id) || null; }

  // The currently active source. When null, we show the source picker overlay.
  let currentSource = null;

  // URL helpers — every API/WS call routes through the active source.
  function apiUrl(path) {
    if (!currentSource) return path;
    return currentSource.url.replace(/\/$/, '') + path;
  }
  function wsUrl() {
    if (!currentSource) return null;
    return currentSource.url.replace(/^http/, 'ws').replace(/\/$/, '') + '/ws';
  }
  function assetUrl(path) { return apiUrl(path); }
  // pending image attachments staged for next send
  const pendingAttachments = [];
  // pending absolute file paths picked from browser modal
  const pendingFiles = [];

  // ---------- WebSocket with auto-reconnect ----------
  let ws = null;
  let reconnectDelay = 500;
  let pingTimer = null;

  function setConn(state) {
    connDot.className = 'dot ' + state;
    connDot.title = state;
  }

  let reconnectTimer = null;

  function connect() {
    // Skip if already connecting/open — prevents stacking multiple WS on mobile
    // when visibility changes fire repeated reconnect attempts.
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

    const url = wsUrl();
    if (!url) { setConn('disconnected'); return; }
    setConn('connecting');
    const sock = new WebSocket(url);
    ws = sock;

    sock.addEventListener('open', () => {
      if (sock !== ws) { try { sock.close(); } catch (_) {} return; }
      setConn('connected');
      reconnectDelay = 500;
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = setInterval(() => {
        if (sock.readyState === 1) sock.send(JSON.stringify({ type: 'ping' }));
      }, 25000);
    });

    sock.addEventListener('message', (e) => {
      // Drop messages from any stale socket that hasn't fully closed yet.
      if (sock !== ws) return;
      try { handleEvent(JSON.parse(e.data)); }
      catch (err) { console.warn('bad ws message', err); }
    });

    sock.addEventListener('close', () => {
      if (sock !== ws) return; // a newer socket has already taken over
      setConn('disconnected');
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
      reconnectTimer = setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 1.6, 8000);
    });

    sock.addEventListener('error', () => {
      // Close THIS socket — never the module-level ws, which may already point
      // at a newer connection.
      try { sock.close(); } catch (_) { /* noop */ }
    });
  }

  // Force-reconnect (and drop stale sockets) when the page comes back to the
  // foreground on mobile. iOS especially likes to keep a half-dead WS around
  // that says readyState=1 but no longer delivers messages — which means we
  // miss the permission_request that fired while we were backgrounded. So we
  // unconditionally close any existing WS on visibility-change and reconnect.
  // The fresh WS triggers a new `hello` from the server, which carries any
  // unanswered pending_perms so the card re-renders.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (!currentSource) return;
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
    connect();
  });

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
  let currentToolGroup = null; // wraps consecutive tool_use/result into one collapsible block
  let typingEl = null;         // "Claude is working" three-dot indicator

  function showTyping() {
    if (typingEl && typingEl.parentNode === messages) {
      messages.appendChild(typingEl); // keep it last
      return;
    }
    hideEmptyState();
    typingEl = document.createElement('div');
    typingEl.className = 'typing';
    typingEl.setAttribute('aria-label', 'Claude 正在工作');
    typingEl.innerHTML = '<span></span><span></span><span></span>';
    messages.appendChild(typingEl);
    scrollToBottom(false);
  }
  function hideTyping() {
    if (typingEl && typingEl.parentNode) typingEl.parentNode.removeChild(typingEl);
    typingEl = null;
  }
  function bumpTyping() {
    if (typingEl && messages.lastElementChild !== typingEl) messages.appendChild(typingEl);
  }

  function ensureToolGroup() {
    if (currentToolGroup) return currentToolGroup;
    const wrap = document.createElement('details');
    wrap.className = 'tool-group';
    wrap.innerHTML = `
      <summary>
        <span class="tg-icon">▸</span>
        <span class="tg-label">工具调用</span>
        <span class="tg-count">0</span>
      </summary>
      <div class="tg-body"></div>
    `;
    messages.appendChild(wrap);
    currentToolGroup = wrap;
    bumpTyping();
    return wrap;
  }

  function bumpToolGroupCount() {
    if (!currentToolGroup) return;
    const body = currentToolGroup.querySelector('.tg-body');
    const cnt = currentToolGroup.querySelector('.tg-count');
    if (cnt && body) cnt.textContent = String(body.children.length);
  }

  function closeToolGroup() { currentToolGroup = null; }
  let currentAssistantBuffer = '';
  const pendingPerms = new Map(); // id -> card element

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // Configure marked: GFM (tables, strikethrough), preserve line breaks
  if (window.marked) {
    window.marked.setOptions({
      gfm: true,
      breaks: true,
      headerIds: false,
      mangle: false,
    });
  }

  function renderMarkdown(text) {
    if (!window.marked) {
      // Minimal fallback if marked failed to load
      return escapeHtml(text).replace(/\n/g, '<br>');
    }
    const html = window.marked.parse(text);
    // Wrap each <pre> in a copy-able container so the existing copy-btn handler picks it up.
    const copySvg = (window.icon && window.icon('copy', 14)) || '⧉';
    return html.replace(
      /<pre>(<code[\s\S]*?<\/code>)<\/pre>/g,
      `<div class="code-block"><button class="copy-btn" type="button" title="复制">${copySvg}</button><pre>$1</pre></div>`
    );
  }

  // ---------- copy-to-clipboard (event delegation) ----------
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) { /* fall through */ }
    // Fallback for http (e.g. localhost over plain HTTP)
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { /* ignore */ }
    document.body.removeChild(ta);
    return ok;
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('.copy-btn');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const wrap = btn.closest('.code-block, .tool-block, .perm-card');
    const pre = wrap && wrap.querySelector('pre');
    if (!pre) return;
    const text = pre.innerText;
    copyText(text).then((ok) => {
      const old = btn.innerHTML;
      const sz = parseInt(btn.querySelector('svg')?.getAttribute('width') || '14', 10);
      const fb = (window.icon && window.icon(ok ? 'check' : 'x', sz)) || (ok ? '✓' : '✗');
      btn.innerHTML = fb;
      btn.classList.toggle('ok', ok);
      btn.classList.toggle('fail', !ok);
      setTimeout(() => {
        btn.innerHTML = old;
        btn.classList.remove('ok', 'fail');
      }, 1200);
    });
  });

  function hideEmptyState() {
    if (emptyState && emptyState.parentNode) emptyState.remove();
  }

  function appendUser(text, images, files) {
    hideEmptyState();
    const el = document.createElement('div');
    el.className = 'msg user';
    if (Array.isArray(images) && images.length) {
      // Split into image + document attachments based on file extension.
      const IMG_RX = /\.(png|jpe?g|webp|gif)(\?|$)/i;
      const imgList = [], docList = [];
      for (const img of images) {
        const url = typeof img === 'string' ? assetUrl(`/uploads/${img}`) : img.url;
        const name = (typeof img === 'string' ? img.split('/').pop() : (img.name || ''));
        const isImg = IMG_RX.test(url);
        if (isImg) imgList.push({ url, name });
        else docList.push({ url, name });
      }
      if (imgList.length) {
        const grid = document.createElement('div');
        grid.className = 'img-grid';
        for (const im of imgList) {
          const a = document.createElement('a');
          a.href = im.url; a.target = '_blank'; a.rel = 'noopener';
          const i = document.createElement('img');
          i.src = im.url; i.loading = 'lazy';
          a.appendChild(i);
          grid.appendChild(a);
        }
        el.appendChild(grid);
      }
      if (docList.length) {
        const dl = document.createElement('div');
        dl.className = 'doc-list';
        for (const d of docList) {
          const ext = (d.url.match(/\.([a-z0-9]+)(\?|$)/i) || [, ''])[1].toLowerCase();
          const ic = ext === 'pdf'                     ? (window.icon ? window.icon('file_pdf',   18) : '')
                   : (ext === 'xlsx' || ext === 'xls') ? (window.icon ? window.icon('file_sheet', 18) : '')
                   :                                     (window.icon ? window.icon('file',        18) : '');
          const a = document.createElement('a');
          a.href = d.url; a.target = '_blank'; a.rel = 'noopener';
          a.className = 'doc-link';
          a.innerHTML = `<span class="doc-icon">${ic}</span><span class="doc-name"></span>`;
          a.querySelector('.doc-name').textContent = d.name || 'file';
          dl.appendChild(a);
        }
        el.appendChild(dl);
      }
    }
    if (Array.isArray(files) && files.length) {
      const fl = document.createElement('div');
      fl.className = 'file-list';
      const clipIcon = (window.icon && window.icon('paperclip', 13)) || '📎';
      fl.innerHTML = `<span class="fl-ic">${clipIcon}</span><span>${escapeHtml(files.join('  ·  '))}</span>`;
      el.appendChild(fl);
    }
    if (text) {
      const t = document.createElement('div');
      t.className = 'msg-text';
      t.textContent = text;
      el.appendChild(t);
    }
    messages.appendChild(el);
    bumpTyping();
    scrollToBottom(true);
  }

  function appendAssistantText(text) {
    hideEmptyState();
    if (!currentAssistantBubble) {
      closeToolGroup();
      currentAssistantBubble = document.createElement('div');
      currentAssistantBubble.className = 'msg assistant';
      currentAssistantBuffer = '';
      messages.appendChild(currentAssistantBubble);
      bumpTyping();
    }
    currentAssistantBuffer += text;
    currentAssistantBubble.innerHTML = renderMarkdown(currentAssistantBuffer);
    scrollToBottom(false);
  }

  function appendToolUse(tool, inp) {
    hideEmptyState();
    const group = ensureToolGroup();
    const body = group.querySelector('.tg-body');
    const el = document.createElement('details');
    el.className = 'tool-block';
    const inputJson = typeof inp === 'string' ? inp : JSON.stringify(inp, null, 2);
    const playIcon = (window.icon && window.icon('play', 11)) || '▶';
    const copyIcon = (window.icon && window.icon('copy', 13)) || '⧉';
    el.innerHTML = `
      <summary>
        <span class="tool-icon">${playIcon}</span>
        <span class="tool-name">${escapeHtml(tool)}</span>
        <button class="copy-btn inline" type="button" title="复制">${copyIcon}</button>
      </summary>
      <pre>${escapeHtml(inputJson)}</pre>
    `;
    body.appendChild(el);
    bumpToolGroupCount();
    scrollToBottom(false);
  }

  function appendToolResult(ok, content) {
    hideEmptyState();
    const group = ensureToolGroup();
    const body = group.querySelector('.tg-body');
    const el = document.createElement('details');
    el.className = 'tool-block ' + (ok ? 'result' : 'error');
    const icon = (window.icon && window.icon(ok ? 'check' : 'x', 13)) || (ok ? '✓' : '✗');
    const copyIcon = (window.icon && window.icon('copy', 13)) || '⧉';
    const label = ok ? 'result' : 'error';
    el.innerHTML = `
      <summary>
        <span class="tool-icon">${icon}</span>
        <span class="tool-name">${label}</span>
        <button class="copy-btn inline" type="button" title="复制">${copyIcon}</button>
      </summary>
      <pre>${escapeHtml(content || '')}</pre>
    `;
    body.appendChild(el);
    bumpToolGroupCount();
    scrollToBottom(false);
  }

  function appendSystem(text) {
    hideEmptyState();
    const el = document.createElement('div');
    el.className = 'msg system';
    el.textContent = text;
    messages.appendChild(el);
    bumpTyping();
    scrollToBottom(false);
  }
  const sysMsg = appendSystem;

  function appendError(text) {
    hideEmptyState();
    const el = document.createElement('div');
    el.className = 'msg error';
    el.textContent = text;
    messages.appendChild(el);
    bumpTyping();
    scrollToBottom(true);
  }

  function appendPermissionCard(id, tool, inp) {
    hideEmptyState();
    const el = document.createElement('div');
    el.className = 'perm-card';
    el.dataset.id = id;
    const inputJson = typeof inp === 'string' ? inp : JSON.stringify(inp, null, 2);
    const toolIcon = (window.icon && window.icon('tool', 16)) || '';
    const copyIcon = (window.icon && window.icon('copy', 14)) || '⧉';
    el.innerHTML = `
      <div class="perm-head">
        <span class="ph-icon">${toolIcon}</span>
        <span>Claude 想运行 <span class="tool">${escapeHtml(tool)}</span></span>
        <button class="copy-btn inline" type="button" title="复制">${copyIcon}</button>
      </div>
      <pre>${escapeHtml(inputJson)}</pre>
      <div class="perm-actions">
        <button type="button" class="deny">拒绝</button>
        <button type="button" class="allow">允许</button>
      </div>
    `;
    el.querySelector('.allow').addEventListener('click', () => respondPerm(id, 'allow'));
    el.querySelector('.deny').addEventListener('click', () => respondPerm(id, 'deny'));
    messages.appendChild(el);
    bumpTyping();
    pendingPerms.set(id, el);
    scrollToBottom(true);
    if (navigator.vibrate) navigator.vibrate([100, 50, 100]);
  }

  function markPermResolved(id, decision) {
    const el = pendingPerms.get(id);
    if (!el || el.classList.contains('resolved')) return;
    el.classList.add('resolved');
    const head = el.querySelector('.perm-head');
    if (head) {
      const tag = document.createElement('span');
      tag.className = 'perm-tag perm-tag-' + (decision || 'unknown');
      tag.textContent = decision === 'allow'   ? '已允许'
                      : decision === 'deny'    ? '已拒绝'
                      : decision === 'timeout' ? '已超时'
                      :                          '已处理';
      head.appendChild(tag);
    }
    el.querySelectorAll('.perm-actions button').forEach((b) => { b.disabled = true; });
    pendingPerms.delete(id);
  }

  function respondPerm(id, decision) {
    if (!send({ type: 'permission_response', id, decision })) return;
    // Local snappy feedback; the server's broadcast will reach back here too
    // but markPermResolved is idempotent so it's a no-op the second time.
    markPermResolved(id, decision);
  }

  // Track whether the user has scrolled away from the bottom. Once true we
  // stop auto-scrolling for streamed chunks until they come back near bottom
  // or a forced scroll happens (turn_done, new user message).
  let stickToBottom = true;
  messagesScroll.addEventListener('scroll', () => {
    const sc = messagesScroll;
    stickToBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 120;
  }, { passive: true });

  function scrollToBottom(force) {
    const sc = messagesScroll;
    if (!force && !stickToBottom) return;
    // Run after layout (rAF) and again on the next frame to catch late
    // re-flow from images, fonts, code-block highlighting, etc.
    requestAnimationFrame(() => {
      sc.scrollTop = sc.scrollHeight;
      requestAnimationFrame(() => { sc.scrollTop = sc.scrollHeight; });
    });
    if (force) stickToBottom = true;
  }

  // When images inside messages finish loading they grow the content; if the
  // user is still pinned to the bottom, follow.
  messagesScroll.addEventListener('load', (e) => {
    if (e.target && e.target.tagName === 'IMG') scrollToBottom(false);
  }, true);

  // ---------- history rendering ----------
  function clearMessages() {
    messages.innerHTML = '';
    if (emptyState) messages.appendChild(emptyState);
    currentAssistantBubble = null; closeToolGroup();
    typingEl = null;  // wiped along with messages.innerHTML
    pendingPerms.clear();
  }

  function renderHistory(msgs) {
    clearMessages();
    if (!msgs || msgs.length === 0) return;
    for (const m of msgs) {
      const c = m.content || {};
      switch (m.role) {
        case 'user':
          appendUser(c.text || '', c.images || [], c.files || []);
          currentAssistantBubble = null; closeToolGroup();
          break;
        case 'assistant_text':
          appendAssistantText(c.text || '');
          break;
        case 'tool_use':
          currentAssistantBubble = null;
          appendToolUse(c.tool, c.input);
          break;
        case 'tool_result':
          appendToolResult(c.ok, c.content);
          break;
      }
    }
    currentAssistantBubble = null; closeToolGroup();
  }

  function setHeader(title, cwd) {
    currentSessionTitle = title || '';
    sessionTitle.textContent = title || 'Claude';
    cwdLabel.textContent = cwd ? cwd : '/';
  }

  function setMode(mode) {
    currentMode = mode || 'code';
    document.querySelectorAll('#workspace-toggle .seg-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.workspace === currentMode);
    });
    const ind = document.getElementById('workspace-indicator');
    if (ind) {
      const ic = (window.icon && window.icon(currentMode === 'chat' ? 'chat' : 'code', 13)) || '';
      ind.innerHTML = `<span class="wi-ic">${ic}</span><span>${currentMode === 'chat' ? 'Chat' : 'Code'}</span>`;
      ind.classList.toggle('chat', currentMode === 'chat');
      ind.classList.toggle('code', currentMode === 'code');
    }
    document.body.classList.toggle('mode-chat', currentMode === 'chat');
    document.body.classList.toggle('mode-code', currentMode === 'code');
    if (input) input.placeholder = '';
    // Adapt empty-state hint
    const hint = document.querySelector('#empty-state .hint');
    if (hint) {
      hint.innerHTML = currentMode === 'chat'
        ? '和 Claude 聊天<br><small>支持发图片让我看，纯对话不操作文件</small>'
        : '开始和 Claude 对话<br><small>添加图片，或选择电脑端文件</small>';
    }
  }

  function setModel(model) {
    currentModel = model || '';
    const labelEl = document.getElementById('model-label');
    if (!labelEl) return;
    const m = (META.models || []).find((x) => x.id === currentModel);
    labelEl.textContent = (m && m.label) || '默认';
  }

  // ---------- session list ----------
  async function loadSessionList() {
    let data;
    try {
      const r = await fetch(apiUrl('/api/sessions'));
      if (!r.ok) return;
      data = await r.json();
    } catch (_) { return; }
    sessionListEl.innerHTML = '';
    const filtered = data.sessions.filter((s) => (s.mode || 'code') === currentMode);
    if (filtered.length === 0) {
      const empty = document.createElement('div');
      empty.style.cssText = 'padding: 20px 12px; text-align: center; color: var(--text-3); font-size: 13px;';
      empty.textContent = currentMode === 'chat'
        ? '没有 Chat 会话，点 ＋ 新建一个'
        : '没有 Code 会话，点 ＋ 新建一个';
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
      const trashIcon = (window.icon && window.icon('trash', 16)) || '×';
      item.innerHTML = `
        <span class="si-badge"></span>
        <div class="si-main">
          <div class="si-title"></div>
          <div class="si-meta"></div>
        </div>
        <button class="si-del" type="button" title="删除"></button>
      `;
      item.querySelector('.si-badge').innerHTML = badgeIcon;
      item.querySelector('.si-del').innerHTML = trashIcon;
      item.querySelector('.si-title').textContent = t;
      item.querySelector('.si-meta').textContent = meta;
      item.addEventListener('click', () => {
        if (s.id !== currentSessionId) {
          send({ type: 'cmd', name: 'load_session', id: s.id });
        }
        closeDrawer();
      });
      item.querySelector('.si-del').addEventListener('click', (e) => {
        e.stopPropagation();
        if (!confirm(`删除「${t}」？此操作不可恢复。`)) return;
        send({ type: 'cmd', name: 'delete_session', id: s.id });
      });
      sessionListEl.appendChild(item);
    }
  }

  function openDrawer() {
    drawer.classList.remove('hidden');
    drawerMask.classList.remove('hidden');
    drawer.setAttribute('aria-hidden', 'false');
    loadSessionList();
  }
  function closeDrawer() {
    drawer.classList.add('hidden');
    drawerMask.classList.add('hidden');
    drawer.setAttribute('aria-hidden', 'true');
  }

  drawerBtn.addEventListener('click', openDrawer);
  drawerClose.addEventListener('click', closeDrawer);
  drawerMask.addEventListener('click', closeDrawer);
  newSessionBtn.addEventListener('click', () => {
    // create a new session in the current workspace mode
    send({ type: 'cmd', name: 'new_session', mode: currentMode });
    closeDrawer();
  });

  // ---------- event router ----------
  function handleEvent(msg) {
    switch (msg.type) {
      case 'hello': {
        currentSessionId = msg.session_id || null;
        if (msg.session) {
          setHeader(msg.session.title, msg.session.cwd);
          setMode(msg.session.mode);
          setModel(msg.session.model);
          renderHistory(msg.session.messages || []);
        } else {
          setHeader('Claude', msg.cwd || '');
          setMode('code');
          setModel('');
          clearMessages();
        }
        // Re-render any unanswered permission requests so a phone reconnecting
        // after tapping a push notification sees the card again.
        if (Array.isArray(msg.pending_perms)) {
          for (const p of msg.pending_perms) {
            if (p && p.id && !pendingPerms.has(p.id)) {
              appendPermissionCard(p.id, p.tool, p.input);
            }
          }
        }
        loadSessionList();
        break;
      }
      case 'session_loaded': {
        const s = msg.session || {};
        currentSessionId = s.id || null;
        setHeader(s.title, s.cwd);
        setMode(s.mode);
        setModel(s.model);
        renderHistory(s.messages || []);
        clearAttachments();
        clearFiles();
        setResponding(false);
        loadSessionList();
        break;
      }
      case 'session_deleted':
        loadSessionList();
        break;
      case 'session_renamed':
        if (msg.id === currentSessionId) setHeader(msg.title, cwdLabel.textContent);
        loadSessionList();
        break;
      case 'session_mode_changed':
        if (msg.id === currentSessionId) setMode(msg.mode);
        break;
      case 'session_model_changed':
        if (msg.id === currentSessionId) setModel(msg.model);
        break;
      case 'system':
        appendSystem(msg.msg || '');
        if (msg.msg && msg.msg.startsWith('turn cancelled')) setResponding(false);
        break;
      case 'error':
        appendError(msg.msg || 'error');
        currentAssistantBubble = null; closeToolGroup();
        setResponding(false);
        break;
      case 'user_echo':
        appendUser(msg.text || '', msg.images || [], msg.files || []);
        break;
      case 'assistant_text':
        appendAssistantText(msg.text || '');
        break;
      case 'tool_use':
        currentAssistantBubble = null;
        appendToolUse(msg.tool, msg.input);
        break;
      case 'tool_result':
        appendToolResult(msg.ok, msg.content);
        break;
      case 'permission_request':
        currentAssistantBubble = null; closeToolGroup();
        appendPermissionCard(msg.id, msg.tool, msg.input);
        break;
      case 'permission_resolved':
        markPermResolved(msg.id, msg.decision);
        break;
      case 'turn_done':
        currentAssistantBubble = null; closeToolGroup();
        setResponding(false);
        scrollToBottom(true);  // ensure the tail of the reply is fully visible
        loadSessionList();  // refresh title (auto-named) and updated_at
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

  function setResponding(flag) {
    isResponding = !!flag;
    sendBtn.classList.toggle('stopping', isResponding);
    if (window.icon) {
      sendBtn.innerHTML = window.icon(isResponding ? 'stop' : 'send', 18);
    } else {
      sendBtn.textContent = isResponding ? '■' : '↑';
    }
    sendBtn.setAttribute('aria-label', isResponding ? '停止' : '发送');
    sendBtn.title = isResponding ? '停止当前回复' : '发送';
    if (isResponding) showTyping(); else hideTyping();
  }

  function sendCurrent() {
    // If Claude is currently responding, the send button acts as STOP.
    if (isResponding) {
      send({ type: 'cmd', name: 'cancel' });
      return;
    }

    const text = input.value.trim();
    if (!text && pendingAttachments.length === 0 && pendingFiles.length === 0) return;

    if (text === '/new') {
      send({ type: 'cmd', name: 'new_session', mode: currentMode });
    } else if (text === '/cancel') {
      send({ type: 'cmd', name: 'cancel' });
    } else if (text.startsWith('/cwd ')) {
      const path = text.slice(5).trim();
      send({ type: 'cmd', name: 'cwd', path });
    } else {
      const images = pendingAttachments.map((a) => a.path);
      const files = pendingFiles.slice();
      const ok = send({ type: 'user_message', text, images, files });
      if (!ok) return;
      clearAttachments();
      clearFiles();
      setResponding(true);  // optimistic; cleared by turn_done / error
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

  // ---------- check-in FAB (Phase 2 Step 1: prompt-only minimum) ----------
  // Composes a minimal ```checkin``` fenced block and sends it as a normal
  // user_message. Server-side CHECKIN.md instructs Claude to parse + write
  // PocketBase. Later steps swap the prompt for a real geo/POI modal.
  function isoNowWithOffset() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const tz = -d.getTimezoneOffset();
    const sign = tz >= 0 ? '+' : '-';
    const tzh = pad(Math.floor(Math.abs(tz) / 60));
    const tzm = pad(Math.abs(tz) % 60);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T`
         + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
         + `${sign}${tzh}:${tzm}`;
  }

  function buildCheckinBlock(fields) {
    // fields: {name, build_location?, activity_type?, amount?, currency?, rate?,
    //         score?, note?, gps?: [lat,lng], accuracy_m?, osm_id?, amap_poi_id?,
    //         type?, city?, address?}
    const lines = ['```checkin'];
    lines.push(`when: ${isoNowWithOffset()}`);
    if (fields.gps) {
      lines.push(`gps: [${fields.gps[0]}, ${fields.gps[1]}]`);
      if (fields.accuracy_m != null) lines.push(`accuracy_m: ${fields.accuracy_m}`);
    }
    if (fields.name) {
      lines.push('selected_poi:');
      lines.push(`  name: ${fields.name}`);
      if (fields.osm_id)      lines.push(`  osm_id: ${fields.osm_id}`);
      if (fields.amap_poi_id) lines.push(`  amap_poi_id: ${fields.amap_poi_id}`);
      if (fields.type)        lines.push(`  type: ${fields.type}`);
      if (fields.city)        lines.push(`  city: ${fields.city}`);
      if (fields.address)     lines.push(`  address: ${fields.address}`);
    }
    lines.push(`build_location: ${fields.build_location === false ? 'false' : 'true'}`);
    if (fields.activity_type) lines.push(`activity_type: ${fields.activity_type}`);
    if (fields.amount != null) lines.push(`amount: ${fields.amount}`);
    if (fields.currency) lines.push(`currency: ${fields.currency}`);
    if (fields.rate != null) lines.push(`rate: ${fields.rate}`);
    if (fields.score != null) lines.push(`score: ${fields.score}`);
    if (fields.note) lines.push(`note: ${fields.note}`);
    lines.push('```');
    return lines.join('\n');
  }

  function sendCheckin(fields) {
    const block = buildCheckinBlock(fields);
    const ok = send({ type: 'user_message', text: block, images: [], files: [] });
    if (!ok) return false;
    setResponding(true);
    return true;
  }

  // Step 2: geolocation cache + async fetch.
  // Per CHECKIN.md the server uses `gps` to dedupe Locations within 100m.
  // We cache the last fix in localStorage so a second click within 30 min is
  // instant; in parallel we kick off a fresh high-accuracy request whose
  // result lands in cache for next time.
  const GPS_CACHE_KEY = 'bridge.lastGps';
  const GPS_CACHE_TTL_MS = 30 * 60 * 1000;

  function loadCachedGps() {
    try {
      const raw = localStorage.getItem(GPS_CACHE_KEY);
      if (!raw) return null;
      const o = JSON.parse(raw);
      if (!o || typeof o.lat !== 'number' || typeof o.lng !== 'number') return null;
      if (Date.now() - (o.t || 0) > GPS_CACHE_TTL_MS) return null;
      return o;
    } catch (_) { return null; }
  }

  function saveCachedGps(lat, lng, accuracy_m) {
    try {
      localStorage.setItem(GPS_CACHE_KEY, JSON.stringify({
        lat, lng, accuracy_m, t: Date.now(),
      }));
    } catch (_) { /* quota or disabled — silent */ }
  }

  function requestGps(timeoutMs = 8000) {
    return new Promise((resolve) => {
      if (!('geolocation' in navigator)) return resolve(null);
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          const lat = pos.coords.latitude;
          const lng = pos.coords.longitude;
          const acc = Math.round(pos.coords.accuracy || 0);
          saveCachedGps(lat, lng, acc);
          finish({ lat, lng, accuracy_m: acc });
        },
        () => finish(null),
        { enableHighAccuracy: true, timeout: timeoutMs, maximumAge: 60000 },
      );
    });
  }

  // Step 3: POI picker dialog backed by /api/poi/around.
  async function searchNearby(lat, lng, radius = 300) {
    try {
      const r = await fetch(apiUrl(`/api/poi/around?lat=${lat}&lng=${lng}&radius=${radius}`));
      if (!r.ok) return [];
      const data = await r.json();
      return Array.isArray(data.pois) ? data.pois : [];
    } catch (_) {
      return [];
    }
  }

  const checkinDialog = $('checkin-dialog');
  const cdStatus = $('cd-status');
  const cdList = $('cd-list');
  const cdManualName = $('cd-manual-name');
  const cdManualGo = $('cd-manual-go');

  // Dialog-close listener: clear contents so next open starts fresh.
  if (checkinDialog) {
    checkinDialog.addEventListener('close', () => {
      cdList.innerHTML = '';
      cdManualName.value = '';
      cdStatus.textContent = '正在定位…';
      cdStatus.className = 'cd-status';
    });
  }

  function renderPoiList(pois, gps) {
    cdList.innerHTML = '';
    if (!pois.length) {
      cdList.innerHTML = '<div class="cd-empty">附近没找到 POI<br><small>输入下方名字手动打卡</small></div>';
      return;
    }
    for (const p of pois) {
      const row = document.createElement('div');
      row.className = 'cd-row ' + (p.source || '');
      row.setAttribute('role', 'listitem');
      const pinIcon = (window.icon && window.icon('pin', 18)) || '📍';
      const meta = [p.type, p.city, p.address].filter(Boolean).join(' · ');
      row.innerHTML = `
        <span class="cd-icon">${pinIcon}</span>
        <div class="cd-main">
          <span class="cd-name"></span>
          <span class="cd-meta"><span class="cd-dist"></span><span class="cd-meta-info"></span></span>
        </div>
      `;
      row.querySelector('.cd-name').textContent = p.name;
      row.querySelector('.cd-dist').textContent = `${p.distance_m}m`;
      row.querySelector('.cd-meta-info').textContent = meta || (p.source === 'osm' ? 'OSM' : '');
      row.addEventListener('click', () => {
        const fields = {
          name: p.name,
          build_location: true,
        };
        if (gps) {
          fields.gps = [gps.lat, gps.lng];
          if (gps.accuracy_m != null) fields.accuracy_m = gps.accuracy_m;
        }
        if (p.osm_id) fields.osm_id = p.osm_id;
        if (p.amap_poi_id) fields.amap_poi_id = p.amap_poi_id;
        if (p.type) fields.type = p.type;
        if (p.city) fields.city = p.city;
        if (p.address) fields.address = p.address;
        sendCheckin(fields);
        checkinDialog.close();
      });
      cdList.appendChild(row);
    }
  }

  function sendManualCheckin(gps) {
    const raw = cdManualName.value.trim();
    if (!raw) {
      cdManualName.focus();
      return;
    }
    const fields = { name: raw, build_location: true };
    if (gps) {
      fields.gps = [gps.lat, gps.lng];
      if (gps.accuracy_m != null) fields.accuracy_m = gps.accuracy_m;
    }
    sendCheckin(fields);
    checkinDialog.close();
  }

  async function openCheckinDialog() {
    if (!checkinDialog || !checkinDialog.showModal) {
      // Fallback for ancient browsers — keep the Step 2 behaviour alive.
      const name = prompt('打卡店名 / 地点:');
      if (name && name.trim()) {
        const cached = loadCachedGps();
        const fields = { name: name.trim(), build_location: true };
        if (cached) { fields.gps = [cached.lat, cached.lng]; fields.accuracy_m = cached.accuracy_m; }
        sendCheckin(fields);
      }
      return;
    }

    // Reset + open.
    cdList.innerHTML = '<div class="cd-loading"><span class="cd-spinner"></span>正在定位…</div>';
    cdStatus.textContent = '正在定位…';
    cdStatus.className = 'cd-status';
    cdManualName.value = '';
    checkinDialog.showModal();

    // Wire manual-entry button to current GPS context.
    let currentGps = loadCachedGps();
    const onManualGo = () => sendManualCheckin(currentGps);
    cdManualGo.onclick = onManualGo;
    cdManualName.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); onManualGo(); }
    };

    // If we have a fresh-enough cached fix, show POIs immediately.
    if (currentGps) {
      cdStatus.textContent = `已有缓存定位 · 刷新中`;
      cdList.innerHTML = '<div class="cd-loading"><span class="cd-spinner"></span>查询附近 POI…</div>';
      const pois = await searchNearby(currentGps.lat, currentGps.lng);
      if (checkinDialog.open) renderPoiList(pois, currentGps);
    }

    // Get a fresh fix in the background; if it differs meaningfully, re-render.
    const fresh = await requestGps(10000);
    if (!checkinDialog.open) return; // user closed before GPS came

    if (!fresh) {
      if (!currentGps) {
        cdStatus.textContent = '无法获取位置 — 可手动输入';
        cdStatus.className = 'cd-status error';
        cdList.innerHTML = '<div class="cd-empty">没有定位信息<br><small>可在下方手动输入打卡名</small></div>';
      } else {
        cdStatus.textContent = `位置 (缓存) · acc ${currentGps.accuracy_m}m`;
        cdStatus.className = 'cd-status';
      }
      return;
    }

    currentGps = fresh;
    cdStatus.textContent = `位置 · acc ${fresh.accuracy_m}m`;
    cdStatus.className = 'cd-status ready';
    cdList.innerHTML = '<div class="cd-loading"><span class="cd-spinner"></span>查询附近 POI…</div>';
    const pois = await searchNearby(fresh.lat, fresh.lng);
    if (checkinDialog.open) renderPoiList(pois, fresh);
  }

  const checkinFab = $('checkin-fab');
  if (checkinFab) {
    checkinFab.addEventListener('click', openCheckinDialog);
  }

  // ---------- top menu ----------
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
      if (cmd === 'new') send({ type: 'cmd', name: 'new_session' });
      else if (cmd === 'cancel') send({ type: 'cmd', name: 'cancel' });
      else if (cmd === 'cwd-prompt') openCwdBrowser('cwd');
      else if (cmd === 'rename') {
        if (!currentSessionId) return;
        const t = prompt('会话标题:', currentSessionTitle);
        if (t === null) return;
        send({ type: 'cmd', name: 'rename_session', id: currentSessionId, title: t });
      }
      else if (cmd === 'usage') {
        openUsageModal();
      }
    });
  });

  // ---------- workspace toggle (Chat ↔ Code, opens new session of that type) ----------
  document.querySelectorAll('#workspace-toggle .seg-btn').forEach((b) => {
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      const newMode = b.dataset.workspace;
      if (newMode === currentMode) return;  // already on this workspace
      // optimistic UI: refresh session list filter immediately
      currentMode = newMode;
      setMode(newMode);
      loadSessionList();
      send({ type: 'cmd', name: 'switch_workspace', mode: newMode });
    });
  });

  // ---------- model picker ----------
  const modelBtn = document.getElementById('model-btn');
  const modelMenu = document.getElementById('model-menu');

  function renderModelMenu() {
    modelMenu.innerHTML = '';
    for (const m of META.models || []) {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'model-item' + (m.id === currentModel ? ' active' : '');
      item.innerHTML = `<span class="label"></span><span class="desc"></span>`;
      item.querySelector('.label').textContent = m.label;
      item.querySelector('.desc').textContent = m.desc || '';
      item.addEventListener('click', () => {
        modelMenu.classList.add('hidden');
        if (m.id === currentModel) return;
        setModel(m.id);
        send({ type: 'cmd', name: 'set_model', model: m.id });
      });
      modelMenu.appendChild(item);
    }
  }

  modelBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!modelMenu.classList.contains('hidden')) {
      modelMenu.classList.add('hidden');
      return;
    }
    renderModelMenu();
    // Drop menu below the model button (button now lives in the top app-bar).
    const r = modelBtn.getBoundingClientRect();
    modelMenu.style.left = Math.max(8, r.right - 200) + 'px';
    modelMenu.style.top = (r.bottom + 6) + 'px';
    modelMenu.style.bottom = '';
    modelMenu.classList.remove('hidden');
  });
  document.addEventListener('click', () => modelMenu.classList.add('hidden'));
  modelMenu.addEventListener('click', (e) => e.stopPropagation());

  // ---------- usage modal ----------
  function fmtMoney(v) {
    if (!v) return '$0.0000';
    if (v >= 1) return '$' + v.toFixed(2);
    return '$' + v.toFixed(4);
  }
  function fmtTokens(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
  }

  async function openUsageModal() {
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
      const r = await fetch(apiUrl('/api/usage'));
      data = await r.json();
    } catch (e) {
      body.innerHTML = `<div class="empty" style="padding:30px;text-align:center;color:var(--error)">加载失败: ${escapeHtml(e.message)}</div>`;
      return;
    }
    renderUsage(body, data);
  }

  function renderUsage(body, data) {
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

  function pct(v, max) {
    if (!max || max <= 0) return 0;
    return Math.max(0, Math.min(100, (v / max) * 100));
  }

  // ---------- meta load (modes/models) ----------
  async function loadMeta() {
    try {
      const r = await fetch(apiUrl('/api/meta'));
      META = await r.json();
      // re-render model button label now that we have labels
      setModel(currentModel);
    } catch (_) { /* ignore */ }
  }
  // loadMeta() now called from bootApp / enterSource after a source is selected.

  // ---------- attachments (image upload) ----------
  const MAX_ATTACH = 4;

  function renderAttachBar() {
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

  function clearAttachments() { pendingAttachments.length = 0; renderAttachBar(); }
  function clearFiles() { pendingFiles.length = 0; renderAttachBar(); }

  async function uploadFiles(files) {
    if (!currentSessionId) { sysMsg('当前无会话'); return; }
    const room = MAX_ATTACH - pendingAttachments.length;
    if (room <= 0) { sysMsg(`最多 ${MAX_ATTACH} 张图`); return; }
    const picked = Array.from(files).slice(0, room);
    const fd = new FormData();
    fd.append('session_id', currentSessionId);
    for (const f of picked) fd.append('files', f);
    try {
      const resp = await fetch(apiUrl('/api/upload'), { method: 'POST', body: fd });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        sysMsg('上传失败: ' + (err.detail || resp.status));
        return;
      }
      const data = await resp.json();
      for (const f of data.files || []) pendingAttachments.push(f);
      renderAttachBar();
    } catch (e) {
      sysMsg('上传出错: ' + e.message);
    }
  }

  const isTouch = window.matchMedia('(hover: none) and (pointer: coarse)').matches;

  // Track whether the most recent gesture on ➕ was an upward swipe.
  // Tap → directly invoke the system file picker (Android shows its native
  // "Choose an action" sheet, iOS shows photo/camera/files chooser).
  // Swipe-up → open our small popover with [📋 剪贴板, 📄 其他选项], so the
  // clipboard option is reachable even when system gestures interfere.
  let attachSwipeOpened = false;
  if (isTouch) {
    let ts = null;
    attachBtn.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) { ts = null; return; }
      const t = e.touches[0];
      ts = { x: t.clientX, y: t.clientY, time: Date.now(), swiped: false };
    }, { passive: true });
    attachBtn.addEventListener('touchmove', (e) => {
      if (!ts) return;
      const t = e.touches[0];
      if (ts.y - t.clientY > 18 && Math.abs(t.clientX - ts.x) < 40) ts.swiped = true;
    }, { passive: true });
    attachBtn.addEventListener('touchend', (e) => {
      if (!ts) return;
      const dt = Date.now() - ts.time;
      if (ts.swiped && dt < 800) {
        e.preventDefault();          // suppress the synthetic click
        attachSwipeOpened = true;
        attachMenu.classList.remove('hidden');
      }
      ts = null;
    });
  }

  if (pasteBtn) {
    pasteBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      pasteFromClipboard();
    });
  }

  attachBtn.addEventListener('click', (e) => {
    if (attachSwipeOpened) { attachSwipeOpened = false; return; }
    if (!isTouch) { fileInput.click(); return; }
    e.stopPropagation();
    // Tap on touch: open the photo gallery directly (image/* only, no
    // `multiple`). Single-image accept="image/*" inputs are more likely to
    // route to Chrome's Android Photo Picker / system Gallery than the
    // generic file manager.
    (galleryInput || albumInput).click();
  });

  if (attachMenu) {
    attachMenu.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-pick]');
      if (!btn) return;
      attachMenu.classList.add('hidden');
      const pick = btn.dataset.pick;
      if (pick === 'clipboard') pasteFromClipboard();
      else if (pick === 'other') fileInput.click();
      // legacy picks kept for backward-compat with cached HTML
      else if (pick === 'camera') cameraInput.click();
      else if (pick === 'album') albumInput.click();
      else if (pick === 'file') fileInput.click();
    });
    document.addEventListener('click', (e) => {
      if (attachMenu.classList.contains('hidden')) return;
      if (!attachMenu.contains(e.target) && e.target !== attachBtn) {
        attachMenu.classList.add('hidden');
      }
    });
  }

  // Read image(s) directly from the system clipboard. iOS Safari (≥13.4) and
  // Android Chrome both support navigator.clipboard.read(); on iOS the first
  // call also surfaces the native "Paste" permission sheet.
  async function pasteFromClipboard() {
    if (!navigator.clipboard || !navigator.clipboard.read) {
      sysMsg('当前浏览器不支持读取剪贴板，长按输入框粘贴试试');
      return;
    }
    let items;
    try {
      items = await navigator.clipboard.read();
    } catch (e) {
      // User dismissed the iOS paste sheet, or clipboard access denied.
      if (e && e.name !== 'NotAllowedError' && e.name !== 'AbortError') {
        sysMsg('读取剪贴板失败: ' + (e.message || e.name));
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
      sysMsg('剪贴板里没有图片');
      return;
    }
    await uploadFiles(files);
  }

  const handleUploadInput = async (e) => {
    if (!e.target.files || e.target.files.length === 0) return;
    await uploadFiles(e.target.files);
    e.target.value = '';
  };
  fileInput.addEventListener('change', handleUploadInput);
  if (cameraInput) cameraInput.addEventListener('change', handleUploadInput);
  if (albumInput) albumInput.addEventListener('change', handleUploadInput);
  if (galleryInput) galleryInput.addEventListener('change', handleUploadInput);

  if (cameraBtn) cameraBtn.style.display = 'none';

  // paste images — listen on document so screenshots paste even when the
  // textarea isn't focused; handle both clipboardData.items and .files
  // (different browsers populate one or the other for screenshots).
  function extractClipboardImages(cd) {
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

  function onPaste(e) {
    const tgt = e.target;
    const inEditable = tgt && (
      tgt === input ||
      tgt.isContentEditable ||
      (tgt.tagName === 'INPUT' && tgt.type === 'text')
    );
    const files = extractClipboardImages(e.clipboardData);
    if (!files.length) return;
    // Prevent the image from being pasted as a literal data URL into the textarea.
    e.preventDefault();
    uploadFiles(files);
    if (!inEditable) input.focus();
  }
  // Bind on document only — paste events bubble up from the textarea, so a
  // single listener covers both "focused in input" and "focused elsewhere".
  document.addEventListener('paste', onPaste);

  // ---------- file/dir picker ----------
  // mode: 'cwd' (pick directory to switch into) | 'file' (pick a file to attach)
  let browseState = { path: '', root: '', abs: '', mode: 'cwd' };

  function ensureCwdModal() {
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

  function openCwdBrowser(mode) {
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

  async function loadBrowse(path) {
    const list = document.getElementById('cwd-list');
    const crumb = document.getElementById('cwd-breadcrumb');
    list.innerHTML = '<div class="empty">加载中…</div>';
    let data;
    try {
      const resp = await fetch(apiUrl('/api/browse?path=' + encodeURIComponent(path)));
      if (!resp.ok) {
        list.innerHTML = `<div class="empty">加载失败 (${resp.status})</div>`;
        return;
      }
      data = await resp.json();
    } catch (e) {
      list.innerHTML = `<div class="empty">网络错误: ${escapeHtml(e.message)}</div>`;
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

  async function mkdirHere() {
    const name = prompt('新文件夹名:', '');
    if (!name) return;
    try {
      const resp = await fetch(apiUrl('/api/mkdir'), {
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

  filePickBtn.addEventListener('click', () => openCwdBrowser('file'));

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
      if (perm !== 'granted') { sysMsg('未授权通知'); return; }
      const keyResp = await fetch(apiUrl('/api/vapid-public-key')).then((r) => r.json());
      if (!keyResp.key) { sysMsg('服务端未配置 VAPID_PUBLIC_KEY'); return; }
      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(keyResp.key),
        });
      }
      await fetch(apiUrl('/api/subscribe'), {
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

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* ignore */ });
  }

  // ---------- visibility re-focus ----------
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && currentSource && (!ws || ws.readyState !== 1)) connect();
  });

  // ---------- source picker ----------
  let pickerPollTimer = null;
  let editingSourceId = null;

  function renderPicker() {
    const sources = loadSources();
    const list = $('sp-list');
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

  async function checkSourceStatus(src) {
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

  async function checkAllStatuses() {
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

  function showPicker() {
    $('source-picker').classList.remove('hidden');
    renderPicker();
    checkAllStatuses();
    if (pickerPollTimer) clearInterval(pickerPollTimer);
    pickerPollTimer = setInterval(checkAllStatuses, 8000);
  }
  function hidePicker() {
    $('source-picker').classList.add('hidden');
    if (pickerPollTimer) { clearInterval(pickerPollTimer); pickerPollTimer = null; }
  }

  function enterSource(id) {
    const src = findSource(id);
    if (!src) return;
    setCurrentSourceId(id);
    currentSource = src;
    $('source-name').textContent = src.name;
    setConn('connecting');
    // close any existing WS
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
    // reset chat-side state
    clearMessages();
    setHeader('Claude', '');
    setMode('code');
    setModel('');
    clearAttachments();
    clearFiles();
    setResponding(false);
    hidePicker();
    loadMeta();
    connect();
  }

  function exitToSourcePicker() {
    if (ws) { try { ws.close(); } catch (_) {} ws = null; }
    setCurrentSourceId('');
    currentSource = null;
    setConn('disconnected');
    showPicker();
  }

  // form (add / edit)
  function openSourceForm(idOrNull) {
    editingSourceId = idOrNull || null;
    const nameInput = $('sp-name');
    const urlInput = $('sp-url');
    const title = $('sp-form-title');
    if (editingSourceId) {
      const s = findSource(editingSourceId);
      title.textContent = '编辑电脑';
      nameInput.value = s ? s.name : '';
      urlInput.value = s ? s.url : '';
    } else {
      title.textContent = '添加电脑';
      nameInput.value = '';
      // Pre-fill with current page origin to make first-time setup easy.
      urlInput.value = location.origin || 'https://';
    }
    $('sp-form').classList.remove('hidden');
    setTimeout(() => nameInput.focus(), 50);
  }
  function closeSourceForm() {
    $('sp-form').classList.add('hidden');
    editingSourceId = null;
  }
  function saveSourceForm() {
    const name = ($('sp-name').value || '').trim();
    let url = ($('sp-url').value || '').trim();
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

  $('sp-add-btn').addEventListener('click', () => openSourceForm(null));
  $('sp-cancel').addEventListener('click', closeSourceForm);
  $('sp-save').addEventListener('click', saveSourceForm);
  // submit on Enter inside the form
  ['sp-name', 'sp-url'].forEach((id) => {
    $(id).addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); saveSourceForm(); }
    });
  });
  // top-bar source button → back to picker
  $('source-btn').addEventListener('click', exitToSourcePicker);

  // ---------- boot ----------
  function bootApp() {
    const id = getCurrentSourceId();
    const sources = loadSources();
    const found = id ? sources.find((s) => s.id === id) : null;
    if (found) {
      currentSource = found;
      $('source-name').textContent = found.name;
      hidePicker();
      loadMeta();
      connect();
    } else {
      showPicker();
      if (sources.length === 0) openSourceForm(null);
    }
  }

  bootApp();
})();
