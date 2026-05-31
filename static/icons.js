// Lucide-style SVG icon set. Single-stroke, 1.75px, 24x24 viewBox.
// Use via window.icon('name') or data-icon attributes (auto-replaced on boot).
(function () {
  'use strict';
  const SVG = (path, opts = '') => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" ${opts}>${path}</svg>`;
  const SVG_FILL = (path) => `<svg viewBox="0 0 24 24" fill="currentColor">${path}</svg>`;

  const ICONS = {
    // navigation
    menu:   SVG('<line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/>'),
    more:   SVG_FILL('<circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/>'),
    close:  SVG('<line x1="6" y1="6" x2="18" y2="18"/><line x1="6" y1="18" x2="18" y2="6"/>'),
    caret:  SVG('<polyline points="6 9 12 15 18 9"/>'),
    back:   SVG('<line x1="20" y1="12" x2="4" y2="12"/><polyline points="10 6 4 12 10 18"/>'),
    chevron_up: SVG('<polyline points="6 15 12 9 18 15"/>'),
    search: SVG('<circle cx="11" cy="11" r="7"/><line x1="20" y1="20" x2="16.65" y2="16.65"/>'),

    // composer
    plus:   SVG('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'),
    send:   SVG('<line x1="12" y1="20" x2="12" y2="4"/><polyline points="6 10 12 4 18 10"/>'),
    stop:   SVG_FILL('<rect x="6" y="6" width="12" height="12" rx="2"/>'),

    // attachments
    camera: SVG('<path d="M21 19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3l1.5-2h5L16 6h3a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="3.5"/>'),
    image:  SVG('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><polyline points="21 15 16 10 5 21"/>'),
    paperclip: SVG('<path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/>'),
    clipboard: SVG('<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>'),
    file:   SVG('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>'),
    file_pdf:   SVG('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><text x="7.5" y="18" font-size="6" font-family="sans-serif" font-weight="700" fill="currentColor" stroke="none">PDF</text>'),
    file_sheet: SVG('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/><line x1="11" y1="13" x2="11" y2="20"/><line x1="14" y1="13" x2="14" y2="20"/>'),

    // actions
    bell:   SVG('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>'),
    bell_active: SVG('<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/><circle cx="18" cy="6" r="3" fill="currentColor" stroke="none"/>'),
    trash:  SVG('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
    edit:   SVG('<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>'),
    chart:  SVG('<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>'),
    folder: SVG('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'),
    calendar: SVG('<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>'),
    refresh: SVG('<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>'),
    copy:   SVG('<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
    check:  SVG('<polyline points="20 6 9 17 4 12"/>'),
    x:      SVG('<line x1="6" y1="6" x2="18" y2="18"/><line x1="6" y1="18" x2="18" y2="6"/>'),

    // device & workspace
    monitor: SVG('<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>'),
    chat:    SVG('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
    code:    SVG('<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>'),

    // brand mark — Claude sparkle
    sparkle: SVG_FILL('<path d="M12 2 L13 9.5 L21 11 L13 12.5 L12 21 L11 12.5 L3 11 L11 9.5 Z"/>'),

    // location / checkin
    pin:     SVG('<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>'),

    // tool indicators
    tool:    SVG('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>'),
    play:    SVG_FILL('<polygon points="6 4 20 12 6 20 6 4"/>'),
  };

  function renderIcon(name, opts = {}) {
    const raw = ICONS[name];
    if (!raw) return '';
    const size = opts.size || 20;
    const cls = opts.cls ? ` class="${opts.cls}"` : '';
    return raw.replace('<svg', `<svg width="${size}" height="${size}"${cls}`);
  }

  function hydrate(root = document) {
    root.querySelectorAll('[data-icon]').forEach((el) => {
      const name = el.dataset.icon;
      const size = parseInt(el.dataset.iconSize || '20', 10);
      const svg = renderIcon(name, { size });
      if (!svg) return;
      // Preserve any non-icon children (like spans following the icon).
      el.innerHTML = svg + (el.dataset.iconAfter || '');
      el.dataset.iconHydrated = '1';
    });
  }

  window.ICONS = ICONS;
  window.icon = renderIcon;
  window.hydrateIcons = hydrate;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => hydrate());
  } else {
    hydrate();
  }
})();
