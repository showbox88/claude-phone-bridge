// Format helpers — pure functions, no DOM access. Extracted from
// legacy app.js IIFE during Phase 4 modularization.

export function fmtMoney(v) {
  if (!v) return '$0.0000';
  if (v >= 1) return '$' + v.toFixed(2);
  return '$' + v.toFixed(4);
}

export function fmtTokens(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

export function fmtDue(s) {
  if (!s) return '无截止';
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

export function scoreStars(score) {
  const n = parseInt(score, 10);
  if (!Number.isFinite(n) || n <= 0) return '';
  const stars = Math.max(1, Math.min(5, Math.round(n / 2)));
  return '⭐'.repeat(stars);
}

export function currencySymbol(cur) {
  const m = { USD: '$', CNY: '¥', JPY: '¥', EUR: '€', GBP: '£', HKD: 'HK$', TWD: 'NT$' };
  return m[(cur || '').toUpperCase()] || (cur ? `${cur} ` : '');
}

export function pct(v, max) {
  if (!max || max <= 0) return 0;
  return Math.max(0, Math.min(100, (v / max) * 100));
}

export function highlightMatch(text, q) {
  const escape = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  if (!q) return escape(text);
  const lower = text.toLowerCase();
  const qLow = q.toLowerCase();
  let out = '', i = 0;
  while (i < text.length) {
    const idx = lower.indexOf(qLow, i);
    if (idx < 0) { out += escape(text.slice(i)); break; }
    out += escape(text.slice(i, idx));
    out += '<mark>' + escape(text.slice(idx, idx + q.length)) + '</mark>';
    i = idx + q.length;
  }
  return out;
}
