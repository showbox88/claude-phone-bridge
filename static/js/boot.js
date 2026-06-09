/**
 * App entrypoint. Loaded as <script type="module" src="/static/app.js">
 * after the Task 15 swap.
 *
 * Responsibilities (mirrors legacy bootApp + the IIFE's top-level wiring):
 *   1. Wire DOM event listeners that don't belong to a specific module
 *      (cross-module wiring like "send button → sendCurrent",
 *      "menu items → feature modules", drawer/menu open-close, source
 *      picker form, etc.).
 *   2. Initialize source picker / current source on startup.
 *   3. Kick off loadMeta + connect + loadSessionList + bell polling
 *      once a source is selected.
 *
 * boot.js NEVER touches DOM directly to render — it only attaches event
 * handlers. All rendering / business logic lives in feature modules.
 *
 * Note on what's NOT in boot.js (already self-wired in their own modules):
 *   - visibilitychange WS reconnect — ws/socket.js
 *   - messages scroll / image-load auto-stick — render/scroll.js
 *   - checkin-dialog activity/score/back/submit/manual entry — features/checkin.js
 *   - desktop drawer matchMedia restore is done inline below since it's
 *     a one-shot at startup
 */
import { get, set } from './state.js';

import {
  // composer
  input, sendBtn, attachBtn, pasteBtn, cameraBtn, attachMenu,
  albumInput, galleryInput, fileInput, cameraInput,
  filePickBtn,
  // app bar
  menu, menuBtn, notifBtn, syncBtn, modelBtn, modelMenu,
  // drawer
  drawerBtn, drawerClose, drawerMask, newSessionBtn,
  sessionSearch, sessionSearchClear,
  // source picker
  spAddBtn, spCancel, spSave, spName, spUrl, sourceBtn, sourceName,
} from './dom.js';

import { apiUrl } from './api.js';
import { apiGet } from './api.js';
import { sendWs } from './ws/socket.js';

// composer
import { autoresize, onPaste } from './composer/input.js';
import { sendCurrent } from './composer/send.js';
import { uploadFiles, pasteFromClipboard } from './composer/attachments.js';

// session
import { toggleDrawer, closeDrawer, isDesktopDrawer } from './session/drawer.js';
import { loadSessionList, applySearch } from './session/list.js';
import { setMode, refreshModelPill, renderModelMenu } from './session/header.js';

// features
import { openUsageModal } from './features/usage.js';
import { openWeeklyReportModal } from './features/weekly-report.js';
import { openSyncSettingsModal } from './features/sync-settings.js';
import { openCheckinDialog } from './features/checkin.js';
import { openCwdBrowser } from './features/cwd-browser.js';
import { applyBellUI, checkBell, openBellPanel } from './features/bell.js';
import {
  loadSources, getCurrentSourceId,
  showPicker, hidePicker,
  enterSource, exitToSourcePicker,
  openSourceForm, closeSourceForm, saveSourceForm,
} from './features/sources.js';

// Toast for the header sync-now spinner (legacy line 1873/1883/1888).
import { toast } from './features/weekly-report.js';

// ---------- meta ----------

async function loadMeta() {
  try {
    const m = await apiGet('/api/meta');
    set('META', m);
    // re-render the model pill (and YOLO state) now that we have labels.
    refreshModelPill();
  } catch (_) { /* ignore — legacy was silent */ }
}

// ---------- event wiring ----------

function wireComposer() {
  if (input) {
    input.addEventListener('input', autoresize);
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        sendCurrent();
      }
    });
  }
  if (sendBtn) sendBtn.addEventListener('click', sendCurrent);

  // Document-level paste handler: legacy bound on document so screenshot
  // paste works whether or not the textarea is focused.
  document.addEventListener('paste', onPaste);

  // Header paste button (clipboard pick).
  if (pasteBtn) {
    pasteBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      pasteFromClipboard();
    });
  }

  // The legacy code hides the camera button — preserve that.
  if (cameraBtn) cameraBtn.style.display = 'none';

  // File input change handlers — common handler that uploads + resets value.
  const handleUploadInput = async (e) => {
    if (!e.target.files || e.target.files.length === 0) return;
    await uploadFiles(e.target.files);
    e.target.value = '';
  };
  if (fileInput) fileInput.addEventListener('change', handleUploadInput);
  if (cameraInput) cameraInput.addEventListener('change', handleUploadInput);
  if (albumInput) albumInput.addEventListener('change', handleUploadInput);
  if (galleryInput) galleryInput.addEventListener('change', handleUploadInput);

  // ⬆ button toggles the upward-expanding menu (checkin / attachments / etc.)
  if (attachBtn) {
    attachBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      attachMenu.classList.toggle('hidden');
    });
  }

  if (attachMenu) {
    attachMenu.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-pick]');
      if (!btn) return;
      attachMenu.classList.add('hidden');
      const pick = btn.dataset.pick;
      if (pick === 'checkin') openCheckinDialog();
      else if (pick === 'clipboard') pasteFromClipboard();
      else if (pick === 'camera') cameraInput && cameraInput.click();
      else if (pick === 'album') albumInput && albumInput.click();
      else if (pick === 'file') fileInput && fileInput.click();
      else if (pick === 'cwd-file') openCwdBrowser('file');
      else if (pick === 'other') fileInput && fileInput.click();
    });
    document.addEventListener('click', (e) => {
      if (attachMenu.classList.contains('hidden')) return;
      if (!attachMenu.contains(e.target) && e.target !== attachBtn) {
        attachMenu.classList.add('hidden');
      }
    });
  }

  // Legacy fallback: standalone file-pick button (if present in DOM).
  if (filePickBtn) filePickBtn.addEventListener('click', () => openCwdBrowser('file'));
}

function wireDrawer() {
  if (drawerBtn) drawerBtn.addEventListener('click', toggleDrawer);
  if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
  if (drawerMask) drawerMask.addEventListener('click', closeDrawer);
  if (newSessionBtn) {
    newSessionBtn.addEventListener('click', () => {
      sendWs({ type: 'cmd', name: 'new_session', mode: get('currentMode') });
      if (!isDesktopDrawer()) closeDrawer();
    });
  }

  // Session search (debounced via applySearch internally).
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
      if (!sessionSearch) return;
      sessionSearch.value = '';
      sessionSearch.focus();
      applySearch(true);
    });
  }

  // Desktop drawer: restore expanded state from localStorage at startup
  // (legacy lines 932-935). Mobile drawer defaults to collapsed.
  if (isDesktopDrawer() && localStorage.getItem('bridge.drawer_expanded') === '1') {
    document.body.classList.add('drawer-expanded');
    loadSessionList();
  }
}

function wireTopMenu() {
  if (menuBtn) {
    menuBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (menu) menu.classList.toggle('hidden');
    });
  }
  // Outside-click closes the menu — legacy behavior.
  document.addEventListener('click', () => { if (menu) menu.classList.add('hidden'); });
  if (menu) {
    menu.addEventListener('click', (e) => e.stopPropagation());
    menu.querySelectorAll('button').forEach((b) => {
      b.addEventListener('click', () => {
        const cmd = b.dataset.cmd;
        menu.classList.add('hidden');
        if (cmd === 'new') sendWs({ type: 'cmd', name: 'new_session' });
        else if (cmd === 'cancel') sendWs({ type: 'cmd', name: 'cancel' });
        else if (cmd === 'cwd-prompt') openCwdBrowser('cwd');
        else if (cmd === 'rename') {
          const sid = get('currentSessionId');
          if (!sid) return;
          const t = prompt('会话标题:', get('currentSessionTitle') || '');
          if (t === null) return;
          sendWs({ type: 'cmd', name: 'rename_session', id: sid, title: t });
        }
        else if (cmd === 'usage') openUsageModal();
        else if (cmd === 'weekly-report') openWeeklyReportModal();
        else if (cmd === 'sync-settings') openSyncSettingsModal();
      });
    });
  }
}

function wireWorkspaceToggle() {
  // Chat ↔ Code segmented switch — opens a new session of that type.
  document.querySelectorAll('#workspace-toggle .seg-btn').forEach((b) => {
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      const newMode = b.dataset.workspace;
      if (newMode === get('currentMode')) return;  // already on this workspace
      // Optimistic UI: refresh session list filter immediately.
      setMode(newMode);
      loadSessionList();
      sendWs({ type: 'cmd', name: 'switch_workspace', mode: newMode });
    });
  });
}

function wireModelPicker() {
  if (!modelBtn || !modelMenu) return;
  modelBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!modelMenu.classList.contains('hidden')) {
      modelMenu.classList.add('hidden');
      return;
    }
    renderModelMenu();
    // Drop the menu below the model button (button lives in the app bar).
    const r = modelBtn.getBoundingClientRect();
    modelMenu.style.left = Math.max(8, r.right - 200) + 'px';
    modelMenu.style.top = (r.bottom + 6) + 'px';
    modelMenu.style.bottom = '';
    modelMenu.classList.remove('hidden');
  });
  document.addEventListener('click', () => modelMenu.classList.add('hidden'));
  modelMenu.addEventListener('click', (e) => e.stopPropagation());
}

function wireSyncButton() {
  // Header sync icon — fires an on-demand sync run (NOT the settings modal;
  // settings is reachable via the top-menu "sync-settings" item).
  if (!syncBtn) return;
  syncBtn.addEventListener('click', async () => {
    if (syncBtn.dataset.busy === '1') return;
    syncBtn.dataset.busy = '1';
    syncBtn.classList.add('spin');
    const oldTitle = syncBtn.title;
    syncBtn.title = '同步中…';
    try {
      const r = await fetch(apiUrl('/api/sync/now'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) {
        toast(`❌ 同步失败 (${r.status}): ${data.stderr || data.detail || '未知错误'}`, true);
      } else {
        const s = data.summary || {};
        const parts = [];
        if (s.applied)            parts.push(`applied=${s.applied}`);
        if (s.conflicts)          parts.push(`conflicts=${s.conflicts}`);
        if (s.deletes)            parts.push(`deletes=${s.deletes}`);
        if (s.pending)            parts.push(`pending=${s.pending}`);
        if (s.decisions_applied)  parts.push(`decisions=${s.decisions_applied}`);
        if (s.archived_resolved)  parts.push(`archived=${s.archived_resolved}`);
        toast(parts.length ? `✅ 同步完成: ${parts.join(' / ')}` : '✅ 同步完成: 无变化');
        // Refresh bell in case a Pending row just materialized.
        try { checkBell(); } catch (_) {}
      }
    } catch (e) {
      toast('❌ 同步失败: ' + (e.message || e), true);
    } finally {
      syncBtn.classList.remove('spin');
      syncBtn.dataset.busy = '';
      syncBtn.title = oldTitle;
    }
  });
}

function wireBell() {
  if (notifBtn) notifBtn.addEventListener('click', openBellPanel);
  // Visibility re-focus: refresh bell when page returns to foreground
  // (legacy 2657-2660). WS reconnect on visibility is handled in ws/socket.js.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) checkBell();
  });
}

function wireSourcePicker() {
  if (spAddBtn) spAddBtn.addEventListener('click', () => openSourceForm(null));
  if (spCancel) spCancel.addEventListener('click', closeSourceForm);
  if (spSave) spSave.addEventListener('click', saveSourceForm);
  // Submit on Enter inside the add/edit form.
  for (const el of [spName, spUrl]) {
    if (!el) continue;
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); saveSourceForm(); }
    });
  }
  // Top-bar source button → back to picker.
  if (sourceBtn) sourceBtn.addEventListener('click', exitToSourcePicker);
}

function wireEvents() {
  wireComposer();
  wireDrawer();
  wireTopMenu();
  wireWorkspaceToggle();
  wireModelPicker();
  wireSyncButton();
  wireBell();
  wireSourcePicker();
}

// ---------- service worker ----------

function registerServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* ignore */ });
  }
}

// ---------- boot ----------
// Legacy bootApp lived at app.js line 2856. Differences in the modular flow:
//   - legacy enterSource() calls loadMeta() AND connect(); the modular
//     enterSource (features/sources.js) only calls connect(), so we call
//     loadMeta() here after enterSource.
//   - bell polling (legacy 2653-2654) used to fire unconditionally at IIFE
//     end. We defer it to init() so it only kicks in after a source is
//     selected (otherwise apiUrl() has no base and the polls all fail).
async function init() {
  wireEvents();
  registerServiceWorker();

  const id = getCurrentSourceId();
  const sources = loadSources();
  const found = id ? sources.find((s) => s.id === id) : null;
  if (!found) {
    showPicker();
    if (sources.length === 0) openSourceForm(null);
    return;
  }

  // A source is selected — wire the main shell up.
  if (sourceName) sourceName.textContent = found.name;
  hidePicker();
  // enterSource resets shell state, sets currentSource, closes any prior
  // WS and connects a new one.
  enterSource(found.id);
  // loadMeta is NOT in the modular enterSource — invoke separately.
  await loadMeta();

  // Bell polling — same cadence as legacy (60s).
  applyBellUI();
  checkBell();
  setInterval(checkBell, 60_000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
