// Composer send pipeline: assemble the outgoing user_message frame, route
// slash commands, drive the check-in fenced-block format.
//
// `sendCurrent` is the single send entry point — bound to the send button
// click and to Enter in the textarea (wiring stays in legacy app.js until
// Task 15). It also handles the dual-role STOP behavior: when Claude is
// mid-response (isResponding=true), the send button cancels instead of
// sending.
//
// User messages are NOT rendered optimistically here. The server echoes
// back a `user_echo` frame which the ws message handler turns into an
// appendUser() call — that's what puts the bubble on screen. This matches
// legacy behavior verbatim (legacy app.js line 1017-1019).
//
// CLIENT_TZ is reported on every outgoing user_message so the agent can
// anchor relative times ("明天 3 点") to the user's actual local timezone.
//
// Extracted from legacy app.js (lines 7-12, 1069-1160) during Phase 4
// modularization. Behavior preserved verbatim except `send()` → `sendWs()`.

import { input } from '../dom.js';
import { get } from '../state.js';
import { sendWs } from '../ws/socket.js';
import {
  pendingAttachments, pendingFiles,
  clearAttachments, clearFiles,
} from './attachments.js';
import { autoresize, setResponding } from './input.js';

// Resolved once at module load — matches legacy IIFE initialization timing.
const CLIENT_TZ = (() => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; }
  catch (_) { return ''; }
})();

export function sendCurrent() {
  // If Claude is currently responding, the send button acts as STOP.
  if (get('isResponding')) {
    sendWs({ type: 'cmd', name: 'cancel' });
    return;
  }

  const text = (input && input.value || '').trim();
  if (!text && pendingAttachments.length === 0 && pendingFiles.length === 0) return;

  if (text === '/new') {
    sendWs({ type: 'cmd', name: 'new_session', mode: get('currentMode') });
  } else if (text === '/cancel') {
    sendWs({ type: 'cmd', name: 'cancel' });
  } else if (text.startsWith('/cwd ')) {
    const path = text.slice(5).trim();
    sendWs({ type: 'cmd', name: 'cwd', path });
  } else {
    const images = pendingAttachments.map((a) => a.path);
    const files = pendingFiles.slice();
    const ok = sendWs({ type: 'user_message', text, images, files, client_tz: CLIENT_TZ });
    if (!ok) return;
    clearAttachments();
    clearFiles();
    setResponding(true);  // optimistic; cleared by turn_done / error
  }
  if (input) input.value = '';
  autoresize();
}

// ---------- check-in FAB (Phase 2 Step 1: prompt-only minimum) ----------
// Composes a minimal ```checkin``` fenced block and sends it as a normal
// user_message. Server-side CHECKIN.md instructs Claude to parse + write
// PocketBase.
export function isoNowWithOffset() {
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

export function buildCheckinBlock(fields) {
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

export function sendCheckin(fields) {
  const block = buildCheckinBlock(fields);
  const ok = sendWs({ type: 'user_message', text: block, images: [], files: [], client_tz: CLIENT_TZ });
  if (!ok) return false;
  setResponding(true);
  return true;
}
