# 📍 打卡（Check-in）处理规则

当用户消息含一个 ```checkin``` fenced code block 时，按以下流程把数据写入本地 PocketBase。其他消息照常对话——别误触发。

---

## 一、输入格式

打卡消息长这样（字段都可选，可能缺）：

```checkin
when: 2026-05-27 18:30
gps: [39.9088, 116.4117]
accuracy_m: 15
selected_poi:
  name: 星巴克(国贸店)
  osm_id: node/123456789
  amap_poi_id: B0FFFEFLBR
  type: 咖啡馆
  city: 北京
  address: 朝阳区国贸三期 1F
build_location: true
activity_type: 用餐
amount: 38
currency: CNY
rate: 0.14
score: 8
note: WiFi慢
```

---

## 二、鉴权

PocketBase 在本机 `$PB_URL` (= http://127.0.0.1:8090)，token 在 `$PB_TOKEN` 环境变量（server.py 已自动 refresh 每 30 分钟）。

**所有 curl 模板**：

```bash
curl -sS -H "Authorization: $PB_TOKEN" -H "Content-Type: application/json" "$PB_URL/api/..."
```

如果遇到 401，报告给用户停止重试。

---

## 三、5 步处理流程

### Step 1：dedup — 找现有 Location 或新建

**如果 build_location == false**：完全跳过这一步，Day.location 留空（街边买水、一次性消费）。

**如果 build_location == true**：

1. 优先用 `osm_id` 查：
   ```bash
   curl -sS -H "Authorization: $PB_TOKEN" "$PB_URL/api/collections/locations/records?filter=(osm_id='node/123456789')&perPage=1"
   ```
2. 若返回 `totalItems: 0`，再用 `amap_poi_id` 查
3. 两个都查不到？最后用 `name` + 100m GPS 半径查模糊匹配（避免重名）
4. 都找不到 → POST 新建：
   ```bash
   curl -sS -X POST -H "Authorization: $PB_TOKEN" -H "Content-Type: application/json"      "$PB_URL/api/collections/locations/records"      -d '{"name":"<name>","lat":<gps0>,"lng":<gps1>,"osm_id":"<osm>","amap_poi_id":"<amap>","type":"<type>","city":"<city>","address":"<addr>","visited":true}'
   ```
5. 命中则复用其 `id`，且把 `visited=true`（PATCH 更新）

**记下 `location_id`** 供 Step 3 用。

---

### Step 2：自动归拢 Trip（按日期）

查今天落在哪个 trip 区间内：

```bash
TODAY=$(date +%Y-%m-%d)
curl -sS -H "Authorization: $PB_TOKEN" "$PB_URL/api/collections/trips/records?filter=(date_start<='$TODAY' %26%26 date_end>='$TODAY')&perPage=1"
```

- 命中 → 拿到 trip_id
- 无命中 → trip_id 留空（散落 Stop，回家建 Trip 时会自动归）

---

### Step 3：upsert Day（容器）+ 新建 Stop（事件）

> **重要**：2026-06-03 stops redesign 之后，`days` 表只剩"日级"容器字段
> （name / date / weather / note / content / trip）。打卡的所有"事件
> 信息"（金额、评分、类型、坐标、checkin 时间）都搬到了 `stops`。
> 一个真实的日历日 = 1 个 day 行 + N 个 stop 行。

**3a. Upsert Day（找今天的容器；没有就建一个）**

```bash
TODAY=$(date +%Y-%m-%d)
DAY_ID=$(curl -sS -G -H "Authorization: $PB_TOKEN" \
  --data-urlencode "filter=(trip='$TRIP_ID' && date='$TODAY')" \
  --data-urlencode "perPage=1" \
  "$PB_URL/api/collections/days/records" | jq -r '.items[0].id // empty')

if [ -z "$DAY_ID" ]; then
  DAY_ID=$(curl -sS -X POST -H "Authorization: $PB_TOKEN" -H "Content-Type: application/json" \
    "$PB_URL/api/collections/days/records" \
    -d '{
      "name": "<行程名 Day N 或 location.city + 日期>",
      "date": "<YYYY-MM-DD>",
      "trip": "<trip_id 或空字符串>"
    }' | jq -r '.id')
fi
```

- 同一行程的同一天只该有一个 Day 行；filter 先查再建避免重复
- 散落打卡（trip 空）：filter 改为 `date='$TODAY' && trip=''`

**3b. 创建 Stop（具体事件）**

```bash
curl -sS -X POST -H "Authorization: $PB_TOKEN" -H "Content-Type: application/json" \
  "$PB_URL/api/collections/stops/records" \
  -d '{
    "name": "<事件描述，默认用 location.name 或 activity 标签>",
    "date": "<YYYY-MM-DD>",
    "checkin": "<when 完整 ISO-8601 时间戳>",
    "categories": ["<打卡|酒店|餐厅|购物|体验|交通|笔记|消费 中的一个或多个>"],
    "amount": <数字或省略>,
    "currency": "<JPY|EUR|USD|CNY|其他 或省略>",
    "rate": <数字或省略>,
    "note": "<短评>",
    "day": "<day_id from 3a>",
    "trip": "<trip_id 或空字符串>",
    "location": "<location_id 或空字符串>",
    "actual_lat": <gps[0]>,
    "actual_lng": <gps[1]>
  }'
```

**字段映射**：旧 YAML 的 `activity_type` 现在翻成 `categories` 数组里的一
个或多个值；用餐 → `["餐厅", "消费"]`，纯逛景点 → `["打卡"]`，住酒店
→ `["酒店", "消费"]`。旧 `score`（0-10 数字）不再存在 stop 上，转写到
Step 4 的 location.rating（已经是这个流程）。

- `amount_usd`：stops 表的 `amount_usd` 字段，目前是给前端 derived 用的；
  你 POST 时可以不传，或者算好传上去（`amount * rate`），二者都行
- `categories` 是 multi-select，至少给一个值
- 字段省略时不要写 `null` 字符串（PB 会当文字"null"）——直接 omit key

---

### Step 4：评分回写 Location（2026-05-27 决策 A）

如果 score 有值（1-10）且 location_id 不空：

| score | rating select |
|---|---|
| 1-2 | ⭐ |
| 3-4 | ⭐⭐ |
| 5-6 | ⭐⭐⭐ |
| 7-8 | ⭐⭐⭐⭐ |
| 9-10 | ⭐⭐⭐⭐⭐ |

```bash
curl -sS -X PATCH -H "Authorization: $PB_TOKEN" -H "Content-Type: application/json"   "$PB_URL/api/collections/locations/records/<location_id>"   -d '{"rating":"⭐⭐⭐⭐"}'
```

**语义**：Location.rating = "对这家店的当前印象"，每次打卡最新评分覆盖（per 用户决策 A）。

---

### Step 5：反馈给用户

成功：
```
✅ 打卡 📍 <店名>
   Day《<day_name>》· Stop <categories[0]> · ¥38 · ⭐⭐⭐⭐
   {若有} 关联 Trip《<trip_title>》
```

失败（任意 curl 非 2xx）：
```
❌ 打卡失败 (step <N>): <HTTP code> <错误摘要>
```
**不要静默继续**。

---

### Step 6：确保同步被触发

**优先路径(自动)**：用 MCP 工具 `mcp__pb__pb_create` / `pb_update` 写
PB 时，pb_tools 会自动 schedule 一次 10 秒 debounce 后的 sync，无需
手动触发。这一次打卡(location + day + stop)的三次 pb 写入会合并
成每个 collection 一次 `--only X` 的后台 sync，~30 秒内 Notion 全部可见。

**fallback(手动)**：如果你走的是 Step 1-3 里的原始 curl 路径（没用
MCP `pb_create`，所以 pb_tools 的自动 hook 不会触发），Step 5 反馈完后
**追加一步**：

通过 MCP：调 `sync_now`（不带 `collection` 参数，locations / days /
stops 全推）。或 shell：

```bash
cd /home/dev/phone-bridge && set -a; . ./.env; set +a; \
  .venv/bin/python -m notion_sync.runner --force-now
```

sync 失败不影响 Step 5 的反馈——PB 已经落地。告诉用户"已写 PB，但
Notion 同步失败：<error>，下一次定时会重试"即可，**不要**回滚 PB。

---

## 四、底线规则

1. **写 PB 完后必须立刻 sync 到 Notion**（Step 6）。原 "不写 Notion" 的设计
   是为了解耦——保留这个解耦原则（写 PB 时不直接调 Notion API），但作为
   Step 6 顺手触发独立的 sync runner，让用户立刻在 Notion 看到。单次打卡
   = 1 次 PB 写入 + 1 次 sync runner 触发。
2. **不动 Foods / Journal**：除非用户在同一消息里明确要记菜或日记，否则只动 trips / locations / days / **stops**。
3. **"买水" 模式**：build_location: false 时绝不创建 Location。直接写 Stop（location 字段空），day 仍然要 upsert（散落到 trip='' && date='today' 的 day 上）。
4. **不重试 401**：token 失效就报告给用户。server.py 后台 refresh 周期 30 分钟，下次自然恢复。
5. **必填字段缺失**：name 字段缺失时用 location.name 或 activity_type 兜底，永远不要 POST 一个没有 name 的 Day。
6. **`$PB_URL` 和 `$PB_TOKEN` 是 env 变量**：直接在 bash 命令里用 `$VAR` 引用，**不要硬编码 URL 或 token 字符串**。

---

## 五、PocketBase Filter 语法速记

PB 用类 SQL 表达式 + URL encode：

| Notion-like | PB filter |
|---|---|
| field = "x" | `filter=(field='x')` |
| field LIKE "%x%" | `filter=(field~'x')` |
| AND | `%26%26` (URL encoded `&&`) |
| OR | `%7C%7C` (URL encoded `||`) |
| date <= today | `filter=(date<='2026-05-27')` |

记得 URL encode 整个 filter 值，或直接用 `-G --data-urlencode "filter=..."`。

