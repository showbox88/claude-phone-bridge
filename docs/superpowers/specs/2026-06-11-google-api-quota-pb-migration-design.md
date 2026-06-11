# Google API 限额闸门：迁回 PocketBase + 4 通道告警

**Date:** 2026-06-11
**Status:** Design approved, pending implementation plan
**Repos touched:** `Smart-Trip` (frontend), `phone-bridge` (PB hooks + push endpoint reuse)

## 1. 背景

### 痛点
Smart-Trip 的 Google API 限额闸门（`src/utils/apiGuard.js`）是一次事件复盘后的产物 —— 当时一个 bug 让某条调用路径 runaway，2 天没人发现，烧了不小的 Google API 账单。复盘后增加了 apiGuard：每类 API 独立开关 + 日/2 分钟限额 + 超限自动关。

但在 2026-06 切换 `VITE_DATA_SOURCE=pb`（从 Supabase 迁到 PocketBase）后，apiGuard 和 AdminPage 的存储（`system_settings` / `api_logs` 两张表）仍在 Supabase 里，PB 模式下 Supabase URL 是 placeholder，**整套限额系统悄悄失效了**：
- `checkApiAllowed` 在 PB 模式下要么 throw 被吞、要么 supabase client 返回 undefined → fail-open → **Google API 调用不再被限额**
- AdminPage 打开就是空数据，开关无法关
- 这一切的可观察症状：开发者意识到时 = 出事时

### 目标
- 把 `system_settings` 和 `api_logs` **1:1 搬到 PocketBase**，闸门重新生效（事件复盘后的限额值 / 层级 / 自动关行为**全部保持不变**）
- 在 4 个 surface 上告警，**让"2 天才发现"不再可能**：
  1. PB `system_alerts` 行（数据层 source of truth）
  2. Navbar 铃铛（始终可见的主入口，跨页生效）
  3. AdminPage 顶部红色横幅（admin 专属备份）
  4. Phone Bridge 手机推送（离机通知 —— 即使不开 SmartTrip 也能收到）

### 非目标
- **不**改限额阈值（200/日、20/2 分钟、auto-disable per-API）—— 那是事件复盘后定的，不动
- **不**改 apiGuard 强度模式（仍为浏览器内软闸；硬闸要 server.js 代理 + GCP key restriction，这次不做）
- **不**接 Google Cloud Monitoring API（"真实账单 quota"留作日后；现在以应用内计数器为唯一闸门）
- **不**迁旧 Supabase 数据（用户决定从零开始）
- **不**为 Smart-Trip 引入新的 push notification SDK；复用 phone-bridge 已有 `/api/push`

## 2. PocketBase Schema

### 2.1 `system_settings`
键值对，apiGuard 配置中心。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `key` | text | UNIQUE | 配置键名 |
| `value` | text | — | 字符串值，调用方解析 |

种子（migration 用 `INSERT OR IGNORE` 实现 idempotent）：

| key | 默认值 | 含义 |
|---|---|---|
| `places_search_enabled` | `'true'` | Places Text/Nearby Search 开关 |
| `place_details_enabled` | `'true'` | Place Details 开关 |
| `directions_enabled` | `'true'` | Directions / Routes 开关 |
| `daily_api_limit` | `'200'` | 单 API 类型日上限（全局，所有用户共享）|
| `per_2min_api_limit` | `'20'` | 单 API 类型 2 分钟 burst 上限 |

API rules：`""`（任何登录用户读写）—— 跟 trips/stops 等业务表保持一致。

### 2.2 `api_logs`
每次 API 调用一行。

| 字段 | 类型 | 索引 | 说明 |
|---|---|---|---|
| `api_type` | text | — | `places_search` / `place_details` / `directions` |
| `user_id` | text | — | 调用者 PB user.id；当前限额是全局，留作未来 per-user 拆分 |
| `status` | text | — | `success` / `blocked` |
| `created` | autodate | composite index `(api_type, status, created)` | PB 系统字段，count 主键 |

### 2.3 `system_alerts`（新）
闸门触发记录 + 告警状态。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `kind` | text | — | 目前唯一值 `api_disabled`；预留扩展位 |
| `api_type` | text | — | `places_search` / `place_details` / `directions` |
| `reason` | text | — | `disabled` / `daily_limit` / `2min_limit`（与 apiGuard return reason 对应）|
| `count` | number | 0 | 触发时刻的实际计数（dailyCount 或 recentCount）|
| `acknowledged` | bool | false | 用户在 UI 上点"已读"后置 true |
| `created` | autodate | — | 系统字段 |

API rules：`""`（任何登录用户读写）。

## 3. 代码改动清单

### 3.1 Smart-Trip 仓库

#### `src/utils/apiGuard.js`（替换全文，函数签名不变）

逻辑等价于原 Supabase 版本，只换底层 + 触发分支加 `createAlert`：

```js
import { pb } from '../lib/pb';

export async function logApiCall(apiType, userId, status = 'success') {
  try {
    await pb.collection('api_logs').create({
      api_type: apiType,
      user_id: userId || '',
      status,
    });
  } catch (e) {
    console.warn('[apiGuard] logApiCall failed (non-fatal):', e?.message);
  }
}

export async function checkApiAllowed(apiType, userId) {
  let map = {};
  try {
    const settings = await pb.collection('system_settings').getFullList({
      filter: `key="${apiType}_enabled" || key="daily_api_limit" || key="per_2min_api_limit"`,
    });
    settings.forEach(s => { map[s.key] = s.value; });
  } catch (e) {
    // fail-open：PB 不可达时允许调用（与原 Supabase 版同语义；闸门 ≠ app 可用性的硬依赖）
    console.warn('[apiGuard] settings read failed, fail-open:', e?.message);
    return { allowed: true, reason: '' };
  }

  if (map[`${apiType}_enabled`] === 'false') return { allowed: false, reason: 'disabled' };

  const dailyLimit   = Number(map.daily_api_limit ?? 200);
  const per2minLimit = Number(map.per_2min_api_limit ?? 20);

  const startOfDay = new Date(); startOfDay.setHours(0,0,0,0);
  const twoMinAgo  = new Date(Date.now() - 2*60*1000);
  const pbTime = (d) => d.toISOString().replace('T',' ');

  const [dailyRes, recentRes] = await Promise.all([
    pb.collection('api_logs').getList(1, 1, {
      filter: `api_type="${apiType}" && status="success" && created>="${pbTime(startOfDay)}"`,
    }),
    pb.collection('api_logs').getList(1, 1, {
      filter: `api_type="${apiType}" && status="success" && created>="${pbTime(twoMinAgo)}"`,
    }),
  ]);
  const dailyCount  = dailyRes.totalItems;
  const recentCount = recentRes.totalItems;

  if (dailyCount >= dailyLimit) {
    await flipSwitch(`${apiType}_enabled`, 'false');
    await createAlert({ kind: 'api_disabled', api_type: apiType, reason: 'daily_limit', count: dailyCount });
    await logApiCall(apiType, userId, 'blocked');
    return { allowed: false, reason: 'daily_limit' };
  }
  if (recentCount >= per2minLimit) {
    await flipSwitch(`${apiType}_enabled`, 'false');
    await createAlert({ kind: 'api_disabled', api_type: apiType, reason: '2min_limit', count: recentCount });
    await logApiCall(apiType, userId, 'blocked');
    return { allowed: false, reason: '2min_limit' };
  }
  return { allowed: true, reason: '' };
}

async function flipSwitch(key, value) {
  try {
    const rec = await pb.collection('system_settings').getFirstListItem(`key="${key}"`);
    await pb.collection('system_settings').update(rec.id, { value });
  } catch (e) {
    console.warn(`[apiGuard] flip ${key} failed:`, e?.message);
  }
}

async function createAlert(payload) {
  try {
    await pb.collection('system_alerts').create({ ...payload, acknowledged: false });
  } catch (e) {
    console.warn('[apiGuard] createAlert failed (non-fatal):', e?.message);
    // 即使 alert 写入失败也不阻断闸门已经关上的事实；下次 guard check 仍会 block
  }
}
```

注入风险：`apiType` 是 12 个 caller 写死的常量（`'places_search'` / `'place_details'` / `'directions'`），filter 字符串拼接安全。

#### `src/hooks/useSystemAlerts.js`（新增）

集中订阅 PB system_alerts，给 Navbar + AdminPage 共用：

```js
import { useEffect, useState } from 'react';
import { pb } from '../lib/pb';

export function useSystemAlerts() {
  const [alerts, setAlerts] = useState([]);
  const [unackCount, setUnackCount] = useState(0);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const list = await pb.collection('system_alerts').getList(1, 20, {
          sort: '-created',
        });
        if (stop) return;
        setAlerts(list.items);
        setUnackCount(list.items.filter(a => !a.acknowledged).length);
      } catch (e) {
        // 静默：PB 不可达时不刷新，不抛错
      }
    };
    tick();
    const t = setInterval(tick, 30_000); // 30 秒 poll；后续可升级 PB realtime
    return () => { stop = true; clearInterval(t); };
  }, []);

  const markAck = async (id) => {
    try {
      await pb.collection('system_alerts').update(id, { acknowledged: true });
      setAlerts(a => a.map(x => x.id === id ? { ...x, acknowledged: true } : x));
      setUnackCount(c => Math.max(0, c - 1));
    } catch (e) {
      console.warn('[useSystemAlerts] markAck failed:', e?.message);
    }
  };

  const markAllAck = async () => {
    const unack = alerts.filter(a => !a.acknowledged);
    try {
      await Promise.all(unack.map(a => pb.collection('system_alerts').update(a.id, { acknowledged: true })));
      setAlerts(a => a.map(x => ({ ...x, acknowledged: true })));
      setUnackCount(0);
    } catch (e) {
      console.warn('[useSystemAlerts] markAllAck failed:', e?.message);
    }
  };

  return { alerts, unackCount, markAck, markAllAck };
}
```

**Poll vs Realtime**：当前用 30s setInterval；PB v0.38 支持 SSE 订阅 (`pb.collection().subscribe('*', cb)`)，更优雅但需要处理重连/取消。若铃铛响应慢成痛点，作为后续升级。

#### `src/components/layout/Navbar.jsx`（接铃铛）

当前：
```jsx
<button className="nav-icon-btn">
  <span className="material-symbols-outlined">notifications</span>
  <span className="nav-dot"></span>
</button>
```

改造：
- 引入 `useSystemAlerts()` hook
- `nav-dot` 在 `unackCount > 0` 时显示，内容 = count（>9 显示 `9+`）
- `onClick` 切换 `bellOpen` state
- 弹出 dropdown：列出 alerts，未读置顶；每行 `[×]` 标记已读；底部 `[全部标记已读]`
- 视觉风格沿用现有 `user-dropdown` 那一套（相同 z-index、定位、暗色 surface）

dropdown 单条 alert 样式：
```
⚠️ places_search 自动关闭
   原因: 触发 2 分钟限额（实际 20 次）
   2 分钟前                                [×]
```

#### `src/pages/AdminPage.jsx`（双部分）

**Part A**：把 API 监控段（line 282–356，4 个函数）从 supabase 改成 pb：

- `loadApiData()`：`pb.collection('system_settings').getFullList()` + 3 个并行 `getList(1,1)` 取 count + 1 个 `getList(1,50)` 取 recent logs
- `toggleApiSwitch(key, currentValue)`：先 `getFirstListItem(\`key="${key}"\`)` 再 `update(id, { value })`
- `saveLimits()`：同上模式
- `toggleAllApis(enable)`：3 个并行 update（如果某个 key 不存在则 create —— upsert 模式）

**Part B**：顶部加红色横幅，用 `useSystemAlerts()`：
- `unackCount > 0` 时渲染 banner（红底白字 + 警告图标）
- 显示最近 1 条 alert 全文 + `[查看全部]` 按钮（展开下方滚动列表）+ `[全部标记已读]`
- banner 沿用现有 AdminPage 的页面宽度，置于内容区顶部

### 3.2 phone-bridge 仓库

#### `pocketbase/pb_migrations/<unix_ts>_create_api_quota_tables.js`（新增 migration）

按照 `1779465616_create_sync_meta.js` 的格式，在一个 migration 文件里建 3 个 collection：`system_settings`、`api_logs`、`system_alerts`。

包括：
- 字段定义如 §2 所示
- 索引：`api_logs (api_type, status, created)` composite
- API rules 全部 `""`
- 种子数据：5 行 `system_settings`（INSERT OR IGNORE）

#### `pocketbase/pb_hooks/system_alerts.pb.js`（新增 hook）

参考现有 `days.pb.js` 风格（goja VM、helper 内联、无顶层共享函数、链式 `e.next()`）：

```js
/// <reference path="../pb_data/types.d.ts" />

onRecordCreate((e) => {
  e.next(); // 先让 PB 完成保存
  // 保存成功后做 push 副作用（hook 第二参 'system_alerts' 已限定 collection）

  const r = e.record;
  const apiType = r.get('api_type') || '?';
  const reason  = r.get('reason') || '?';
  const count   = r.get('count') || 0;
  const reasonText = {
    'disabled': '管理员关闭',
    'daily_limit': '触发日限额',
    '2min_limit': '触发 2 分钟限额',
  }[reason] || reason;

  const body = `${apiType} 自动关闭（${reasonText}，实际 ${count} 次）`;

  // POST phone-bridge /api/push（同机 loopback）
  try {
    const res = $http.send({
      url: 'http://127.0.0.1:8001/api/push',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: 'Google API 闸门触发',
        body,
        tag: 'smart-trip-api-quota',
      }),
      timeout: 5,
    });
    if (res.statusCode >= 400) {
      console.log('[system_alerts hook] push failed status=' + res.statusCode);
    }
  } catch (err) {
    console.log('[system_alerts hook] push exception: ' + err);
  }
}, 'system_alerts');
```

注意事项（PB v0.38 hook 坑，CLAUDE.md 已记）：
- 每个 callback 在独立 goja VM；helper 必须内联，不能抽顶层函数复用
- 调用 `e.next()` 让 PB 完成保存后再做副作用；如果不调，PB 不会写入
- `$http.send` 设 `timeout: 5` 避免推送端卡住阻塞 PB 写入流程

**实现时第一步必须验证**：在 implementation plan 里先读 `pocketbase/pb_data/types.d.ts` 确认 `onRecordCreate` / `$http.send` 在当前 PB 版本 (v0.38.2) 的精确签名，再落代码。

#### Phone Bridge `/api/push` 端点

复用现有实现 —— 当前 session 摘要显示已存在 `app/api/push.py`，按现有约定调用即可。如果该端点需要 auth header / 特定字段名，**implementation plan 第一步显式验证 payload schema 后再写 hook**。

## 4. 数据流总图

```
浏览器 (Smart-Trip)                                    PocketBase 127.0.0.1:8090
─────────────────────                                  ──────────────────────────
某个 hook 调 Google API
  ├─→ apiGuard.checkApiAllowed(type, userId)  ────→  GET system_settings (filter)
  │                                            ←──── settings rows
  │                                            ────→  GET api_logs?filter=count_today
  │                                            ←──── totalItems
  │                                            ────→  GET api_logs?filter=count_2min
  │                                            ←──── totalItems
  │
  │   (超限分支)
  ├─→ flipSwitch('${type}_enabled', 'false')  ────→  PATCH system_settings/<id>
  ├─→ createAlert({kind,api_type,reason,count}) ──→  POST system_alerts
  │                                                       │
  │                                              hook 触发 ▼
  │                                                   $http.send POST
  │                                                   http://127.0.0.1:8001/api/push
  │                                                       │
  │                                                       ▼
  │                                                   Phone Bridge → 推送到用户手机
  └─→ logApiCall(type, userId, 'blocked')      ────→  POST api_logs

Navbar useSystemAlerts() 每 30s tick     ────→  GET system_alerts (sort=-created, page=1, perPage=20)
                                          ←──── 列表
点 [×] / [全部已读]                       ────→  PATCH system_alerts/<id> { acknowledged: true }
```

## 5. 部署 & 回滚

### 部署顺序
1. **phone-bridge** 先 deploy：新 PB migration 创建 3 个 collection 并种子化；新 `pb_hooks/system_alerts.pb.js` 装上
2. **Smart-Trip** 再 deploy：新 apiGuard.js + useSystemAlerts.js + Navbar/AdminPage 改动 → `npm run build:pb-vm` → scp dist
3. 验证：
   - PB admin UI 看到 3 个新 collection + 5 行 system_settings 种子
   - Smart-Trip 加载后 Network 面板看 `system_alerts` 30s 一次 GET（poll 心跳）
   - **人造触发测试**：在 PB admin UI 手工 INSERT 一行 `system_alerts {kind:'api_disabled',api_type:'places_search',reason:'2min_limit',count:21,acknowledged:false}` → 应该 30 秒内：
     - Navbar 铃铛出红点+数字
     - AdminPage 顶部出红色横幅
     - 手机收到推送通知

### 回滚
- Smart-Trip：`rm -rf /home/dev/smat-trip/dist && mv dist.bak.<ts> dist`（已有现成机制）
- PB hooks：`sudo rm /opt/pocketbase/pb_hooks/system_alerts.pb.js && sudo systemctl restart pocketbase`
- 3 个 collection 不必删除（保留就是空数据 + 种子，对其他系统无害）

## 6. Bedrock 建议（不在代码 scope，但请务必同步做）

应用层闸门是软闸 —— bug 跑出 apiGuard 包装外、或 PB 故障 fail-open 时仍可能烧钱。最后一道防线在 Google Cloud Console：

1. **Billing budget alert（最关键）**：
   - GCP 控制台 → Billing → Budgets & alerts
   - 设当月预算 e.g. $20，触发 50% / 90% / 100% 邮件报警（发到 showbox88@gmail.com）
2. **API quota override（硬上限）**：
   - GCP 控制台 → APIs & Services → 选 Maps Platform 下的每个 API → Quotas
   - 把日上限改成你能承受的硬数字（例如 Places 500/day）—— Google 直接 503，bug 也烧不动
3. **API key restriction**（防 key 外泄）：
   - GCP 控制台 → Credentials → 选 Maps key → Application restrictions
   - 选 HTTP referrers → 加 `https://dashboard-server.tail4cfa2.ts.net:8451/*`
   - 别人偷到 key 也调不了

这三步只要做一次，长效保险。

## 7. 验证清单（implementation 后逐项打勾）

- [ ] PB 里 3 个 collection schema 跟 §2 完全一致
- [ ] PB 里 5 行 `system_settings` 种子存在且值正确
- [ ] Smart-Trip 启动后 console 无 `apiGuard ... failed` warning
- [ ] 任一 hook 调 Google API：成功路径 → `api_logs` 多一行 `status=success`
- [ ] 人造连续 21 次 places_search 调用 → 第 21 次返回 `blocked`，PB 里：
  - `system_settings.places_search_enabled` = `'false'`
  - 新增 1 行 `system_alerts {reason:'2min_limit',count:21}`
- [ ] 30 秒内 Navbar 铃铛红点亮 + 显示 `1`
- [ ] 30 秒内 AdminPage 顶部出现红色横幅
- [ ] 手机收到 Phone Bridge 推送
- [ ] AdminPage 点开关恢复 → `system_settings.places_search_enabled` 回 `'true'`，下次 guard 调用恢复
- [ ] 铃铛 dropdown 点 [×] → `acknowledged=true`，红点消失
- [ ] PB 重启后所有数据持久化（验证 collection 不是临时）

## 8. 风险与权衡

| 风险 | 缓解 |
|---|---|
| Poll 30s 不够实时 | 急的场景手机推送会立刻到；铃铛是兜底视觉 |
| PB 故障 → fail-open | 跟原 Supabase 同语义；GCP 硬 quota 是真正兜底 |
| 推送 hook 阻塞 PB 写入 | `$http.send` 设 5s timeout；hook 用 `onRecordAfterCreateRequest` 是 fire-and-forget |
| 多 tab 并发把 alert 写重 | apiGuard 不去重；每次触发 = 一行 system_alerts。可接受（每分钟只可能触发几次）|
| 种子值 INSERT OR IGNORE 后再改默认值需手动 | 文档化；migration 里只做 first-time seed，后续值修改通过 AdminPage |
