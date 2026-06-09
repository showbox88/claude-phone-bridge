# Phase 4 · 前端模块化 + XSS 防护 + 流式渲染 + CSS 拆分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended for the volume) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 2873 行 `static/app.js` 拆成 25+ 个 ES Modules（无构建工具，原生浏览器模块）；把 2048 行 `static/style.css` 拆成 12+ 个文件 + 设计 token；vendored DOMPurify 防 XSS；流式渲染 CPU bug 修；iOS 14 `<dialog>` 兼容；删 46 行死代码。

**Architecture:**
- **双轨并存策略**：旧 `static/app.js` 不动直到所有新模块就位。Task 15 在 `index.html` 里 atomic swap：换 `<script>` 为 `<script type="module">` + 改 entry 到新 boot。Task 16 删除旧 `app.js`。任何中间失败可以 revert index.html 一行回到旧轨。
- **State 中心化**：`static/js/state.js` 提供最小 pub-sub store（get/set/subscribe）。原 14 处"模块作用域 let"集中到 store，模块间不互相 import 可变状态；都从 store 读。
- **WS 表驱动**：原 `handleEvent` 的 `if t === 'hello' ... else if t === 'assistant_text' ...` 长链改为 `HANDLERS[type] = fn`，未知 type 走 default。
- **流式渲染优化**：原代码每收到一个 chunk 就重新 markdown-parse 全 buffer（O(n²)）。改成 buffer 到段落/代码块边界才完整 markdown 渲染，期间用 `textContent` 增量追加显示。
- **DOMPurify vendored**：下载 v3.x UMD bundle 到 `static/vendor/purify.min.js`，所有 `innerHTML = markdown_html` 改 `innerHTML = DOMPurify.sanitize(markdown_html)`。共 65 处 innerHTML 赋值需审计。

**Tech Stack:** 原生 ES Modules (`<script type="module">` + import/export)、`window.marked` (已 vendored)、`window.DOMPurify` (本 Phase vendored)、原生 CSS custom properties。无 npm / Vite / 任何 build tool。

**Branch:** `refactor/phase-4-frontend-modules` (已创建，从 `742aa11`)
**Parent spec:** [2026-06-06-refactor-roadmap.md](../specs/2026-06-06-refactor-roadmap.md) §Phase 4
**Roadmap 风险标识：** 中（前端是用户直接看的；任何 regression 立即可见）

---

## File Structure (Target)

```
static/
  app.js                          # ≤ 3 行 entry（Task 15 替换）
  app.legacy.js                   # 旧 IIFE，Task 15 重命名，Task 16 删除
  index.html                      # Task 15 切 <script type="module">
  manifest.json                   # 不动
  icon.svg                        # 不动
  sw.js                           # Task 19 更新 cache 清单
  icons.js                        # 保留（utility 文件）
  marked.min.js                   # 保留（vendored markdown lib）
  vendor/
    purify.min.js                 # Task 0 vendored DOMPurify v3
  js/
    state.js                      # pub-sub store: get/set/subscribe
    dom.js                        # 所有 document.getElementById 缓存到导出常量
    api.js                        # apiUrl / apiGet/apiPost/apiPatch/apiDelete + 统一错误 + ApiError
    boot.js                       # 顶层入口（Task 14）
    util/
      timers.js                   # debounce/throttle/sleep
      format.js                   # fmtMoney/fmtTokens/fmtDue/scoreStars/currencySymbol/highlightMatch/pct
      yaml.js                     # parseCheckinYaml + buildCheckinBlock
      escape.js                   # escapeHtml
      dialog.js                   # openDialog (iOS 14 fallback)
    ws/
      socket.js                   # connect / send / reconnect / ping / setConn
      handlers.js                 # 表驱动 HANDLERS[type]
    render/
      markdown.js                 # renderMarkdownFinal + appendStreamChunk + finalizeStream + DOMPurify
      message.js                  # appendUser / appendAssistantText / appendSystem / appendError / hideEmptyState / clearMessages / renderHistory
      tool.js                     # appendToolUse / appendToolResult / ensureToolGroup / closeToolGroup / bumpToolGroupCount
      perm.js                     # appendPermissionCard / markPermResolved / respondPerm
      typing.js                   # showTyping / hideTyping / bumpTyping
      scroll.js                   # scrollToBottom
      checkin-card.js             # renderCheckinCard / fmtCheckinTime
    session/
      list.js                     # loadSessionList / applySearch
      header.js                   # setHeader / setMode / setModel / refreshModelPill / renderModelMenu / setAutoApprove
      drawer.js                   # openDrawer / closeDrawer / toggleDrawer / isDesktopDrawer
    composer/
      input.js                    # autoresize / setResponding / onPaste
      attachments.js              # renderAttachBar / clearAttachments / clearFiles / uploadFiles / pasteFromClipboard / extractClipboardImages / pendingAttachments / pendingFiles
      send.js                     # sendCurrent / sendCheckin / buildCheckinBlock / isoNowWithOffset
    features/
      sources.js                  # source-picker
      checkin.js                  # POI flow
      cwd-browser.js              # cwd modal
      usage.js                    # usage modal
      weekly-report.js            # weekly-report modal + toast
      sync-settings.js            # sync-settings modal
      bell.js                     # today-todos bell (setupPush DELETED)
  css/
    tokens.css                    # design tokens
    base.css                      # body / html reset
    layout.css                    # .app-bar / .main-pane / grid
    appbar.css                    # 顶栏
    drawer.css                    # 抽屉
    messages.css                  # .messages / .bubble
    tools-perms.css               # .tool-group / .perm-card
    composer.css                  # .composer / .input / .attach-bar
    picker.css                    # .source-picker
    utilities.css                 # .hidden / .muted / .icon-btn
    dialogs/
      checkin.css usage.css sync.css weekly.css cwd.css bell.css

tests/
  test_static_assets.py           # 新增 (Task 0)：验证 imports + asset 存在
  baseline/
    00-main-empty.png             # 截图基线 (Task 0)
    01-conversation.png
    02-drawer.png
    03-modal-usage.png
    04-modal-weekly.png
    05-modal-sync.png
    06-checkin-dialog.png
    07-cwd-browser.png
```

**Out of scope (留给 Phase 5/6):**
- 单元测试套件（前端无 npm/vitest，spec 未要求）— 仅静态资源完整性 + smoke
- TypeScript / JSDoc 类型标注（YAGNI）
- 真正的 iOS 14 dialog polyfill — Task 18 加 graceful CSS fallback 即可

---

## Pre-Flight Notes

### `index.html` 当前 script 加载
```html
<link rel="stylesheet" href="/static/style.css?v=46">
...
<script src="/static/icons.js?v=46"></script>
<script src="/static/marked.min.js"></script>
<script src="/static/app.js?v=46"></script>
```

Task 15 后变成：
```html
<link rel="stylesheet" href="/static/css/tokens.css?v=47">
... 16 CSS link tags ...
<script src="/static/icons.js?v=47"></script>
<script src="/static/marked.min.js"></script>
<script src="/static/vendor/purify.min.js?v=47"></script>
<script type="module" src="/static/app.js?v=47"></script>
```

### IIFE → modules 的状态搬迁规则

- **App-wide state** → `state.js` store keys（外部读 `get('currentMode')`，写 `set('currentMode', x)`）
- **DOM 缓存** → `dom.js` 命名导出（`export const input = $('input')`）
- **本地状态**（如某个 feature 闭包内的 buffer/cache）→ 留在所属模块的 module-scope `let`

### DOMPurify 选型
- 版本：v3.2.4（latest stable，纯 JS、无构建）
- 来源：cdn.jsdelivr.net 或 github releases 下载
- 引入方式：plain `<script>` 暴露 `window.DOMPurify`
- 调用：`DOMPurify.sanitize(html, {USE_PROFILES: {html: true}})`

### 验证策略（无前端单测框架）
- **Per task**：`node --check static/js/path.js` 仅语法检查；module imports 不验证
- **Per checkpoint (Tasks 7/13/15/17)**：deploy + smoke + 手工打开 PWA 验证主流程没崩
- **Final (Task 20)**：双 device 手测 + journal 干净 + 24h soak

---

## Task 0: 预备 - vendored DOMPurify + 资源清单 test + screenshot baseline

**Files:**
- Create: `static/vendor/purify.min.js`
- Create: `tests/test_static_assets.py`
- Create: `tests/baseline/00-main-empty.png` 等 8 张截图

- [ ] **Step 1: vendor DOMPurify v3**

```bash
cd "/d/Projects/Phone Bridge"
mkdir -p static/vendor
curl -sL "https://cdn.jsdelivr.net/npm/dompurify@3.2.4/dist/purify.min.js" -o static/vendor/purify.min.js
ls -la static/vendor/purify.min.js  # expect ~80KB
head -c 100 static/vendor/purify.min.js  # sanity: starts with `!function` or similar
```

如 curl 不行，从 https://github.com/cure53/DOMPurify/releases/tag/3.2.4 下载 `dist/purify.min.js`。

- [ ] **Step 2: 写 `tests/test_static_assets.py`**

```python
"""Verify the frontend module manifest stays consistent.

Phase 4 introduces 25+ ES modules under static/js/. This test catches:
- index.html references a file that doesn't exist
- An ES module imports another module that's missing from disk
- DOMPurify is referenced but the vendor file is missing
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
JS_ROOT = STATIC / "js"


def _index_html_text() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _list_js_modules() -> list[Path]:
    if not JS_ROOT.exists():
        return []
    return sorted(JS_ROOT.rglob("*.js"))


def test_index_html_references_exist():
    """Every src=/static/... and href=/static/... in index.html points to a real file."""
    html = _index_html_text()
    refs = set()
    for m in re.finditer(r'(?:src|href)="(/static/[^"?]+)', html):
        refs.add(m.group(1))
    missing = []
    for ref in refs:
        rel = ref.removeprefix("/static/")
        if not (STATIC / rel).exists():
            missing.append(ref)
    assert not missing, f"index.html references non-existent files: {missing}"


def test_no_imports_to_missing_modules():
    """Every `from './x.js'` or `from '../foo/y.js'` resolves to a real file."""
    missing = []
    for mod in _list_js_modules():
        text = mod.read_text(encoding="utf-8")
        for m in re.finditer(r'from\s+[\'"](\.{1,2}/[^\'"]+)[\'"]', text):
            rel = m.group(1)
            target = (mod.parent / rel).resolve()
            if not target.exists():
                missing.append((str(mod.relative_to(STATIC)), rel))
    assert not missing, f"Imports to missing modules: {missing}"


def test_purify_present_if_referenced():
    """If index.html references purify.min.js, it must be on disk."""
    html = _index_html_text()
    if "/static/vendor/purify.min.js" in html:
        assert (STATIC / "vendor" / "purify.min.js").exists(), (
            "index.html references purify.min.js but file is missing")
```

- [ ] **Step 3: Run test on VM**

```bash
scp tests/test_static_assets.py dashboard-server:/home/dev/phone-bridge/tests/
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_static_assets.py -v 2>&1 | tail -10'
```

Expected: 3/3 pass.

- [ ] **Step 4: Capture baseline screenshots (USER, 5 min)**

User opens PWA on phone/desktop. For each view, take a screenshot and save to `tests/baseline/`:
- `00-main-empty.png` — initial empty composer state
- `01-conversation.png` — sample conversation showing assistant_text + tool_use
- `02-drawer.png` — open session drawer
- `03-modal-usage.png` — usage modal
- `04-modal-weekly.png` — weekly report modal
- `05-modal-sync.png` — sync settings modal (scrolled to show targets)
- `06-checkin-dialog.png` — checkin POI list
- `07-cwd-browser.png` — cwd browser modal

These are the visual yardstick for Task 15/17 verification.

- [ ] **Step 5: Commit**

```bash
git add static/vendor/purify.min.js tests/test_static_assets.py tests/baseline/*.png
git commit -m "chore(phase4): vendor DOMPurify + static-asset test + baseline screenshots

Phase 4 Task 0 (preflight)."
```

---

## Task 1: `static/js/util/` 工具集

**Files:** Create `static/js/util/{escape,format,timers,yaml}.js`

低风险起步：纯函数，无外部状态。

- [ ] **Step 1: `static/js/util/escape.js`**

```javascript
// HTML escape for inserting untrusted strings into innerHTML when full
// markdown rendering isn't appropriate (e.g. session titles).
export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
```

- [ ] **Step 2: `static/js/util/format.js`**

Read legacy `static/app.js` and copy these function bodies (search for each name):
- `fmtMoney` (~line 1594)
- `fmtTokens` (~line 1599)
- `fmtDue` (~line 2601)
- `scoreStars` (~line 448)
- `currencySymbol` (~line 443)
- `pct` (~line 1695)
- `highlightMatch` (~line 766)

Paste each into format.js with the body verbatim, prepended by `export`:

```javascript
export function fmtMoney(v) {
  // body from legacy app.js
}
export function fmtTokens(n) { /* body */ }
export function fmtDue(s) { /* body */ }
export function scoreStars(score) { /* body */ }
export function currencySymbol(cur) { /* body */ }
export function pct(v, max) { /* body */ }
export function highlightMatch(text, q) { /* body */ }
```

- [ ] **Step 3: `static/js/util/timers.js`**

```javascript
// Timer helpers. Pulled from inline patterns in legacy app.js.

export function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

export function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function throttle(fn, ms) {
  let last = 0;
  let pending = null;
  return (...args) => {
    const now = Date.now();
    if (now - last >= ms) {
      last = now;
      fn(...args);
    } else {
      clearTimeout(pending);
      pending = setTimeout(() => {
        last = Date.now();
        fn(...args);
      }, ms - (now - last));
    }
  };
}
```

- [ ] **Step 4: `static/js/util/yaml.js`**

Read legacy `parseCheckinYaml` (~line 398) and copy:

```javascript
// Tiny YAML subset parser for ```checkin``` fenced blocks in chat mode.
// Supports: top-level key:value, indented sub-keys, quoted strings,
// inline arrays [a, b, c]. NOT a full YAML parser.

export function parseCheckinYaml(text) {
  // copy body from legacy app.js parseCheckinYaml
}
```

- [ ] **Step 5: Syntax + test**

```bash
for f in static/js/util/*.js; do node --check "$f"; done
scp -r static/js/util dashboard-server:/home/dev/phone-bridge/static/js/util
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_static_assets.py -v 2>&1 | tail -5'
```

Expected: all parse OK; 3/3 tests pass.

- [ ] **Step 6: Commit**

```bash
git add static/js/util/
git commit -m "refactor(frontend): extract util/{escape,format,timers,yaml}

Phase 4 Task 1. First 4 modules in the ES Modules split. All pure
functions, no external dependencies — safe starting point.

Legacy app.js untouched; loaded entry unchanged until Task 15."
```

---

## Task 2: `static/js/state.js` pub-sub store

**Files:** Create `static/js/state.js`

- [ ] **Step 1: Write `static/js/state.js`**

```javascript
/**
 * Minimal pub-sub state store.
 *
 * Replaces the ~14 module-scoped `let`s in the legacy IIFE app.js for
 * app-wide state (currentSessionId, currentMode, autoApprove, ws, etc.).
 *
 * Usage:
 *   import { get, set, subscribe } from './state.js';
 *   const sid = get('currentSessionId');
 *   set('currentSessionId', newSid);          // notifies subscribers
 *   const unsub = subscribe('currentMode', (m) => updateUI(m));
 */

const _state = {
  // session
  currentSessionId: null,
  currentSessionTitle: '',
  currentMode: 'code',
  currentModel: '',
  autoApprove: false,
  META: { modes: [], models: [] },
  isResponding: false,

  // ws
  ws: null,
  reconnectDelay: 500,
  pingTimer: null,
  reconnectTimer: null,

  // render
  currentAssistantBubble: null,
  currentToolGroup: null,
  typingEl: null,
  currentAssistantBuffer: '',

  // source picker
  currentSource: null,
};

const _subs = new Map();  // key -> Set<callback>

export function get(key) { return _state[key]; }

export function set(key, value) {
  if (_state[key] === value) return;
  _state[key] = value;
  const subs = _subs.get(key);
  if (subs) {
    for (const cb of subs) {
      try { cb(value); }
      catch (e) { console.error(`state subscriber for ${key} threw:`, e); }
    }
  }
}

export function subscribe(key, cb) {
  if (!_subs.has(key)) _subs.set(key, new Set());
  _subs.get(key).add(cb);
  return () => _subs.get(key).delete(cb);
}

// For tests / debugging only — do not use in app code.
export function _all() { return { ..._state }; }
```

- [ ] **Step 2: Syntax + commit**

```bash
node --check static/js/state.js
git add static/js/state.js
git commit -m "refactor(frontend): add state.js pub-sub store

Phase 4 Task 2."
```

---

## Task 3: `static/js/dom.js` DOM element cache

**Files:** Create `static/js/dom.js`

- [ ] **Step 1: Cross-check legacy `app.js:24-52` for the canonical list of element IDs**

```bash
sed -n '24,52p' static/app.js
```

- [ ] **Step 2: Write `static/js/dom.js`** — one named export per `getElementById` call:

```javascript
/**
 * DOM element cache. One named export per document.getElementById.
 * Modules import only what they need. Modules MUST NOT call
 * getElementById themselves — go through this file.
 */
const $ = (id) => document.getElementById(id);

// Conversation surface
export const messagesScroll = $('messages');
export const messages = messagesScroll?.querySelector('.messages-inner') || messagesScroll;
export const emptyState = $('empty-state');

// Composer
export const input = $('input');
export const sendBtn = $('send-btn');
export const attachBtn = $('attach-btn');
export const pasteBtn = $('paste-btn');
export const cameraBtn = $('camera-btn');
export const attachMenu = $('attach-menu');
export const albumInput = $('album-input');
export const galleryInput = $('gallery-input');
export const filePickBtn = $('file-pick-btn');
export const fileInput = $('file-input');
export const cameraInput = $('camera-input');
export const attachBar = $('attach-bar');

// App bar
export const connDot = $('conn-dot');
export const cwdLabel = $('cwd-label');
export const sessionTitle = $('session-title');
export const menu = $('menu');
export const menuBtn = $('menu-btn');
export const notifBtn = $('notif-btn');

// Drawer
export const drawer = $('drawer');
export const drawerMask = $('drawer-mask');
export const drawerBtn = $('drawer-btn');
export const drawerClose = $('drawer-close');
export const newSessionBtn = $('new-session-btn');
export const sessionListEl = $('session-list');
export const sessionSearch = $('session-search');
export const sessionSearchClear = $('session-search-clear');

// Source picker
export const sourcePicker = $('source-picker');
```

Add any additional IDs referenced by features (search legacy app.js for `getElementById` calls beyond the top capture block; e.g. checkin dialog elements).

- [ ] **Step 3: Syntax + commit**

```bash
node --check static/js/dom.js
git add static/js/dom.js
git commit -m "refactor(frontend): add dom.js with named exports for all element IDs

Phase 4 Task 3."
```

---

## Task 4: `static/js/api.js` fetch wrapper

**Files:** Create `static/js/api.js`

- [ ] **Step 1: Write `static/js/api.js`**

```javascript
/**
 * Unified fetch wrapper. Replaces 24 raw fetch() calls in legacy app.js.
 *
 * - apiGet/apiPost/apiPatch/apiDelete: JSON-friendly wrappers
 * - apiPostForm: multipart upload
 * - All throw ApiError on non-2xx
 */
import { get } from './state.js';

export class ApiError extends Error {
  constructor(status, body, msg) {
    super(msg || `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

export function apiUrl(path) {
  const src = get('currentSource');
  const base = (src && src.url) ? src.url.replace(/\/$/, '') : '';
  return base + path;
}

export function wsUrl() {
  const src = get('currentSource');
  if (!src || !src.url) return '';
  const u = new URL(src.url);
  const proto = u.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${u.host}/ws`;
}

export function assetUrl(path) { return apiUrl(path); }

async function _do(method, path, body) {
  const init = { method, credentials: 'include' };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const r = await fetch(apiUrl(path), init);
  const text = await r.text();
  let parsed = null;
  if (text) {
    try { parsed = JSON.parse(text); }
    catch { parsed = { _raw: text.slice(0, 500) }; }
  }
  if (!r.ok) {
    throw new ApiError(r.status, parsed, `${method} ${path}: ${r.status}`);
  }
  return parsed;
}

export const apiGet    = (path)        => _do('GET', path);
export const apiPost   = (path, body)  => _do('POST', path, body ?? {});
export const apiPatch  = (path, body)  => _do('PATCH', path, body ?? {});
export const apiPut    = (path, body)  => _do('PUT', path, body ?? {});
export const apiDelete = (path)        => _do('DELETE', path);

// Special case: multipart upload (/api/upload). Browser writes boundary.
export async function apiPostForm(path, formData) {
  const r = await fetch(apiUrl(path), {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });
  const text = await r.text();
  let parsed = null;
  if (text) {
    try { parsed = JSON.parse(text); }
    catch { parsed = { _raw: text.slice(0, 500) }; }
  }
  if (!r.ok) throw new ApiError(r.status, parsed, `POST ${path}: ${r.status}`);
  return parsed;
}
```

- [ ] **Step 2: Syntax + commit**

```bash
node --check static/js/api.js
git add static/js/api.js
git commit -m "refactor(frontend): add api.js fetch wrapper

Phase 4 Task 4."
```

---

## Task 5: `static/js/render/markdown.js` + DOMPurify + 流式优化

**Files:** Create `static/js/render/markdown.js`

Phase 4 最重要的安全 + 性能改动之一。

- [ ] **Step 1: Write `static/js/render/markdown.js`**

```javascript
/**
 * Markdown rendering pipeline.
 *
 * Pipeline: raw text → marked.parse() → DOMPurify.sanitize() → safe HTML
 *
 * Streaming optimization:
 * Legacy code re-parsed the ENTIRE buffer on every assistant_text chunk —
 * O(n²). For a 5000-char response this pegged phone CPU.
 *
 * Here:
 *   renderMarkdownFinal(text)  — full parse + sanitize (turn end)
 *   appendStreamChunk(container, oldBuffer, newChunk) — incremental:
 *     fast-path appends plain textContent during stream; only re-parses
 *     full buffer when a paragraph break or fenced code block closes.
 *   finalizeStream(container, finalBuffer) — last clean render
 */

const SANITIZE_OPTS = { USE_PROFILES: { html: true } };
const BOUNDARY_RE = /\n\n|```[a-z0-9]*\n[\s\S]*?\n```/m;

function _sanitize(html) {
  if (typeof window !== 'undefined' && window.DOMPurify) {
    return window.DOMPurify.sanitize(html, SANITIZE_OPTS);
  }
  console.warn('DOMPurify missing; rendering unsanitized markdown');
  return html;
}

export function renderMarkdownFinal(text) {
  if (!text) return '';
  const raw = window.marked.parse(text);
  return _sanitize(raw);
}

export function appendStreamChunk(container, oldBuffer, newChunk) {
  if (!newChunk) return oldBuffer;
  const buffer = oldBuffer + newChunk;
  if (BOUNDARY_RE.test(buffer)) {
    container.innerHTML = renderMarkdownFinal(buffer);
    return buffer;
  }
  let tail = container.querySelector(':scope > .stream-tail');
  if (!tail) {
    tail = document.createElement('span');
    tail.className = 'stream-tail';
    container.appendChild(tail);
  }
  tail.textContent += newChunk;
  return buffer;
}

export function finalizeStream(container, finalBuffer) {
  container.innerHTML = renderMarkdownFinal(finalBuffer);
}
```

- [ ] **Step 2: Syntax + commit**

```bash
node --check static/js/render/markdown.js
git add static/js/render/markdown.js
git commit -m "feat(frontend): markdown rendering with DOMPurify + streaming opt

Phase 4 Task 5. renderMarkdownFinal/appendStreamChunk/finalizeStream
exported. DOMPurify sanitization. Fixes streaming CPU O(n²) bug."
```

---

## Task 6: `static/js/render/` other rendering modules

**Files:** Create scroll.js, typing.js, message.js, tool.js, perm.js, checkin-card.js

Each module copies the corresponding function bodies from legacy app.js, prepends `export`, and rewrites:
- `state.X` → `get('X')` / `set('X', y)` (from state.js)
- `getElementById(X)` → import from dom.js
- `fetch(...)` → `apiGet/apiPost(...)` from api.js
- markdown rendering → uses functions from `./markdown.js`

- [ ] **Step 1: `static/js/render/scroll.js`** — copy legacy `scrollToBottom` (~line 659)

```javascript
import { messagesScroll } from '../dom.js';

export function scrollToBottom(force) {
  // copy body from legacy
}
```

- [ ] **Step 2: `static/js/render/typing.js`** — copy `showTyping/hideTyping/bumpTyping` (~lines 182-201)

```javascript
import { messages } from '../dom.js';
import { get, set } from '../state.js';

export function showTyping() { /* copy body */ }
export function hideTyping() { /* copy body */ }
export function bumpTyping() { /* copy body */ }
```

References to `typingEl` become `get('typingEl')` / `set('typingEl', x)`.

- [ ] **Step 3: `static/js/render/message.js`** — copy:
  - `hideEmptyState` (~line 305)
  - `appendUser` (~line 309)
  - `appendAssistantText` (~line 516) — uses new streaming API
  - `appendSystem` (~line 575)
  - `appendError` (~line 586)
  - `clearMessages` (~line 678)
  - `renderHistory` (~line 686)

```javascript
import { messages, emptyState } from '../dom.js';
import { get, set } from '../state.js';
import { escapeHtml } from '../util/escape.js';
import { renderMarkdownFinal, appendStreamChunk, finalizeStream } from './markdown.js';
import { scrollToBottom } from './scroll.js';
import { renderCheckinCard } from './checkin-card.js';
import { parseCheckinYaml } from '../util/yaml.js';

export function hideEmptyState() { /* copy */ }
export function appendUser(text, images, files) { /* copy + use renderMarkdownFinal */ }

export function appendAssistantText(text) {
  hideEmptyState();
  let bubble = get('currentAssistantBubble');
  let buffer = get('currentAssistantBuffer');
  if (!bubble) {
    bubble = document.createElement('div');
    bubble.className = 'bubble assistant';
    messages.appendChild(bubble);
    set('currentAssistantBubble', bubble);
    buffer = '';
  }
  buffer = appendStreamChunk(bubble, buffer, text);
  set('currentAssistantBuffer', buffer);
  scrollToBottom();
}

export function appendSystem(text) { /* copy */ }
export function appendError(text) { /* copy */ }
export function clearMessages() { /* copy */ }
export function renderHistory(msgs) { /* copy */ }
```

`finalizeStream` is called from `ws/handlers.js` on `turn_done`, not from inside message.js.

- [ ] **Step 4: `static/js/render/tool.js`** — copy `appendToolUse/appendToolResult/ensureToolGroup/closeToolGroup/bumpToolGroupCount` (~lines 203-228, 531-573)

```javascript
import { messages } from '../dom.js';
import { get, set } from '../state.js';
import { escapeHtml } from '../util/escape.js';
import { renderMarkdownFinal } from './markdown.js';

export function ensureToolGroup() { /* copy */ }
export function bumpToolGroupCount() { /* copy */ }
export function closeToolGroup() { set('currentToolGroup', null); }
export function appendToolUse(tool, inp) { /* copy */ }
export function appendToolResult(ok, content) { /* copy + sanitize inputs */ }
```

- [ ] **Step 5: `static/js/render/perm.js`** — copy `appendPermissionCard/markPermResolved/respondPerm` (~lines 596-657)

```javascript
import { messages } from '../dom.js';
import { escapeHtml } from '../util/escape.js';
import { sendWs } from '../ws/socket.js';

const pendingPerms = new Map();  // module-local

export function appendPermissionCard(id, tool, inp) { /* copy */ }
export function markPermResolved(id, decision) { /* copy */ }
export function respondPerm(id, decision) {
  sendWs({ type: 'permission_response', id, decision });
  markPermResolved(id, decision);
}
```

- [ ] **Step 6: `static/js/render/checkin-card.js`** — copy `renderCheckinCard/fmtCheckinTime` (~lines 431-514)

```javascript
import { escapeHtml } from '../util/escape.js';
import { fmtMoney, currencySymbol, scoreStars } from '../util/format.js';

export function fmtCheckinTime(when) { /* copy */ }
export function renderCheckinCard(data, rawYaml) { /* copy */ }
```

- [ ] **Step 7: Syntax + commit**

```bash
for f in static/js/render/*.js; do node --check "$f"; done
scp -r static/js/render dashboard-server:/home/dev/phone-bridge/static/js/
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_static_assets.py -v 2>&1 | tail -5'
git add static/js/render/
git commit -m "refactor(frontend): extract render/ modules (6 files)

Phase 4 Task 6."
```

---

## Task 7: `static/js/ws/` socket + handlers

**Files:** Create `socket.js` + `handlers.js`

- [ ] **Step 1: `static/js/ws/socket.js`**

```javascript
import { connDot } from '../dom.js';
import { get, set } from '../state.js';
import { dispatch } from './handlers.js';

function _wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

export function setConn(state) {
  if (!connDot) return;
  connDot.className = `dot ${state}`;
  connDot.title = state;
}

function _clearPing() {
  const t = get('pingTimer');
  if (t) { clearInterval(t); set('pingTimer', null); }
}

function _startPing(sock) {
  _clearPing();
  set('pingTimer', setInterval(() => {
    if (sock.readyState === WebSocket.OPEN) {
      sock.send(JSON.stringify({ type: 'ping' }));
    }
  }, 30000));
}

export function connect() {
  const rt = get('reconnectTimer');
  if (rt) { clearTimeout(rt); set('reconnectTimer', null); }

  setConn('connecting');
  const sock = new WebSocket(_wsUrl());
  set('ws', sock);

  sock.addEventListener('open', () => {
    setConn('connected');
    set('reconnectDelay', 500);
    _startPing(sock);
  });

  sock.addEventListener('message', (ev) => {
    let msg = null;
    try { msg = JSON.parse(ev.data); }
    catch { return; }
    dispatch(msg);
  });

  sock.addEventListener('close', () => {
    setConn('disconnected');
    _clearPing();
    const delay = Math.min(get('reconnectDelay') || 500, 30000);
    set('reconnectDelay', delay * 2);
    set('reconnectTimer', setTimeout(connect, delay));
  });

  sock.addEventListener('error', () => setConn('error'));
}

export function sendWs(obj) {
  const sock = get('ws');
  if (!sock || sock.readyState !== WebSocket.OPEN) return false;
  sock.send(JSON.stringify(obj));
  return true;
}
```

- [ ] **Step 2: `static/js/ws/handlers.js`** — table-driven dispatch

```javascript
import { get, set } from '../state.js';
import {
  appendAssistantText, appendSystem, appendError, hideEmptyState,
  renderHistory, clearMessages,
} from '../render/message.js';
import { finalizeStream } from '../render/markdown.js';
import { appendToolUse, appendToolResult, closeToolGroup } from '../render/tool.js';
import { appendPermissionCard, markPermResolved } from '../render/perm.js';
import { hideTyping } from '../render/typing.js';
import { scrollToBottom } from '../render/scroll.js';
import { setHeader, setMode, setModel, setAutoApprove } from '../session/header.js';

function endStream() {
  const bubble = get('currentAssistantBubble');
  const buf = get('currentAssistantBuffer');
  if (bubble && buf) finalizeStream(bubble, buf);
  set('currentAssistantBubble', null);
  set('currentAssistantBuffer', '');
  closeToolGroup();
  hideTyping();
}

const HANDLERS = {
  hello: (m) => {
    if (m.cwd !== undefined) setHeader(null, m.cwd);
    if (m.auto_approve !== undefined) setAutoApprove(!!m.auto_approve);
    if (m.session) {
      set('currentSessionId', m.session.id);
      set('currentSessionTitle', m.session.title || '');
      setHeader(m.session.title || '', m.session.cwd || '');
      if (m.session.mode) setMode(m.session.mode);
      if (m.session.model !== undefined) setModel(m.session.model);
      if (Array.isArray(m.session.messages)) {
        clearMessages();
        renderHistory(m.session.messages);
      }
    }
    if (Array.isArray(m.pending_perms)) {
      for (const p of m.pending_perms) appendPermissionCard(p.id, p.tool, p.input);
    }
  },
  user_echo: () => {},  // PWA renders user message optimistically on send
  assistant_text: (m) => { hideTyping(); appendAssistantText(m.text || ''); },
  tool_use: (m) => { endStream(); appendToolUse(m.tool, m.input); },
  tool_result: (m) => appendToolResult(m.ok, m.content),
  permission_request: (m) => {
    appendPermissionCard(m.id, m.tool, m.input);
    scrollToBottom();
  },
  permission_resolved: (m) => markPermResolved(m.id, m.decision),
  turn_done: () => { endStream(); scrollToBottom(true); },
  system: (m) => appendSystem(m.msg || ''),
  error: (m) => appendError(m.msg || 'error'),
  pong: () => {},
  session_loaded: (m) => {
    if (m.session) {
      set('currentSessionId', m.session.id);
      setHeader(m.session.title || '', m.session.cwd || '');
      if (m.session.mode) setMode(m.session.mode);
      if (m.session.model !== undefined) setModel(m.session.model);
      clearMessages();
      if (Array.isArray(m.session.messages)) renderHistory(m.session.messages);
    }
  },
  session_deleted: () => {},
  session_renamed: (m) => {
    if (m.id === get('currentSessionId')) {
      set('currentSessionTitle', m.title || '');
      setHeader(m.title || '', null);
    }
  },
  session_model_changed: (m) => {
    if (m.id === get('currentSessionId')) setModel(m.model || '');
  },
  auto_approve_changed: (m) => setAutoApprove(!!m.value),
  sessions_changed: () => {},
};

export function dispatch(msg) {
  const t = msg && msg.type;
  const fn = HANDLERS[t];
  if (fn) {
    try { fn(msg); }
    catch (e) { console.error('WS handler', t, 'threw:', e); }
  } else if (t) {
    console.warn('WS unknown frame type:', t, msg);
  }
}
```

- [ ] **Step 3: Syntax + commit**

```bash
node --check static/js/ws/socket.js
node --check static/js/ws/handlers.js
git add static/js/ws/
git commit -m "refactor(frontend): extract ws/{socket,handlers} table-driven dispatch

Phase 4 Task 7. endStream() helper consolidates 5 legacy spots."
```

---

## Task 8: `static/js/session/` modules

**Files:** Create `header.js`, `list.js`, `drawer.js`

- [ ] **Step 1: `static/js/session/header.js`** — copy `setHeader/setMode/setModel/refreshModelPill/renderModelMenu/setAutoApprove` (~lines 711-756, 1535-1593) with `state.X` → `get/set`, DOM → imports from dom.js.

- [ ] **Step 2: `static/js/session/list.js`** — copy `loadSessionList/applySearch` (~lines 783-897). Re-import `highlightMatch` from `../util/format.js`.

- [ ] **Step 3: `static/js/session/drawer.js`** — copy `openDrawer/closeDrawer/toggleDrawer/isDesktopDrawer` (~lines 899-944).

- [ ] **Step 4: Syntax + commit**

```bash
for f in static/js/session/*.js; do node --check "$f"; done
git add static/js/session/
git commit -m "refactor(frontend): extract session/{header,list,drawer}

Phase 4 Task 8."
```

---

## Task 9: `static/js/composer/` modules

**Files:** Create `input.js`, `attachments.js`, `send.js`

- [ ] **Step 1: `static/js/composer/input.js`** — copy `autoresize/setResponding/onPaste` (~lines 1051-1067, 2363-2384).

- [ ] **Step 2: `static/js/composer/attachments.js`** — copy `renderAttachBar/clearAttachments/clearFiles/uploadFiles/pasteFromClipboard/extractClipboardImages` + the `pendingAttachments/pendingFiles` arrays (~lines 2185-2362). Use `apiPostForm` from api.js.

- [ ] **Step 3: `static/js/composer/send.js`** — copy `isoNowWithOffset/buildCheckinBlock/sendCheckin/sendCurrent` (~lines 1069-1169).

```javascript
import { input } from '../dom.js';
import { get, set } from '../state.js';
import { sendWs } from '../ws/socket.js';
import {
  pendingAttachments, pendingFiles,
  clearAttachments, clearFiles,
} from './attachments.js';
import { setResponding } from './input.js';
import { appendUser } from '../render/message.js';

export function isoNowWithOffset() { /* copy */ }
export function buildCheckinBlock(fields) { /* copy */ }
export function sendCheckin(fields) { /* copy */ }
export function sendCurrent() { /* copy */ }
```

- [ ] **Step 4: Syntax + commit**

```bash
for f in static/js/composer/*.js; do node --check "$f"; done
git add static/js/composer/
git commit -m "refactor(frontend): extract composer/{input,attachments,send}

Phase 4 Task 9."
```

---

## Task 10: `static/js/features/sources.js`

**Files:** Create `static/js/features/sources.js`

Copy: `loadSources/saveSources/getCurrentSourceId/setCurrentSourceId/findSource/renderPicker/checkSourceStatus/checkAllStatuses/showPicker/hidePicker/enterSource/exitToSourcePicker/openSourceForm/closeSourceForm/saveSourceForm` (~lines 63-82, 2666-2855).

```javascript
import { sourcePicker } from '../dom.js';
import { get, set } from '../state.js';

const SOURCES_KEY = 'bridge.sources';
const CURRENT_KEY = 'bridge.current_source_id';

export function loadSources() { /* copy */ }
export function saveSources(arr) { /* copy */ }
export function getCurrentSourceId() { /* copy */ }
export function setCurrentSourceId(id) { /* copy */ }
export function findSource(id) { /* copy */ }
export function renderPicker() { /* copy */ }
export async function checkSourceStatus(src) { /* copy */ }
export async function checkAllStatuses() { /* copy */ }
export function showPicker() { /* copy */ }
export function hidePicker() { /* copy */ }
export function enterSource(id) { /* copy */ }
export function exitToSourcePicker() { /* copy */ }
export function openSourceForm(idOrNull) { /* copy */ }
export function closeSourceForm() { /* copy */ }
export function saveSourceForm() { /* copy */ }
```

- [ ] **Step 1: Write + syntax + commit**

```bash
node --check static/js/features/sources.js
git add static/js/features/sources.js
git commit -m "refactor(frontend): extract features/sources.js

Phase 4 Task 10."
```

---

## Task 11: `static/js/features/checkin.js` (POI flow)

**Files:** Create `static/js/features/checkin.js`

Copy: `loadCachedGps/saveCachedGps/requestGps/searchNearby/resetCheckinDialog/showStage/enterFormStage/renderPoiList/stageManualEntry/openCheckinDialog` (~lines 1170-1534).

```javascript
import { apiGet } from '../api.js';
import { sendCheckin } from '../composer/send.js';
import { openDialog } from '../util/dialog.js';  // Task 18 dependency

const GPS_CACHE_KEY = 'bridge.last_gps';

export function loadCachedGps() { /* copy */ }
export function saveCachedGps(lat, lng, accuracy_m) { /* copy */ }
export function requestGps(timeoutMs = 8000) { /* copy */ }
export async function searchNearby(lat, lng, radius = 300) { /* copy */ }
export function resetCheckinDialog() { /* copy */ }
export function showStage(name) { /* copy */ }
export function enterFormStage(selection) { /* copy */ }
export function renderPoiList(pois, gps) { /* copy */ }
export function stageManualEntry(gps) { /* copy */ }
export async function openCheckinDialog() { /* copy */ }
```

Note: `openDialog` (util/dialog.js) is created in Task 18. For Task 11, you can use native `.showModal()` directly; Task 18 will replace those calls. Or just write Task 18 first and import here.

- [ ] **Step 1: Write + syntax + commit**

```bash
node --check static/js/features/checkin.js
git add static/js/features/checkin.js
git commit -m "refactor(frontend): extract features/checkin.js (POI flow)

Phase 4 Task 11. Largest single feature module."
```

---

## Task 12: `static/js/features/cwd-browser.js`

**Files:** Create `static/js/features/cwd-browser.js`

Copy: `ensureCwdModal/openCwdBrowser/loadBrowse/mkdirHere/pickHere` (~lines 2385-2519).

```javascript
import { apiGet, apiPost } from '../api.js';
import { sendWs } from '../ws/socket.js';

let _cwdModal = null;

export function ensureCwdModal() { /* copy */ }
export function openCwdBrowser(mode) { /* copy */ }
export async function loadBrowse(path) { /* copy */ }
export async function mkdirHere() { /* copy */ }
export function pickHere() { /* copy */ }
```

- [ ] **Step 1: Write + syntax + commit**

```bash
node --check static/js/features/cwd-browser.js
git add static/js/features/cwd-browser.js
git commit -m "refactor(frontend): extract features/cwd-browser.js

Phase 4 Task 12."
```

---

## Task 13: `static/js/features/{usage,weekly-report,sync-settings,bell}.js`

**Files:** Create 4 feature modules.

- [ ] **Step 1: `static/js/features/usage.js`** — copy `openUsageModal/renderUsage` (~lines 1605-1701). Import `pct` from `../util/format.js`.

- [ ] **Step 2: `static/js/features/weekly-report.js`** — copy `openWeeklyReportModal/loadWeeklyReportConfig/saveWeeklyReport/runWeeklyReportNow/setWRStatus/toast` (~lines 1702-1896). `toast` is shared utility — re-imported by other features as needed.

- [ ] **Step 3: `static/js/features/sync-settings.js`** — copy `openSyncSettingsModal/loadSyncTargets/renderSyncTargets/patchSyncTarget/confirmDeleteSyncTarget/openAddSyncTarget/fillSyncSettings/setSSStatus` (~lines 1897-2171). Import `toast` from `./weekly-report.js`.

- [ ] **Step 4: `static/js/features/bell.js`** — copy `applyBellUI/checkBell/openBellPanel` (~lines 2570-2665). **DELETE `setupPush` (~lines 2529-2569) — 46 行死代码（spec calls out for deletion）**.

```javascript
import { apiGet, apiPost } from '../api.js';
import { fmtDue } from '../util/format.js';

export function applyBellUI() { /* copy */ }
export async function checkBell() { /* copy */ }
export async function openBellPanel() { /* copy */ }

// setupPush(): DELETED — 46 lines of legacy push subscription code
// that's been replaced by server-side push.py + /api/subscribe.
```

- [ ] **Step 5: Syntax + commit**

```bash
for f in static/js/features/{usage,weekly-report,sync-settings,bell}.js; do node --check "$f"; done
git add static/js/features/usage.js static/js/features/weekly-report.js static/js/features/sync-settings.js static/js/features/bell.js
git commit -m "refactor(frontend): extract features/{usage,weekly-report,sync-settings,bell}

Phase 4 Task 13. setupPush (46 lines) DELETED — dead code."
```

---

## Task 14: `static/js/boot.js` 顶层入口

**Files:** Create `static/js/boot.js`

把 legacy `bootApp()` (~line 2856) + DOMContentLoaded listeners 改造成 ES module 入口。

- [ ] **Step 1: Read legacy event-wiring code**

```bash
# Legacy bootApp body + post-IIFE event wiring
sed -n '2856,2873p' static/app.js
# Search for addEventListener in legacy
grep -n "addEventListener\|onclick\|on[A-Z]" static/app.js | head -30
```

- [ ] **Step 2: Write `static/js/boot.js`**

```javascript
/**
 * App entrypoint. Loaded as <script type="module" src="/static/app.js">.
 *
 * Responsibilities (mirrors legacy bootApp + DOMContentLoaded handler):
 * 1. Wire DOM event listeners
 * 2. Initialize source picker / current source
 * 3. Connect WebSocket
 * 4. Load sessions list + meta + bell
 */
import {
  sendBtn, attachBtn, pasteBtn, cameraBtn, menuBtn, notifBtn,
  drawerBtn, drawerClose, newSessionBtn, sessionSearch, sessionSearchClear,
  input, menu, attachMenu,
} from './dom.js';

import { set, get } from './state.js';
import { connect, sendWs } from './ws/socket.js';
import { sendCurrent } from './composer/send.js';
import { autoresize, onPaste } from './composer/input.js';
import { toggleDrawer, closeDrawer } from './session/drawer.js';
import { loadSessionList, applySearch } from './session/list.js';
import { renderModelMenu } from './session/header.js';
import { openUsageModal } from './features/usage.js';
import { openWeeklyReportModal } from './features/weekly-report.js';
import { openSyncSettingsModal } from './features/sync-settings.js';
import { openCheckinDialog } from './features/checkin.js';
import { openCwdBrowser } from './features/cwd-browser.js';
import { applyBellUI, checkBell, openBellPanel } from './features/bell.js';
import {
  loadSources, getCurrentSourceId, findSource, renderPicker,
  showPicker, enterSource,
} from './features/sources.js';
import { apiGet } from './api.js';

async function loadMeta() {
  try {
    const m = await apiGet('/api/meta');
    set('META', m);
    renderModelMenu();
  } catch (e) {
    console.warn('loadMeta failed', e);
  }
}

function wireEvents() {
  if (sendBtn) sendBtn.addEventListener('click', sendCurrent);
  if (input) {
    input.addEventListener('input', autoresize);
    input.addEventListener('paste', onPaste);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        sendCurrent();
      }
    });
  }

  if (drawerBtn) drawerBtn.addEventListener('click', toggleDrawer);
  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  if (newSessionBtn) newSessionBtn.addEventListener('click', () => {
    sendWs({ type: 'cmd', name: 'new_session', mode: get('currentMode') });
  });
  if (sessionSearch) sessionSearch.addEventListener('input', () => applySearch());
  if (sessionSearchClear) sessionSearchClear.addEventListener('click', () => {
    sessionSearch.value = '';
    applySearch(true);
  });

  if (menu) {
    menu.addEventListener('click', (e) => {
      const cmd = e.target?.dataset?.cmd || e.target?.closest('[data-cmd]')?.dataset?.cmd;
      if (!cmd) return;
      menu.classList.add('hidden');
      switch (cmd) {
        case 'rename': /* inline rename — legacy behavior preserved */ break;
        case 'cwd-prompt': openCwdBrowser('cwd'); break;
        case 'usage': openUsageModal(); break;
        case 'weekly-report': openWeeklyReportModal(); break;
        case 'sync-settings': openSyncSettingsModal(); break;
      }
    });
  }

  if (notifBtn) notifBtn.addEventListener('click', openBellPanel);

  if (attachBtn) attachBtn.addEventListener('click', () => {
    if (attachMenu) attachMenu.classList.toggle('hidden');
  });

  // Attach menu items — checkin button if present
  const checkinBtn = document.querySelector('[data-attach-cmd="checkin"]');
  if (checkinBtn) checkinBtn.addEventListener('click', () => {
    if (attachMenu) attachMenu.classList.add('hidden');
    openCheckinDialog();
  });
}

async function init() {
  wireEvents();

  const sources = loadSources();
  const currentId = getCurrentSourceId();
  let current = currentId ? findSource(currentId) : null;
  if (!current && sources.length === 1) current = sources[0];

  if (!current) {
    showPicker();
    renderPicker();
    return;
  }
  set('currentSource', current);
  enterSource(current.id);

  await loadMeta();
  connect();
  loadSessionList();
  applyBellUI();
  checkBell();
  setInterval(checkBell, 60000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
```

The wireEvents body must cover EVERY legacy event listener — diff against legacy `app.js` carefully.

- [ ] **Step 3: Syntax + commit**

```bash
node --check static/js/boot.js
git add static/js/boot.js
git commit -m "refactor(frontend): add boot.js module entrypoint

Phase 4 Task 14."
```

---

## Task 15: Atomic swap

**Files:**
- `git mv static/app.js static/app.legacy.js`
- Create new `static/app.js` (3 lines)
- Modify `static/index.html`

- [ ] **Step 1: Rename + new entry**

```bash
git mv static/app.js static/app.legacy.js
cat > static/app.js <<'EOF'
// Phase 4 entry. Just imports boot.js. Loaded as
// <script type="module" src="/static/app.js"> in index.html.
import './js/boot.js';
EOF
wc -l static/app.js  # expect 3
```

- [ ] **Step 2: Modify `static/index.html`**

Find:
```html
<script src="/static/app.js?v=46"></script>
```

Replace with:
```html
<script src="/static/vendor/purify.min.js?v=47"></script>
<script type="module" src="/static/app.js?v=47"></script>
```

Bump CSS version too:
```html
<link rel="stylesheet" href="/static/style.css?v=47">
```

(CSS will be split in Task 17; for now just bump.)

Also bump `/static/icons.js?v=47`.

- [ ] **Step 3: Static-asset test**

```bash
scp static/index.html static/app.js dashboard-server:/home/dev/phone-bridge/static/
scp -r static/js static/vendor dashboard-server:/home/dev/phone-bridge/static/
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_static_assets.py -v 2>&1 | tail -5'
```

Expected: 3/3 pass.

- [ ] **Step 4: Deploy + smoke**

```bash
deploy
```

```powershell
$env:BASE = "https://dashboard-server.tail4cfa2.ts.net"
$env:BRIDGE_COOKIE = "bridge_session=<cookie>"
python tests/smoke_backend.py
```

Expected: 5/5 green.

- [ ] **Step 5: USER manual verification (10 min)**

Open PWA in browser, DevTools open. Verify:
- No console errors / 404s in Network
- Send a message → assistant_text streams
- Open drawer → session list renders
- Click each menu item: cwd-prompt / usage / weekly / sync-settings (each modal opens)
- Bell icon clickable, shows panel
- Composer: attach button, paste, send
- Markdown code block renders correctly (DOMPurify keeps `<pre><code>`)

If ANY breaks:
```bash
# Hotfix: revert index.html to load legacy
sed -i.bak 's|src="/static/app.js"|src="/static/app.legacy.js"|;s|type="module" ||' static/index.html
deploy
```

- [ ] **Step 6: Commit**

```bash
git add static/app.js static/app.legacy.js static/index.html
git commit -m "feat(frontend): atomic swap to ES Modules

Phase 4 Task 15. index.html now loads boot.js via <script type=module>.
Legacy app.js renamed to app.legacy.js (kept 24h as rollback)."
```

---

## Task 16: Delete `app.legacy.js`

**Files:** `git rm static/app.legacy.js`

After ~24h verification, legacy file no longer needed.

- [ ] **Step 1: Delete + commit**

```bash
git rm static/app.legacy.js
git commit -m "chore(frontend): delete app.legacy.js after Phase 4 swap verified

Phase 4 Task 16."
```

---

## Task 17: CSS 拆分到 `static/css/`

**Files:** Create 16 CSS files; delete `static/style.css`; modify `static/index.html`.

CSS 拆分独立于 JS swap，无 import 依赖。Cascade order 重要 — `<link>` 顺序决定优先级。

- [ ] **Step 1: Create `static/css/tokens.css`** with design tokens

```css
/* Design tokens. Single source for spacing/sizing/fonts/colors. */
:root {
  /* Spacing */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;

  /* Radius */
  --radius-sm: 4px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-full: 9999px;

  /* Font */
  --font-xs: 11px;
  --font-sm: 13px;
  --font-base: 15px;
  --font-lg: 17px;
  --font-xl: 20px;

  /* Colors */
  --bg-0: #0a0a0a;
  --bg-1: #1a1a1a;
  --bg-2: #2a2a2a;
  --text-0: #e0e0e0;
  --text-1: #b0b0b0;
  --text-2: #707070;
  --accent: #5fa8ff;
  --error: #ff6b6b;
  --warn: #ffb86b;
  --ok: #5fdc8b;
  --border: #333;
}
```

- [ ] **Step 2: Read `static/style.css` and split**

```bash
wc -l static/style.css
grep -n "^/\*\|^/\*\*\|^\." static/style.css | head -50
```

Open the 2048-line file and split by section comments:
- `:root` token block + `body/html` reset → `base.css`
- `.app-bar` + `.title` + `.actions` + `.icon-btn` (app-bar variant) → `appbar.css`
- `.drawer` + `.session-list` + `.session-search` → `drawer.css`
- `.messages` + `.bubble` + `.assistant` + `.user` + `.stream-tail` → `messages.css`
- `.tool-group` + `.perm-card` → `tools-perms.css`
- `.composer` + `.input` + `.attach-bar` → `composer.css`
- `.source-picker` + `.sp-list` → `picker.css`
- `.hidden` + `.muted` + `.icon-btn` (utility variant) + `.ic` + .toast → `utilities.css`
- `.app-bar` containers + `.main-pane` grid → `layout.css`
- `.checkin-dialog` + `.cd-*` → `dialogs/checkin.css`
- `.modal` + `.wr-*` (weekly-report) shared base → `dialogs/sync.css` (sync + WR share modal styles)
- `.usage-*` → `dialogs/usage.css`
- weekly-specific overrides → `dialogs/weekly.css`
- `.cwd-*` + `.modal-list` → `dialogs/cwd.css`
- `.bell-*` → `dialogs/bell.css`

Pull rules into each file preserving original order. tokens.css overrides the legacy `:root` block.

- [ ] **Step 3: Modify `static/index.html`** — 1 link → 16 links

```html
<link rel="stylesheet" href="/static/css/tokens.css?v=47">
<link rel="stylesheet" href="/static/css/base.css?v=47">
<link rel="stylesheet" href="/static/css/utilities.css?v=47">
<link rel="stylesheet" href="/static/css/layout.css?v=47">
<link rel="stylesheet" href="/static/css/appbar.css?v=47">
<link rel="stylesheet" href="/static/css/drawer.css?v=47">
<link rel="stylesheet" href="/static/css/messages.css?v=47">
<link rel="stylesheet" href="/static/css/tools-perms.css?v=47">
<link rel="stylesheet" href="/static/css/composer.css?v=47">
<link rel="stylesheet" href="/static/css/picker.css?v=47">
<link rel="stylesheet" href="/static/css/dialogs/checkin.css?v=47">
<link rel="stylesheet" href="/static/css/dialogs/usage.css?v=47">
<link rel="stylesheet" href="/static/css/dialogs/sync.css?v=47">
<link rel="stylesheet" href="/static/css/dialogs/weekly.css?v=47">
<link rel="stylesheet" href="/static/css/dialogs/cwd.css?v=47">
<link rel="stylesheet" href="/static/css/dialogs/bell.css?v=47">
```

Delete the old `<link rel="stylesheet" href="/static/style.css?v=47">` line.

- [ ] **Step 4: Delete `static/style.css`**

```bash
git rm static/style.css
grep -rn "style\.css" static/ tests/  # should be empty
```

- [ ] **Step 5: Deploy + visual check**

```bash
deploy
```

USER: open PWA, take screenshots, compare against `tests/baseline/*.png`. No layout regressions.

- [ ] **Step 6: Commit**

```bash
git add static/css/ static/index.html
git commit -m "refactor(frontend): split style.css into 16 files under css/

Phase 4 Task 17."
```

---

## Task 18: iOS 14 `<dialog>` fallback

**Files:** Create `static/js/util/dialog.js`; modify features that use `<dialog>`.

iOS < 15 doesn't support `<dialog>.showModal()`.

- [ ] **Step 1: Create `static/js/util/dialog.js`**

```javascript
/**
 * Tiny <dialog>.showModal() fallback for iOS 14.
 *
 * Returns a "close" function.
 */
export function openDialog(el) {
  if (!el) return () => {};
  if (typeof el.showModal === 'function') {
    el.showModal();
    return () => el.close();
  }
  el.classList.add('dialog-open');
  el.setAttribute('aria-modal', 'true');
  const prevBodyOverflow = document.body.style.overflow;
  document.body.style.overflow = 'hidden';
  return () => {
    el.classList.remove('dialog-open');
    el.removeAttribute('aria-modal');
    document.body.style.overflow = prevBodyOverflow;
  };
}
```

- [ ] **Step 2: Add CSS fallback to `static/css/dialogs/checkin.css`**

```css
/* iOS 14 / older Safari fallback for <dialog> */
dialog.checkin-dialog:not([open]).dialog-open {
  display: block;
  position: fixed;
  inset: 0;
  margin: auto;
  background: var(--bg-1);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  z-index: 100;
  max-width: 92vw;
  max-height: 80vh;
  overflow: auto;
}
dialog.checkin-dialog:not([open]).dialog-open::backdrop {
  background: rgba(0, 0, 0, 0.6);
}
```

- [ ] **Step 3: Use `openDialog` in `static/js/features/checkin.js`**

Find calls to `checkinDialog.showModal()` and replace:
```javascript
import { openDialog } from '../util/dialog.js';
// ...
const close = openDialog(checkinDialog);
// later:
close();
```

Repeat for any other `<dialog>` usage (cwd-browser if applicable).

- [ ] **Step 4: Syntax + commit**

```bash
node --check static/js/util/dialog.js
git add static/js/util/dialog.js static/js/features/checkin.js static/css/dialogs/checkin.css
git commit -m "feat(frontend): iOS 14 <dialog> fallback

Phase 4 Task 18."
```

---

## Task 19: Service Worker cache update

**Files:** Modify `static/sw.js`

- [ ] **Step 1: Read current sw.js**

```bash
cat static/sw.js
```

- [ ] **Step 2: Update cache manifest**

```javascript
const CACHE_VERSION = 'v47';
const CACHE_NAME = `phone-bridge-${CACHE_VERSION}`;

const ASSETS = [
  '/',
  '/static/icon.svg',
  '/static/icons.js?v=47',
  '/static/marked.min.js',
  '/static/vendor/purify.min.js?v=47',
  '/static/app.js?v=47',

  '/static/js/boot.js',
  '/static/js/state.js',
  '/static/js/dom.js',
  '/static/js/api.js',
  '/static/js/util/escape.js',
  '/static/js/util/format.js',
  '/static/js/util/timers.js',
  '/static/js/util/yaml.js',
  '/static/js/util/dialog.js',
  '/static/js/ws/socket.js',
  '/static/js/ws/handlers.js',
  '/static/js/render/markdown.js',
  '/static/js/render/scroll.js',
  '/static/js/render/typing.js',
  '/static/js/render/message.js',
  '/static/js/render/tool.js',
  '/static/js/render/perm.js',
  '/static/js/render/checkin-card.js',
  '/static/js/session/header.js',
  '/static/js/session/list.js',
  '/static/js/session/drawer.js',
  '/static/js/composer/input.js',
  '/static/js/composer/attachments.js',
  '/static/js/composer/send.js',
  '/static/js/features/sources.js',
  '/static/js/features/checkin.js',
  '/static/js/features/cwd-browser.js',
  '/static/js/features/usage.js',
  '/static/js/features/weekly-report.js',
  '/static/js/features/sync-settings.js',
  '/static/js/features/bell.js',

  '/static/css/tokens.css?v=47',
  '/static/css/base.css?v=47',
  '/static/css/utilities.css?v=47',
  '/static/css/layout.css?v=47',
  '/static/css/appbar.css?v=47',
  '/static/css/drawer.css?v=47',
  '/static/css/messages.css?v=47',
  '/static/css/tools-perms.css?v=47',
  '/static/css/composer.css?v=47',
  '/static/css/picker.css?v=47',
  '/static/css/dialogs/checkin.css?v=47',
  '/static/css/dialogs/usage.css?v=47',
  '/static/css/dialogs/sync.css?v=47',
  '/static/css/dialogs/weekly.css?v=47',
  '/static/css/dialogs/cwd.css?v=47',
  '/static/css/dialogs/bell.css?v=47',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE_NAME).then((c) => c.addAll(ASSETS)));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
});

// existing fetch handler — preserve
```

- [ ] **Step 3: Commit**

```bash
git add static/sw.js
git commit -m "feat(frontend): SW cache covers all Phase 4 modules + CSS

Phase 4 Task 19. CACHE_NAME bumped to v47."
```

---

## Task 20: Final deploy + verification

- [ ] **Step 1: Deploy + smoke + unit tests on VM**

```bash
deploy
```

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/test_static_assets.py tests/test_session_manager.py tests/test_notion_api_backoff.py tests/test_pb_client.py tests/test_settings.py tests/test_io_utils.py -v 2>&1 | tail -10'
```

Expected: 38/38 green.

```powershell
$env:BASE = "https://dashboard-server.tail4cfa2.ts.net"
$env:BRIDGE_COOKIE = "bridge_session=<cookie>"
python tests/smoke_backend.py
```

Expected: 5/5 green.

- [ ] **Step 2: USER manual flow check (15 min)**

Fresh browser (DevTools "Disable cache" + hard reload):
1. PWA loads, no console errors, no 404s
2. `window.DOMPurify` defined
3. Send long message → assistant_text streams smoothly, scrolls, finalizes
4. Open ALL modals: usage / weekly / sync / cwd-browser / checkin / bell
5. Switch session via drawer; switch model via pill
6. Take screenshots of each, diff against `tests/baseline/*.png`
7. Offline test: DevTools → Application → Service Workers → Offline → hard reload. Shell loads; send blocked; no crash.

- [ ] **Step 3: iOS test (USER, if available)**

Any iOS 14+ device: load PWA, open checkin modal (verify fallback dialog appears), send message, receive response.

- [ ] **Step 4: 24h staging soak**

After 24h:
```bash
ssh dashboard-server 'sudo journalctl -u phone-bridge --since "24 hours ago" --no-pager | grep -iE "error|exception|traceback" | head -10'
```

Expected: empty.

---

## Task 21: Completion report + merge to main

- [ ] **Step 1: Write CHANGELOG entry**

Insert above the Phase 3 entry in `CHANGELOG.md`:

```markdown
## 2026-06-XX — Phase 4 · 前端模块化 + XSS 防护 + 流式渲染 + CSS 拆分

**Branch:** `refactor/phase-4-frontend-modules` (~23 commits)
**实际工时:** 约 X 小时

### 落地的事
- `static/app.js` (2873 行 IIFE) → 30+ ES Modules under `static/js/`
- `static/style.css` (2048 行) → 16 文件 under `static/css/` + design tokens
- vendored DOMPurify v3.2.4 + 所有 `innerHTML = markdown_html` 改 sanitized
- 流式渲染：buffer 到 paragraph/code-block 边界才完整 markdown parse，期间用 textContent 追加。长回答 CPU 不再爆炸。
- iOS 14 `<dialog>` fallback via `openDialog()` + `.dialog-open` CSS class
- SW cache 清单覆盖所有新模块 + 16 CSS
- 删 setupPush 46 行死代码
- 新增 `tests/test_static_assets.py`：3 个测试守护 import 完整性 + 资源存在 + DOMPurify 引用

### 闸门
- ✅ 38/38 unit tests green (35 Phase 3 + 3 new)
- ✅ smoke 5/5
- ✅ 8 baseline screenshots → final 视觉无 regression
- ✅ iOS dialog fallback 验证
- ✅ 24h staging soak journal 0 ERROR

### 偏离计划
(fill in after execution)

### 量化
- `static/app.js`: 2873 → 3 行 entry (-99.9%)
- 新增 ES modules ~3000 行 (30+ 文件)
- `static/style.css`: 2048 → 0 行 (拆到 16 文件 ~2050 行 + design tokens)
- 删除死代码 46 行 (setupPush)

### 下一步
👉 Phase 5 · `notion_sync/runner.py` 拆解 + 算法升级
新窗口续接指令："继续重构路线图，从 Phase 5 开始"
```

- [ ] **Step 2: Update progress table in `docs/superpowers/specs/2026-06-06-refactor-roadmap.md`**

```markdown
| 4 前端 | 🚧 已部署 待合并 | `refactor/phase-4-frontend-modules` | 2026-06-XX | `<head-SHA>` | CHANGELOG §Phase 4 |
| 5 sync | ⏳ 待开始 | `refactor/phase-5-sync-runner` | — | — | — |
```

Update 下一步入口 section.

- [ ] **Step 3: Commit on branch**

```bash
git add CHANGELOG.md docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(changelog): Phase 4 completion report

Phase 4 Task 21."
```

- [ ] **Step 4: Merge to main**

```bash
git checkout main
git merge --no-ff refactor/phase-4-frontend-modules -m "Merge branch 'refactor/phase-4-frontend-modules'

Phase 4 · 前端模块化 + XSS 防护 + 流式渲染 + CSS 拆分

详见 CHANGELOG §Phase 4。"
git log --oneline -3  # capture merge SHA
```

- [ ] **Step 5: Update roadmap on main with merge SHA**

```markdown
| 4 前端 | ✅ 已合并 | `refactor/phase-4-frontend-modules` | 2026-06-XX | `<merge-SHA>` | CHANGELOG §Phase 4 |
```

```bash
git add docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(roadmap): mark Phase 4 ✅ merged at <SHA>; Phase 5 next"
git push origin main
```

---

## Self-Review

**1. Spec coverage:**

| Spec 动作 | Plan task |
|---|---|
| 拆 ES Modules | Tasks 1-14 (modules) + 15 (swap) + 16 (delete legacy) |
| state.js: 80 个 let 集中成 store + subscribe | Task 2 |
| ws/handlers.js: 表驱动 + endStream() helper | Task 7 |
| 流式增量渲染 | Task 5 (markdown.js appendStreamChunk) |
| vendored DOMPurify + 所有 innerHTML 走 sanitize | Task 0 (vendor) + Task 5 (markdown.js) + Tasks 6-13 (render/feature audits) |
| 删 setupPush 46 行死代码 | Task 13 |
| style.css 拆 12 文件 + tokens | Task 17 (16 文件 implementation) |
| `<dialog>`.showModal() polyfill / fallback | Task 18 |
| JS/CSS 版本号合一 (?v=N) | Tasks 15 + 17 (both bump to v47) |
| sw.js 缓存清单更新 | Task 19 |
| 统一 fetch wrapper api.js | Task 4 + Tasks 6-13 use it |
| **准出:** playwright smoke | Replaced by user manual + smoke_backend.py (Task 20) |
| **准出:** baseline 截图对比 | Task 0 capture + Tasks 15+17+20 visual check |
| **准出:** iOS 14/15/17 PWA | Task 18 fallback + Task 20 manual test |
| **准出:** 长回答 CPU < 50% | Task 5 streaming + manual measurement at Task 20 |
| **准出:** 离线启动正常 | Task 19 (SW cache) + Task 20 DevTools offline test |

✅ All spec items covered.

**2. Placeholder scan:**

Inspected — `/* copy body from legacy app.js */` markers in tasks 1, 6, 8, 9, 10, 11, 12, 13 are POINTERS to actual code already in the codebase (legacy `app.js`). These are mechanical copy operations; the body content is fixed by the existing source. Not deferred design work.

The `// TODO inline` comment for menu rename in Task 14 is documentation of legacy behavior to preserve, not a deferred placeholder. The implementer will inline the existing rename UI from legacy.

**3. Type consistency:**

- `apiGet/apiPost/apiPatch/apiPut/apiDelete/apiPostForm` from `api.js`
- `get(key)/set(key, value)/subscribe(key, cb)` from `state.js`
- DOM exports from `dom.js`
- `sendWs(obj)` from `ws/socket.js`
- `renderMarkdownFinal/appendStreamChunk/finalizeStream` from `render/markdown.js`
- `openDialog(el)` from `util/dialog.js` — returns close function
- `dispatch(msg)` from `ws/handlers.js`

All consistent.

**4. Order dependencies:**

- Task 0 first (DOMPurify enables Task 5; tests enable verification)
- Tasks 1-4 base layer (util + state + dom + api)
- Task 5 (markdown.js) depends on DOMPurify being vendored
- Task 6 (render/) depends on state/dom/api/markdown
- Task 7 (ws/) depends on state/render/session
- Task 8 (session/) depends on state/dom/api
- Task 9 (composer/) depends on state/dom/api/ws/render
- Tasks 10-13 (features/) depend on api/state/composer/render/util/dialog
- Task 14 (boot.js) depends on EVERYTHING
- Task 15 (atomic swap) — go-live
- Task 16 (delete legacy) — after Task 15 verified
- Tasks 17-19 (CSS / iOS / SW) — independent of JS swap; can interleave with 15/16 if careful
- Task 20 (verification) — after all
- Task 21 (merge) — final

**5. Honest scope:**

- 22 tasks (0-21), ~32 new files + 4 modifies + 2 deletes
- ~3-5 days wall-clock
- Each task independently revertible until Task 15 (atomic swap); Task 15 has built-in rollback
- Visual diff is primary regression net (no JS unit tests)
- DOMPurify integration risk: legacy has 65 innerHTML sites — Task 5 introduces the sanitize path; Tasks 6-13's render/feature modules should NOT bypass it (audit during each task)

---

**Plan complete.**
