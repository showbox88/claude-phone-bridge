/// <reference path="../pb_data/types.d.ts" />

// system_alerts → push notification fan-out.
//
// Triggered after a Smart-Trip apiGuard write (rate-limit trip). Posts
// to phone-bridge's loopback /api/push/send which then calls
// push.send_to_all() to reach every VAPID subscriber.
//
// PB v0.38 hook caveats (see CLAUDE.md):
//  - each callback is its own goja VM; helpers must be inlined
//  - call e.next() so PB completes the save before the side-effect
//  - $http.send is synchronous; cap timeout to avoid blocking writes

onRecordCreate((e) => {
  e.next(); // persist row first

  const r = e.record;
  const apiType = r.get("api_type") || "?";
  const reason  = r.get("reason") || "?";
  const count   = r.get("count") || 0;

  const REASON_TEXT = {
    "disabled":    "管理员关闭",
    "daily_limit": "触发日限额",
    "2min_limit":  "触发 2 分钟限额",
  };
  const reasonText = REASON_TEXT[reason] || reason;
  const body = apiType + " 自动关闭（" + reasonText + "，实际 " + count + " 次）";

  try {
    const res = $http.send({
      url:    "http://127.0.0.1:8001/api/push/send",
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body:   JSON.stringify({
        title: "Google API 闸门触发",
        body:  body,
        tag:   "smart-trip-api-quota",
      }),
      timeout: 5,
    });
    if (res.statusCode >= 400) {
      console.log("[system_alerts hook] push failed status=" + res.statusCode + " body=" + (res.raw || "").slice(0, 200));
    }
  } catch (err) {
    console.log("[system_alerts hook] push exception: " + err);
  }
}, "system_alerts");
