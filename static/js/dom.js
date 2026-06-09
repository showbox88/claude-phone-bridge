/**
 * DOM element cache. One named export per document.getElementById.
 * Modules import only what they need. Modules MUST NOT call
 * getElementById themselves — go through this file.
 *
 * Returns null for elements that don't exist; callers should null-check
 * before use.
 *
 * Note: lazily-created modals (cwd-modal, usage-modal, wr-modal, ss-modal,
 * sync-add-dialog form fields, toast) are NOT cached here — they are
 * (re)looked-up by the modules that create them, since they may not exist
 * at module-load time.
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
export const filePickBtn = $('file-pick-btn'); // legacy; may be null after menu consolidation
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
export const syncBtn = $('sync-btn');
export const appVersion = $('app-version');
export const workspaceIndicator = $('workspace-indicator');
export const workspaceToggle = $('workspace-toggle');

// Model picker (in app bar / menu)
export const modelBtn = $('model-btn');
export const modelLabel = $('model-label');
export const modelMenu = $('model-menu');

// Drawer
export const drawer = $('drawer');
export const drawerMask = $('drawer-mask');
export const drawerBtn = $('drawer-btn');
export const drawerClose = $('drawer-close');
export const newSessionBtn = $('new-session-btn');
export const sessionListEl = $('session-list');
export const sessionSearch = $('session-search');
export const sessionSearchClear = $('session-search-clear');

// Source picker (multi-backend selector)
export const sourcePicker = $('source-picker');
export const sourceBtn = $('source-btn');
export const sourceName = $('source-name');
export const spList = $('sp-list');
export const spAddBtn = $('sp-add-btn');
export const spForm = $('sp-form');
export const spFormTitle = $('sp-form-title');
export const spName = $('sp-name');
export const spUrl = $('sp-url');
export const spCancel = $('sp-cancel');
export const spSave = $('sp-save');

// Check-in dialog (cd-*)
export const checkinDialog = $('checkin-dialog');
export const cdStatus = $('cd-status');
export const cdList = $('cd-list');
export const cdManualName = $('cd-manual-name');
export const cdManualGo = $('cd-manual-go');
export const cdStageList = $('cd-stage-list');
export const cdStageForm = $('cd-stage-form');
export const cdBack = $('cd-back');
export const cdFormName = $('cd-form-name');
export const cdFormMeta = $('cd-form-meta');
export const cdActivity = $('cd-activity');
export const cdAmount = $('cd-amount');
export const cdCurrency = $('cd-currency');
export const cdScore = $('cd-score');
export const cdScoreVal = $('cd-score-val');
export const cdNote = $('cd-note');
export const cdBuildLoc = $('cd-build-loc');
export const cdSubmit = $('cd-submit');

// Sync-add dialog (static shell — inner fields populated when opened)
export const syncAddDialog = $('sync-add-dialog');
