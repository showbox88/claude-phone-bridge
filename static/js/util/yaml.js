// Tiny YAML subset parser for ```checkin``` fenced blocks in chat mode.
// Supports: top-level key:value, indented sub-keys, quoted strings,
// inline arrays [a, b, c]. NOT a full YAML parser. Extracted from
// legacy app.js IIFE during Phase 4.

export function parseCheckinYaml(text) {
  const out = { selected_poi: null };
  let inPoi = false;
  for (const rawLn of text.split('\n')) {
    if (!rawLn.trim()) continue;
    const indented = /^[ \t]{2,}/.test(rawLn);
    const ln = rawLn.trim();
    const kv = ln.match(/^([a-z_]+):\s*(.*)$/i);
    if (!kv) continue;
    const key = kv[1].toLowerCase();
    let val = kv[2];
    if (indented) {
      if (!inPoi) continue;
      if (!out.selected_poi) out.selected_poi = {};
      out.selected_poi[key] = val;
      continue;
    }
    // Top-level
    if (key === 'selected_poi') {
      inPoi = true;
      out.selected_poi = {};
      continue;
    }
    inPoi = false;
    // List literal [a, b, ...]
    if (val.startsWith('[') && val.endsWith(']')) {
      val = val.slice(1, -1).split(',').map((s) => s.trim());
    }
    out[key] = val;
  }
  return out;
}
