# Smart Note 打卡：Phase 2 计划书

> 写于 2026-05-27 末，Phase 1（后端）当晚刚上线。明天去 office 电脑拉这个 branch 继续。

---

## Phase 1 已完成（背景）

- 后端：PocketBase v0.38.2 跑在 dashboard-server，5 个 collections（trips / locations / days / foods / journal）schema 1:1 翻译自 Notion 同名库 + 打卡新字段（locations.{lat,lng,osm_id,amap_poi_id}、days.{actual_lat,actual_lng}）
- 鉴权：`server.py` 启动自动取 PB token 入环境变量 `$PB_TOKEN`，30 分钟 refresh
- 打卡入口：手机 PWA 发送 ` ```checkin ` fenced YAML block → Claude SDK 读 `CHECKIN.md` → curl 写 PocketBase
- 端到端已通过手敲 YAML 块测试

详见 [`CHECKIN.md`](./CHECKIN.md) 和 [`pocketbase/README.md`](./pocketbase/README.md)。

---

## Phase 2 目标

**让打卡变成手机上"按一下"的事**——不用手敲 YAML 块。前端的 modal 收集 GPS + POI + 字段，生成 fenced block，自动发到聊天。所有后端流程不动。

### 验收标准

1. 手机 PWA 聊天界面右下角有一个 📍 FAB 按钮
2. 点击 → modal 弹出，0.5-3 秒内显示当前位置 + 附近 POI 候选列表
3. 用户选店（或手填）+ 填字段（消费 / 评分 / 短评 / 类型 / 是否建 Location）+ 点发送
4. 一条 markdown 消息（含 fenced `checkin` block）自动注入聊天流，Claude 当作普通消息处理
5. 聊天历史里这条消息**不显示原始 YAML**，而是渲染成漂亮卡片（marked 自定义 renderer）
6. 离线时（飞机 / 地铁）可以暂存 IndexedDB，联网自动 POST（Service Worker Background Sync）

---

## 文件改动清单

涉及的全部文件都在 `static/` 下：

| 文件 | 改动量 | 干什么 |
|---|---|---|
| `static/index.html` | 中（~30 行新增）| 加 FAB 按钮 + 隐藏 `<dialog class="checkin-modal">` 表单 |
| `static/app.js` | 大（~300 行新增）| FAB 点击逻辑 + geolocation + POI 查询（Overpass + 高德）+ 表单提交 + marked 自定义 renderer + IndexedDB 离线队列 |
| `static/style.css` | 中（~100 行新增）| `.fab-checkin`、`.checkin-modal`、`.checkin-card` 三套样式 |
| `static/sw.js` | 小（~40 行新增）| Background Sync handler |
| `static/poi.js` | 新文件（~150 行）| 抽出 POI 查询逻辑：Overpass + 高德 双源 + dedup + 按距离排序 |
| `static/checkin-render.js` | 新文件（~50 行）| marked 的 `code(lang="checkin")` 自定义 renderer |

`server.py` 在 Phase 2 **完全不需要改动**——打卡消息走现有 WS 通道，Claude SDK 看到 fenced block 后自然按 CHECKIN.md 处理。

---

## 实施顺序（按依赖排，从最简单试到最完整）

### Step 1：最丑能用的 FAB 按钮（30 分钟）
- index.html 加固定定位的 `<button class="fab-checkin">📍</button>`
- app.js 加 click handler：弹一个原生 `prompt("店名:")` → 拼一个最小 fenced block → 走现有 sendMessage 路径
- 验收：手机上点按钮 → 输入店名 → Claude 收到并照常处理

### Step 2：geolocation 接入（30 分钟）
- 替换 prompt 为 `navigator.geolocation.watchPosition({enableHighAccuracy:true})`
- 缓存 last-known 到 localStorage，按按钮时**先用旧的渲染**新的回来再 refresh
- 验收：fenced block 里有 `gps: [lat, lng]` + `accuracy_m: <m>`

### Step 3：POI 查询（半天）
- 新建 `static/poi.js`，封装：
  - Overpass API: `[out:json][timeout:5]; (node[~"amenity|shop|tourism"="."](around:200,$lat,$lng);); out body 10;`
  - 高德 Web API: 用 `/place/around` endpoint
  - 并发两个 fetch，超时 3 秒，合并去重（按 name + 距离 < 30m 视为同一）
  - 排序：距离升序 + 高德结果优先（中文显示更友好）
- modal UI 显示 top 5 候选，每条可点选；最后一项固定是 "手填" → 弹文本框
- 验收：modal 显示真实附近店，点选后 fenced block 含 `selected_poi.osm_id` 或 `amap_poi_id`

⚠️ 需要先去高德开放平台 [console.amap.com](https://console.amap.com/) 申请 Web API key（免费 5000/天）。

**更安全的做法**：高德 POI 查询走 `server.py` 代理（新增 `/api/poi/around?lat=&lng=` endpoint），key 放 `.env` 永远不出后端。前端 fetch 这个本地代理。多一个 endpoint 但 key 不会泄漏。

### Step 4：表单字段（1 小时）
- modal 里加：
  - 是否建 Location（toggle，默认 ON）
  - activity_type（select：用餐 / 购物 / 休息 / 交通 / ...）
  - amount（number）+ currency（select：CNY / USD / JPY / EUR / 其他）
  - rate（number，可空，CNY 默认 0.14、JPY 0.0064 等）
  - score（0-10 slider）
  - note（textarea）
- 提交按钮 → 拼成完整 YAML fenced block → 走 sendMessage

### Step 5：marked 自定义 renderer（30 分钟）
- `static/checkin-render.js`：
  ```js
  marked.use({ renderer: { code(code, lang) {
    if (lang !== 'checkin') return false;
    const data = parseYaml(code);  // 手写极简 YAML parser
    return `<div class="checkin-card">📍 ${data.selected_poi?.name || data.name} ...</div>`;
  }}});
  ```
- style.css 加 `.checkin-card` 样式
- 验收：聊天历史里之前发的打卡块自动显示成卡片，不见 raw YAML

### Step 6：Service Worker Background Sync（1 小时）
- sw.js 注册 `sync` event handler
- app.js 提交时优先走 fetch；网络失败 → 存 IndexedDB → 注册 sync tag
- 上线后 sw 自动 fetch 重发
- 验收：开飞行模式点打卡，落地连上 wifi 后自动出现 Claude 的回复

---

## 跳过的事 / 留到 Phase 3

- Notion 单向 sync（PocketBase → Notion）—— 不打卡 critical path，等需要时单独写
- 自动归拢 Trip 的"回填"逻辑（建新 Trip 时把覆盖日期范围的旧 stops 自动归入）—— 写个一次性脚本即可
- PocketBase 数据迁移到 US Oracle ARM（Phase 1 决策的"路线 A"）—— 等 Oracle 到手 `scp data.db` 15 分钟搞定
- 自建 UI 取代 Notion 浏览—— 远期，PocketBase admin UI 暂时够用

---

## 风险 / 已知坑

1. **iOS Safari Geolocation 慢启动**：watchPosition 必须 user gesture 触发。Android Chrome 同样需要。
2. **高德 API 出海外不工作**：海外打卡只能靠 Overpass。出国期间双源策略很关键。
3. **PB v0.38 JS hook 坑**：详见 `pocketbase/README.md` 末尾的告警。
4. **Tailscale 必须保持在线**：PWA 必须从 tailnet 内访问。出国时手机要保持 Tailscale 在线。如果断网 + 打卡 → 走 Background Sync 队列等回连。

---

## 给明天自己的话

去 office 电脑后第一件事：
```
git clone https://github.com/showbox88/claude-phone-bridge
cd claude-phone-bridge
git checkout smart-note-pocketbase
# 读完这份 PHASE2_PLAN.md + CHECKIN.md + pocketbase/README.md
# 从 Step 1（FAB 按钮 prompt 兜底版）开始
```

如果 ssh-key 没配 github、用 HTTPS clone 也行，公开 repo 不需要 auth 就能 read。

打卡这个东西要早用早攒经验。Step 1-2（半小时）就能让"按按钮"代替"敲 YAML"，剩下的可以分批做。
