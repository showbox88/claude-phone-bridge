# 时区感知的提醒 & 行程数据 — 设计

**日期**：2026-06-05
**背景**：未来要给 agent 加"明天3点提醒我 / 后天下午6点约餐厅"这类提醒功能。当前数据模型里 `trips / days / stops / todos / expenses / foods` 都没有时区字段，跨时区旅行会导致提醒和日期错乱。本 spec 把时区做进数据层，提醒功能的具体投递机制（push / Notion / phone-bridge）另文设计。

## 0. 目标

- 跨时区旅行时，"明天/后天 N 点"按**当地时间**触发，不按家里时间或服务器时间。
- 提醒可在出发前（在家时）就预先建好，落地后自动以目的地时区触发。
- 历史记录（哪天花了什么、哪天打了卡）按**当时所在地的日历日**归属。
- 现有数据不丢失语义，能批量 backfill。

## 1. 不做的事（YAGNI）

- 不存 `trips.timezone`。trip 的 tz 从其下 stops 反推；trip 完全没 stops 时退回手机当前 tz。
- 不建独立的 "tz_segments" 表。stops 按日期排序天然构成时区时间线。
- 不做夏令时跨界的特殊提示。IANA 名（`Europe/Paris`）本身就带 DST 规则，`zoneinfo` 自动处理。
- 不做跨 tz 提醒的"重复提醒/snooze"逻辑——只设计单次提醒触发的时间锚定。
- 不做 trip 创建时让用户手选 tz 下拉。
- 不在本 spec 内做提醒投递通道（push notification / 邮件 / Notion 提醒 / phone-bridge in-app 通知）——本 spec 只产出 `(due_at, due_tz)` 这对锚点字段，投递机制是下一篇 spec。

## 2. Schema 变更

### 2.1 `locations` —— 新增 `timezone`

```
timezone   text, max 64   // IANA 名，如 "Asia/Tokyo"；空 = 未推断
```

写入规则：
- 新增 location 且带 `lat/lng` → 用 `timezonefinder` 离线算一次，写入。
- 新增 location 无 lat/lng → 留空。后续 location 被 stop 引用且 stop 有 GPS 时，回写。
- lat/lng 之后变更 → **不**自动重算（tz 跟物理位置绑死，POI 不会跨时区漂移；如需修正手动 patch）。

### 2.2 `stops` —— 新增 `timezone`

```
timezone   text, max 64   // IANA；denormalized
```

写入时 fallback 链（writer 实现，PB 不强制）：

```
stop.timezone =
   1. stop.location.timezone                              （有 location 且 location 已有 tz）
   2. timezonefinder(stop.actual_lat, stop.actual_lng)    （没 location 但有 GPS）
   3. day.timezone                                        （继承当天）
   4. <留空，agent 写入时提示用户>
```

### 2.3 `days` —— 新增 `timezone`

```
timezone   text, max 64   // IANA；当天第一个 stop 进来时回写
```

写入规则：
- day 被 stop 引用时，若 `day.timezone` 为空 → 拿当前 stop 的 tz 写入。
- 同一 day 后续来的 stops 若 tz 不同（不太可能但理论上可以——例如真的跨时区飞行那一天），保留 `day.timezone` 为当天第一个 stop 的 tz，不强制对齐。

### 2.4 `todos` —— 新增 `due_at` + `due_tz`

```
due_at   date (PB datetime, UTC)   // 提醒触发时刻
due_tz   text, max 64              // 用户当时表达的 tz（IANA）
```

设计要点：
- `due_at` 是 **UTC datetime**，触发判断直接 `due_at <= now()`，PB index 友好。
- `due_tz` 保留用户**意图**（"东京下午6点"）。即便后续 trip 的城市改了、stop 的 tz 改了，已有 todo 的 tz 锁定不变。
- 显示时：把 `due_at` 转 `due_tz` 给用户看本地时间。
- Notion 同步：见 §5。

### 2.5 `expenses` / `foods` —— 新增 `timezone`

```
timezone   text, max 64   // IANA；写入时继承
```

写入规则（沿用现有 `expense.trip = expense.day.trip` 的 denormalization 思路）：

```
.timezone =
   1. parent stop.timezone
   2. parent day.timezone
   3. agent 当前推断（见 §3）
   4. <留空>
```

### 2.6 `trips` —— 不动

不加 `timezone` 字段。trip 在需要 tz 时按 §3 的规则反推或回退。

## 3. Agent 解析"明天3点"的算法

输入：
- `utterance_at`：消息抵达 agent 的时刻（UTC）
- `phone_tz`：前端上报的手机当前 IANA tz（从 `Intl.DateTimeFormat().resolvedOptions().timeZone`）
- `phrase`：用户原话，如"明天下午3点"

步骤：

```
1. anchor_date = (utterance_at 在 phone_tz 下的日期) + delta_days(phrase)
   例："明天" → +1；"后天" → +2；具体日期 → 直接用

2. local_time = parse_clock(phrase)  // "下午3点" → 15:00

3. target_tz =
     stops.where(date=anchor_date).order(reserved ASC).first().timezone
     OR days.where(date=anchor_date).first().timezone
     OR (trips covering anchor_date with stops) ? 该 trip 内 ≤ anchor_date 的最后一个 stop.timezone
     OR phone_tz

4. due_local_dt = datetime(anchor_date, local_time, tz=target_tz)
   due_at = due_local_dt.astimezone(UTC)

5. 写入 todos: { due_at, due_tz: target_tz, ... }
```

边界情况：
- anchor_date 那天既无 stop 也无 day 也不在任何 trip 内 → `target_tz = phone_tz`。
- 用户在巴黎（phone_tz=Europe/Paris），后天那天的 stop 在东京（stop.timezone=Asia/Tokyo） → 用 Asia/Tokyo，按东京日期 + 15:00 算 UTC。
- 用户原话明确带 tz（"东京时间下午3点"）→ 覆盖算法，直接用用户指定的。

## 4. 写入时 fallback 链（写 stop / expense / food / todo 时如何填 timezone）

统一封装成 writer-side helper（伪代码）：

```python
def resolve_tz(*, stop=None, day=None, lat=None, lng=None,
               phone_tz=None, anchor_date=None) -> str | None:
    if stop and stop.timezone:
        return stop.timezone
    if lat is not None and lng is not None:
        return tzfinder.timezone_at(lng=lng, lat=lat)
    if day and day.timezone:
        return day.timezone
    if anchor_date:
        # 找 anchor_date 那天 / 那段 trip 的 stops
        s = find_stop_for_date(anchor_date)
        if s and s.timezone:
            return s.timezone
    return phone_tz   # 可能仍为 None
```

所有 mutator（`pb_create_stop` / agent 写 expense / Gmail importer 等）都走这个 helper。

## 5. Notion 同步

- **datetime 字段（如 `todos.due_at`、`stops.checkin`）**：
  - PB 存 UTC。
  - 同步到 Notion 时渲染为 ISO 8601 带 offset：`2026-06-08T18:00:00+09:00`。Notion 的 date property 接受这个格式，渲染时显示成 "Jun 8, 2026 6:00 PM"。
  - offset 由 `(utc_value, iana_tz)` 在同步时刻算：`utc_value.astimezone(ZoneInfo(iana_tz)).isoformat()`。
- **`timezone` / `due_tz` 字段（IANA 字符串）**：
  - 同步成 Notion 的 text 列。原样存，方便用户在 Notion 表格里直接看到"哪个 tz"。
- 反向同步（Notion → PB）：
  - 用户在 Notion 直接编辑 datetime → Notion 返回带 offset 的 ISO 串 → PB 存 UTC + 同时按 offset 反查 IANA 名写入 tz 字段（多数情况能匹配；匹配不到时保留旧的 IANA）。
  - 用户在 Notion 直接改 `timezone` 文字（如改成 `Asia/Seoul`） → 同步回 PB 时**不**重算 datetime（这是改"标签"，不是改时刻；如果用户想改时刻应直接改 datetime 列）。

## 6. 现有数据 backfill

按顺序跑一次性脚本（每个独立、可重跑）：

1. **`scripts/backfill_location_timezones.py`**
   - 对所有 `locations` where `lat != ''` and `lng != ''` and `timezone == ''`
   - 调 `timezonefinder.timezone_at(lng=lng, lat=lat)` 写入
   - 输出统计：成功 N / 跳过（无 GPS）M

2. **`scripts/backfill_stop_timezones.py`**
   - 对所有 `stops` where `timezone == ''`
   - 走 §4 的 helper：location → GPS → day → 留空
   - 同时回写 `days.timezone`（若该 day 还为空）

3. **`scripts/backfill_child_timezones.py`**
   - 对 `expenses` / `foods` where `timezone == ''`
   - 走 stop → day → 留空
   - 不动 `todos`：todos 不加 `timezone` 字段（创建时 tz 价值有限），历史 todos 也不存在 `due_at` 需要 backfill。

跑不出来的（POI 无 GPS、历史 stops 无 location 无 GPS）→ 留空。后续被 agent 引用时再补。

## 7. 依赖与运行时

- **Python 库**：`timezonefinder`（离线，自带~15MB 多边形数据；调用即查，无外部 API）。加入 `requirements.txt`。
- **Python 标准库**：`zoneinfo`（Py 3.9+，处理 IANA tz 计算），已有。
- **前端**：phone-bridge 的 chat 前端在每条用户消息附带 `client_tz: Intl.DateTimeFormat().resolvedOptions().timeZone`，server 透传给 agent。
- **MCP 工具**：`pb_create_stop` / `pb_create_expense` 等加可选 `client_tz` 参数；不传时各字段的 tz 留空，由后续写入或脚本补。

## 8. 同步注册表（sync_config）影响

- 新增的 5 个 `timezone` 文本字段：宽度 64，作为普通 text 列同步。`sync-registry-design.md` 里的 PB→Notion 类型映射对 text 已覆盖，无需改 codec。
- `todos.due_at` 是新 datetime 字段：按现有 datetime 同步规则走（pipeline 不动）。
- `todos.due_tz` 是新 text 字段。

每个新字段都需要：
- 加进对应 collection 的 PB migration（详见实施计划 PR1）。
- 在同步设置 UI 重新拉取 schema，让 provisioner 把字段在 Notion DB 一侧也加上（或在 migration 同步阶段一次性补齐）。

## 9. 不在本 spec 内的后续工作

- **提醒投递机制**：触发到 `due_at <= now()` 后，怎么 push 给用户（in-app WebSocket、APNs、Notion 提醒、邮件）。
- **重复提醒 / snooze**：今天先做单次。
- **跨 tz 飞行那一天的展示**："2026-06-08 在 Tokyo (起飞) → Seoul (落地)"这种叙事化呈现。
- **trip 多城市的 UI 总览**（"这趟去了 4 个时区"）。

## 10. 验收

- 在 phone-bridge 里跟 agent 说"明天下午3点提醒我"，且自己手机 tz 是 `America/Los_Angeles` → `todos.due_at` 应为 (今天日期+1) 15:00 LA → UTC（即 22:00 / 23:00 UTC 取决于 DST），`due_tz='America/Los_Angeles'`。
- 假装人在巴黎（前端 client_tz=Europe/Paris），且某 stop 在那天指向 `Asia/Tokyo` → 同样的话产出的 due_at 是东京 15:00 对应的 UTC。
- backfill 后，跑 `SELECT count(*) FROM locations WHERE lat!='' AND timezone=''` 应为 0（除非 lat/lng 落在公海或 timezonefinder 解析失败）。
- Notion 上 `todos.due_at` 列显示的时间，等于 `due_at` 在 `due_tz` 下的本地时间。
