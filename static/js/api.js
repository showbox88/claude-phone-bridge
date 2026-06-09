/**
 * Unified fetch wrapper. Replaces 24 raw fetch() calls in legacy app.js.
 *
 * - apiGet/apiPost/apiPatch/apiPut/apiDelete: JSON-friendly wrappers
 * - apiPostForm: multipart upload (FormData; browser writes boundary)
 * - All throw ApiError on non-2xx
 * - apiUrl/wsUrl/assetUrl: resolve against currentSource (from state.js),
 *   preserving legacy semantics (apiUrl returns path as-is when no source;
 *   wsUrl returns null when no source)
 */
import { get } from './state.js';

export class ApiError extends Error {
  constructor(status, body, msg) {
    super(msg || `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

// Legacy verbatim: if no currentSource, return path as-is.
// Otherwise strip trailing slash from source.url and prepend.
export function apiUrl(path) {
  const currentSource = get('currentSource');
  if (!currentSource) return path;
  return currentSource.url.replace(/\/$/, '') + path;
}

// Legacy verbatim: if no currentSource, return null.
// Otherwise http→ws prefix replacement (covers https→wss), strip trailing
// slash, append /ws.
export function wsUrl() {
  const currentSource = get('currentSource');
  if (!currentSource) return null;
  return currentSource.url.replace(/^http/, 'ws').replace(/\/$/, '') + '/ws';
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

// Multipart upload (/api/upload). Browser writes the boundary header.
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
