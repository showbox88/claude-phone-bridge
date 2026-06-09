/**
 * 打卡 (check-in) feature: GPS + nearby POI search + 2-stage dialog.
 *
 * Stage 1: POI list (or manual entry). User picks/types a venue.
 * Stage 2: form (activity / amount / score / note) → buildCheckinBlock
 *   in composer/send.js produces the ```checkin``` fenced YAML block
 *   which gets prepended to the next user_message frame.
 *
 * GPS cache (localStorage bridge.lastGps, 30-minute TTL) survives offline;
 * fresh GPS request times out at 8s (or 10s when called by openCheckinDialog
 * for the background refresh) then falls through to cached coords.
 *
 * Ported verbatim from legacy app.js lines 1167-1483. The only structural
 * change: searchNearby now uses apiGet (was raw fetch + apiUrl) — apiGet
 * throws on non-2xx, which is caught by the existing try/catch so behavior
 * is identical (returns []).
 *
 * Note: stage-2 wiring (activity chips, score input, back button, submit
 * button) reads from the same DOM elements every time and is set up here
 * at module load — same as the legacy IIFE.
 */
import { apiGet } from '../api.js';
import { sendCheckin } from '../composer/send.js';
import {
  checkinDialog, cdStatus, cdList, cdManualName, cdManualGo,
  cdStageList, cdStageForm, cdBack, cdFormName, cdFormMeta,
  cdActivity, cdAmount, cdCurrency, cdScore, cdScoreVal, cdNote,
  cdBuildLoc, cdSubmit,
} from '../dom.js';

const GPS_CACHE_KEY = 'bridge.lastGps';
const GPS_CACHE_TTL_MS = 30 * 60 * 1000;

// Approximate FX rates → USD (rough Phase-2 defaults; user can edit later).
// Only used so the server-side hook can compute amount_usd. Field is omitted
// entirely if we can't guess.
const FX_TO_USD = {
  USD: 1, CNY: 0.14, JPY: 0.0064, EUR: 1.08, GBP: 1.27, HKD: 0.13, TWD: 0.031,
};

// Currently-staged selection (carried between Stage 1 → Stage 2).
let pendingSelection = null;

export function loadCachedGps() {
  try {
    const raw = localStorage.getItem(GPS_CACHE_KEY);
    if (!raw) return null;
    const o = JSON.parse(raw);
    if (!o || typeof o.lat !== 'number' || typeof o.lng !== 'number') return null;
    if (Date.now() - (o.t || 0) > GPS_CACHE_TTL_MS) return null;
    return o;
  } catch (_) { return null; }
}

export function saveCachedGps(lat, lng, accuracy_m) {
  try {
    localStorage.setItem(GPS_CACHE_KEY, JSON.stringify({
      lat, lng, accuracy_m, t: Date.now(),
    }));
  } catch (_) { /* quota or disabled — silent */ }
}

export function requestGps(timeoutMs = 8000) {
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

// POI picker dialog backed by /api/poi/around.
export async function searchNearby(lat, lng, radius = 300) {
  try {
    const data = await apiGet(`/api/poi/around?lat=${lat}&lng=${lng}&radius=${radius}`);
    return Array.isArray(data && data.pois) ? data.pois : [];
  } catch (_) {
    return [];
  }
}

export function resetCheckinDialog() {
  cdList.innerHTML = '';
  cdManualName.value = '';
  cdStatus.textContent = '正在定位…';
  cdStatus.className = 'cd-status';
  showStage('list');
  // Form fields
  cdActivity.querySelectorAll('button').forEach((b) => b.classList.remove('active'));
  cdAmount.value = '';
  cdCurrency.value = '';
  cdScore.value = '0';
  cdScoreVal.textContent = '—';
  cdNote.value = '';
  cdBuildLoc.checked = true;
  pendingSelection = null;
}

export function showStage(name) {
  cdStageList.classList.toggle('hidden', name !== 'list');
  cdStageForm.classList.toggle('hidden', name !== 'form');
}

export function enterFormStage(selection) {
  pendingSelection = selection;
  cdFormName.textContent = selection.name;
  const metaParts = [];
  if (selection.type) metaParts.push(selection.type);
  if (selection.city) metaParts.push(selection.city);
  if (selection.address) metaParts.push(selection.address);
  if (selection.distance_m != null) metaParts.unshift(`${selection.distance_m}m`);
  cdFormMeta.textContent = metaParts.join(' · ');
  showStage('form');
  // Focus the activity chips area on enter, but don't open mobile keyboard.
  setTimeout(() => cdSubmit.focus(), 0);
}

export function renderPoiList(pois, gps) {
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
      const selection = {
        name: p.name,
        source: 'poi',
        gps: gps ? { lat: gps.lat, lng: gps.lng, accuracy_m: gps.accuracy_m } : null,
        osm_id: p.osm_id || '',
        amap_poi_id: p.amap_poi_id || '',
        fsq_id: p.fsq_id || '',
        type: p.type || '',
        city: p.city || '',
        address: p.address || '',
        distance_m: p.distance_m,
      };
      enterFormStage(selection);
    });
    cdList.appendChild(row);
  }
}

export function stageManualEntry(gps) {
  const raw = cdManualName.value.trim();
  if (!raw) {
    cdManualName.focus();
    return;
  }
  enterFormStage({
    name: raw,
    source: 'manual',
    gps: gps ? { lat: gps.lat, lng: gps.lng, accuracy_m: gps.accuracy_m } : null,
    osm_id: '', amap_poi_id: '', fsq_id: '',
    type: '', city: '', address: '',
    distance_m: null,
  });
}

export async function openCheckinDialog() {
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

  // Wire manual-entry button to current GPS context (transitions to form stage).
  let currentGps = loadCachedGps();
  const onManualGo = () => stageManualEntry(currentGps);
  cdManualGo.onclick = onManualGo;
  cdManualName.onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); onManualGo(); }
  };

  // Tiny inline haversine — same formula as the server uses, in metres.
  const _distM = (a, b) => {
    const R = 6371000, toRad = (d) => d * Math.PI / 180;
    const dLat = toRad(b.lat - a.lat), dLng = toRad(b.lng - a.lng);
    const s = Math.sin(dLat / 2) ** 2
            + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(s));
  };

  // If we have a fresh-enough cached fix, show POIs immediately.
  if (currentGps) {
    cdStatus.textContent = `位置 (缓存) · acc ${currentGps.accuracy_m}m · 刷新中`;
    cdList.innerHTML = '<div class="cd-loading"><span class="cd-spinner"></span>查询附近 POI…</div>';
    const pois = await searchNearby(currentGps.lat, currentGps.lng);
    if (checkinDialog.open) renderPoiList(pois, currentGps);
  }

  // Get a fresh fix in the background.
  const fresh = await requestGps(10000);
  if (!checkinDialog.open) return;

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

  // If fresh is essentially the same spot, just update the status — keep
  // the already-rendered POIs to avoid the "list flashes twice" effect.
  if (currentGps && _distM(currentGps, fresh) < 30) {
    currentGps = fresh;     // adopt newer accuracy / timestamp for manual entry
    cdStatus.textContent = `位置 · acc ${fresh.accuracy_m}m`;
    cdStatus.className = 'cd-status ready';
    return;
  }

  // Moved meaningfully (or no cache to begin with) — re-query.
  currentGps = fresh;
  cdStatus.textContent = `位置 · acc ${fresh.accuracy_m}m`;
  cdStatus.className = 'cd-status ready';
  cdList.innerHTML = '<div class="cd-loading"><span class="cd-spinner"></span>查询附近 POI…</div>';
  const pois = await searchNearby(fresh.lat, fresh.lng);
  if (checkinDialog.open) renderPoiList(pois, fresh);
}

// ---------- Module-load wiring (mirrors legacy IIFE) ----------

// Dialog-close listener: clear contents so next open starts fresh.
if (checkinDialog) {
  checkinDialog.addEventListener('close', resetCheckinDialog);
}

// Activity-chip single-select
if (cdActivity) {
  cdActivity.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-val]');
    if (!btn) return;
    e.preventDefault();
    const active = btn.classList.contains('active');
    cdActivity.querySelectorAll('button').forEach((b) => b.classList.remove('active'));
    if (!active) btn.classList.add('active');
  });
}

if (cdScore) {
  cdScore.addEventListener('input', () => {
    const v = parseInt(cdScore.value, 10);
    cdScoreVal.textContent = v > 0 ? `${v}/10` : '—';
  });
}

if (cdBack) {
  cdBack.addEventListener('click', () => showStage('list'));
}

if (cdSubmit) {
  cdSubmit.addEventListener('click', () => {
    if (!pendingSelection) return;
    const fields = {
      name: pendingSelection.name,
      build_location: !!cdBuildLoc.checked,
    };
    if (pendingSelection.gps) {
      fields.gps = [pendingSelection.gps.lat, pendingSelection.gps.lng];
      if (pendingSelection.gps.accuracy_m != null) {
        fields.accuracy_m = pendingSelection.gps.accuracy_m;
      }
    }
    if (pendingSelection.osm_id)      fields.osm_id = pendingSelection.osm_id;
    if (pendingSelection.amap_poi_id) fields.amap_poi_id = pendingSelection.amap_poi_id;
    if (pendingSelection.type)        fields.type = pendingSelection.type;
    if (pendingSelection.city)        fields.city = pendingSelection.city;
    if (pendingSelection.address)     fields.address = pendingSelection.address;

    const activeChip = cdActivity.querySelector('button.active');
    if (activeChip) fields.activity_type = activeChip.dataset.val;
    const amountRaw = cdAmount.value.trim();
    if (amountRaw) {
      const amt = Number(amountRaw);
      if (Number.isFinite(amt) && amt >= 0) {
        fields.amount = amt;
        if (cdCurrency.value) {
          fields.currency = cdCurrency.value;
          const rate = FX_TO_USD[cdCurrency.value];
          if (rate != null) fields.rate = rate;
        }
      }
    }
    const scoreVal = parseInt(cdScore.value, 10);
    if (scoreVal > 0) fields.score = scoreVal;
    const noteRaw = cdNote.value.trim();
    if (noteRaw) fields.note = noteRaw;

    sendCheckin(fields);
    checkinDialog.close();
  });
}
