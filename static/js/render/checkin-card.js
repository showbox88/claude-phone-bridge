// Compact card rendered in place of a ```checkin``` fenced YAML block in
// user messages. Schema is documented in CHECKIN.md.
//
// Extracted from legacy app.js IIFE during Phase 4 modularization.

import { currencySymbol, scoreStars } from '../util/format.js';

export function fmtCheckinTime(when) {
  if (!when) return '';
  const d = new Date(when);
  if (isNaN(d.getTime())) return when;
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n) => String(n).padStart(2, '0');
  const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (sameDay) return `今天 ${hhmm}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hhmm}`;
}

export function renderCheckinCard(data, rawYaml) {
  const card = document.createElement('div');
  card.className = 'checkin-card';

  const poi = data.selected_poi || {};
  const name = poi.name || data.name || '(未命名)';
  const when = fmtCheckinTime(data.when);
  const accuracy = data.accuracy_m;
  const type = poi.type;
  const city = poi.city;
  const address = poi.address;
  const activity = data.activity_type;
  const amount = data.amount;
  const currency = data.currency;
  const score = data.score;
  const note = data.note;

  const pinIcon = (window.icon && window.icon('pin', 18)) || '📍';

  const subParts = [];
  if (type) subParts.push(type);
  if (city) subParts.push(city);
  if (address) subParts.push(address);
  const sub = subParts.join(' · ');

  const detailParts = [];
  if (activity) detailParts.push(activity);
  if (amount) detailParts.push(`${currencySymbol(currency)}${amount}`);
  if (score) {
    const stars = scoreStars(score);
    detailParts.push(stars ? `${stars} ${score}/10` : `${score}/10`);
  }
  const detail = detailParts.join(' · ');

  card.innerHTML = `
    <div class="cc-row cc-main">
      <span class="cc-pin"></span>
      <div class="cc-body">
        <div class="cc-name"></div>
        ${sub ? '<div class="cc-sub"></div>' : ''}
      </div>
      <span class="cc-time"></span>
    </div>
    ${detail ? '<div class="cc-detail"></div>' : ''}
    ${note ? '<div class="cc-note"></div>' : ''}
    <details class="cc-raw">
      <summary>查看原始 YAML</summary>
      <pre></pre>
    </details>
  `;
  card.querySelector('.cc-pin').innerHTML = pinIcon;
  card.querySelector('.cc-name').textContent = name;
  if (sub) card.querySelector('.cc-sub').textContent = sub;
  card.querySelector('.cc-time').textContent = when;
  if (detail) card.querySelector('.cc-detail').textContent = detail;
  if (note) card.querySelector('.cc-note').textContent = '"' + note + '"';
  card.querySelector('.cc-raw pre').textContent = rawYaml;
  if (accuracy) card.title = `定位精度 ±${accuracy}m`;
  return card;
}
