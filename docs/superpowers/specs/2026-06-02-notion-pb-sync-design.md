# Notion ↔ PocketBase 同步设计

**日期:** 2026-06-02
**项目:** Phone Bridge
**作者讨论:** showbox88 + Claude

## 目标

- PB 是 Phone Bridge / Claude 的权威数据库(写入、查询、推理)。
- Notion 是用户的"驾驶舱":直观浏览、手动编辑、移动端友好。
- 两边内容保持一致;用户主要在 Notion 改/看,Claude 主要在 PB 写。
- 同步**不需要实时**:每天凌晨跑一次 cron + 手动触发 + Claude 可按需触发。
- 一切需要人工裁决的事情(冲突、删除、初次去重)都进**一张 Notion 表**,用户在 Notion 里就能选,不另做 UI。

## 非目标

- 不做实时 / webhook 双向推送。
- 不做自动冲突合并(谁赢谁输由用户裁决,不写规则去猜)。
- 不同步全部 12+ collection,只同步面向人的几个(下节列出)。
- 不为 Notion 付费版功能(webhook、SCIM 等)做依赖。

## 同步范围

**同步的(面向人):**
- `trips`
- `days`
- `plans`
- `todos`
- `contacts`
- `locations`

**不同步的(只在 PB,Claude 工作笔记):**
- `claude_memos`、`daily_briefing`、`transactions`、`journal`、`foods`、`ideas`、`pages`

范围可配置(见下文 `sync_config`),后续可一张张加/减。

## 架构

```
                  ┌────────────────────────────────────────┐
                  │  dashboard-server  (Tailscale)         │
                  │                                        │
                  │  ┌────────────┐   ┌─────────────────┐  │
                  │  │ PocketBase │   │ phone-bridge    │  │
                  │  │ 127.0.0.1: │◄──┤ FastAPI :8001   │  │
                  │  │ 8090       │   │ (Claude SDK)    │  │
                  │  └────────────┘   └─────────────────┘  │
                  │        ▲                  ▲            │
                  │        │                  │            │
                  │  ┌─────┴──────────────────┴─────────┐  │
                  │  │  notion_sync                     │  │
                  │  │  (Python module + systemd timer) │  │
                  │  │  • 03:00 daily cron              │  │
                  │  │  • on-demand: HTTP endpoint      │  │
                  │  │  • on-demand: pb_tools MCP tool  │  │
                  │  └────────────────┬─────────────────┘  │
                  └───────────────────┼────────────────────┘
                                      │ HTTPS
                                      ▼
                            ┌──────────────────┐
                            │  Notion API      │
                            │  (existing DBs)  │
                            └──────────────────┘
```

新模块 `notion_sync/` 在 phone-bridge 仓库内,跟 server.py 共享虚拟环境和 `.env`。
不引入新进程,用 systemd `OnCalendar=*-*-* 03:00:00` timer。

## 数据模型

### 1. 管线字段(每张同步表都加)

**PocketBase 端(已有 collection 通过 `pb_update_collection` patch):**
- `notion_id` (text) — 对端 Notion page id(管线字段,不是用户字段)
- `notion_last_edited` (datetime) — 上次同步看到的 Notion `last_edited_time`
- `last_synced_at` (datetime) — 本侧最后一次成功同步的时间戳
- 已有的 `updated` 字段(PB 自带)作为"PB 本地修改时间"

**Notion 端(每个同步 DB 都加):**
- `pb_id` (rich_text) — 对端 PB record id
- `last_synced_at` (date) — 同步时间戳

> 这些是管线字段,只有 sync 写入,用户不应手改。Notion 端在每个 DB 的默认视图里把它们设为隐藏列;PB 端没有"隐藏列"概念,只能靠命名(以 `notion_` 前缀)和约定。

### 2. Sync Activity(新增 Notion DB)

不在 PB 建。直接是一张顶层 Notion DB。**所有同步动作都进这里**(不只是需要裁决的),作为审计 + 待办二合一:大部分行是已自动应用的(灰色,不打扰你),只有需要你选的才是 Pending(高亮)。

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` | title | 自动生成,例 "trips · Notion → PB (出发时间)" / "todos · 冲突" |
| `op` | select | `Auto-applied` / `Conflict` / `Delete?` / `Possible duplicate` / `Schema mismatch` |
| `direction` | select | `Notion→PB` / `PB→Notion` / `Both` (冲突)/ `None` (待裁决) |
| `collection` | select | trips / days / plans / todos / contacts / locations |
| `record_link` | url | 跳到 Notion 对应记录(若存在) |
| `pb_id` | rich_text | |
| `notion_id` | rich_text | |
| `summary` | rich_text | 一句话,例 "departure_time: '10:00' → '11:00'" 或 "Notion='10:00' / PB='11:00'" |
| `notion_snapshot` | rich_text | JSON 快照(应用决定时用) |
| `pb_snapshot` | rich_text | JSON 快照 |
| `decision` | select | `Pending` / `Use Notion` / `Use PB` / `Delete both` / `Keep both` / `Merge` / `N/A` |
| `detected_at` | date | |
| `applied_at` | date | 同步实际应用的时间(自动 op 这一项立刻有,Pending 项是用户裁决后下一轮才有) |
| `notes` | rich_text | 用户可写,例:"merge 时把 PB 的 notes 字段保留" |

**默认视图过滤:**
- 主视图: `decision = Pending` — 你打开 Notion 看到的就是"还要你选什么"
- 历史视图: 全部,按 `detected_at` 降序 — 想看"今天/这周同步了啥"切到这里

**应用规则:**
- `op == Auto-applied` → 已经应用,`decision = N/A`,只是记录
- `decision == Pending` → 下次 cron 跳过这条
- `Use Notion` / `Use PB` → 把对应快照应用到另一边
- `Delete both` → 两边都硬删(若快照对应行还在)
- `Keep both` → 不动数据,只标 applied(本来误报)
- `Merge` → 这个分支留给未来,目前 sync 看到这个就忽略 + 在 Phone Bridge 提醒用户手动处理

**清理策略:** `op=Auto-applied AND applied_at < now - 30天` 的行,每周由 cron 自动清掉,避免 Notion 表无限膨胀。冲突/删除决定保留 90 天供回溯。

> **重点澄清(per 用户反馈):** 单边手动编辑 Notion 一行 → 不是冲突,是 `Auto-applied` 同步事件,直接同步到 PB,在 Sync Activity 留一行带 `decision=N/A` 的记录。只有**双方都改了同一字段**才进 Pending。

### 3. sync_config(新增 PB collection,per-collection 配置)

设计为 **per-collection 配置表**(不是单行),加新表 = 加一行,无代码改动。

```
sync_config (per-collection rows)
  ├─ collection:            text     例 "trips" — 同时是 PB collection 名
  ├─ notion_db_id:          text     对应 Notion DB id
  ├─ enabled:               bool     总开关,关掉不同步这张表
  ├─ field_map_overrides:   json     例 {"出发时间": "departure_time"}
  │                                  默认按 snake_case ↔ Notion 列名同名匹配,
  │                                  不一致的字段才需要在这覆盖
  ├─ last_synced_at:        datetime 本表最近一次同步的时间戳
  └─ last_sync_summary:     text     例 "5 auto-synced, 1 conflict queued"

sync_global (单行全局配置)
  ├─ timezone:        text     例 "America/New_York" 默认值;旅游时改成
  │                            "Asia/Tokyo" 等,立刻生效
  ├─ sync_hour_local: number   每日同步触发的本地小时数,默认 3 (= 03:00)
  ├─ paused:          bool     全局急停
  └─ last_run_at:     datetime cron 上次"考虑要不要跑"的时间戳
```

**加新表流程(零手术):**
```
1. 在 Notion 建好对应 DB(加 pb_id / last_synced_at 管线字段)
2. 调一次 pb_create("sync_config", {
     collection: "新表",
     notion_db_id: "abc-123",
     enabled: true,
     field_map_overrides: {}   # 大多数情况留空
   })
3. 下一轮 cron 自动开始同步,首次会做初次对齐(模糊匹配 + 入 Sync Activity)
```

**字段映射策略:**
- 默认行为:PB 字段名 ↔ Notion 列名,snake_case 自动转 Title Case 双向匹配
  (e.g. `departure_time` ↔ `Departure Time`)
- 类型映射(写在通用 codec 模块,不分表):
  - PB text/email/url ↔ Notion rich_text / email / url
  - PB number ↔ Notion number
  - PB bool ↔ Notion checkbox
  - PB date/datetime ↔ Notion date
  - PB select ↔ Notion select (枚举值要一致)
  - PB relation ↔ Notion relation (按对端 pb_id ↔ notion_id 解析)
- 不匹配的字段 → 在 `field_map_overrides` 显式声明,如 `{"出发时间": "departure_time"}`
- PB 有 / Notion 没有的字段 → sync 不动它,只在 PB 里存
- Notion 有 / PB 没有的字段 → 写一条 `Schema mismatch` 到 Sync Activity

## 同步流程

### 触发机制
- **systemd timer:** `OnCalendar=hourly`(每小时整点跑一次,固定 UTC)
- **守门逻辑(Python 里):**
  ```
  now_local = utcnow().astimezone(sync_global.timezone)
  if now_local.hour != sync_global.sync_hour_local:
      log.info(f"not sync hour ({now_local.hour}h != {sync_hour_local}h in {tz}), skipping")
      exit 0
  ```
  → 你旅游时改 `timezone` 从 `America/New_York` 到 `Asia/Tokyo`,下一个整点 cron 就会用东京时间判断。无须 reload systemd,无须改 timer 文件。

### 跑同步时的步骤

```
1. 读 sync_global + 所有 enabled sync_config 行
   ├─ 若 paused → 写日志退出
   └─ 否则继续

2. 处理 Sync Activity 里已决定的待办:
   for row in Sync Activity where decision != Pending AND decision != N/A AND applied_at is null:
     apply(row)            # 把 snapshot 应用到对应一边
     row.applied_at = now()
     row.op = "Auto-applied" if user-driven else keep current

3. 对每个 enabled sync_config 行:
   ├─ pb_rows  = pb.search(collection, filter='updated > last_synced_at OR notion_id = ""')
   ├─ notion_rows = notion.query(db, filter='last_edited_time > last_synced_at OR pb_id is empty')
   ├─ 按 (pb_id, notion_id) 对齐两边
   │
   ├─ 单边新建:
   │    ├─ Notion 有 pb_id 为空 → PB 创建,回填 pb_id 到 Notion
   │    │     → 写 Sync Activity (op=Auto-applied, direction=Notion→PB)
   │    └─ PB 有 notion_id 为空 → Notion 创建,回填 notion_id 到 PB
   │          → 写 Sync Activity (op=Auto-applied, direction=PB→Notion)
   │
   ├─ 单边修改:
   │    ├─ 只 PB updated > last_synced_at → 推到 Notion
   │    │     → 写 Sync Activity (op=Auto-applied, direction=PB→Notion)
   │    └─ 只 Notion last_edited > last_synced_at → 推到 PB  ← 用户手动改 Notion 走这里
   │          → 写 Sync Activity (op=Auto-applied, direction=Notion→PB)
   │
   ├─ 双边修改(真冲突):
   │    └─ 写 Sync Activity (op=Conflict, direction=None, decision=Pending),不动数据
   │
   ├─ 单边"消失"(对端通过映射 ID 找不到了):
   │    └─ 写 Sync Activity (op=Delete?, direction=None, decision=Pending),不动数据
   │
   └─ 更新本表 last_synced_at

4. sync_config 每行的 last_synced_at + last_sync_summary 更新

5. 若 Sync Activity 有新增 decision=Pending 行 → 调用 push.py 发推送通知
   标题: "同步待确认 N 项"
   正文: 列前 3 条 summary
   动作: 打开 Notion Sync Activity 的 URL(过滤 Pending 视图)

6. 每周日清理:删 op=Auto-applied AND applied_at < now-30d 的行;
   删 op∈{Conflict, Delete?, Possible duplicate} AND applied_at < now-90d 的行
```

## 初次对齐(PR1 一次性脚本)

现状:Notion 现有 DB ≈ 80% 跟 PB 一样,但**没有任何 `pb_id` 映射**,而且双方都各自加了一些新数据(PB 多了一些新行 + 新 collection;Notion 多了一个 trip,但 PB 里也有一样的影子)。

`scripts/reconcile_initial.py` 做一次性的"握手":

```
for each enabled_collection:
    pb_rows     = all PB rows
    notion_rows = all Notion rows in corresponding DB

    # 一阶段:确定匹配
    for nrow in notion_rows:
        match = best_match(nrow, pb_rows)  # 按 title + date 模糊匹配
        if match.score > 0.95 and unique:
            link(nrow, match.pb_row)         # 双向回填 ID
        elif match.score > 0.6:
            write_sync_queue(op="Possible duplicate", ...)   # 用户裁决
        # 否则视为 Notion-only,留到二阶段

    # 二阶段:差集处理
    pb_only      = PB 里没有 notion_id 的
    notion_only  = Notion 里 pb_id 还空着的

    for row in pb_only:
        create in Notion, link
    for row in notion_only:
        create in PB, link
```

跑完之后两边对齐,Sync Activity 里只剩"可能重复"的几条等用户在 Notion 里确认。

> 跑前自动备份 PB 到 `.bridge_data/backups/`。Notion 端无法 API 备份,所以**只读不删**,任何"应该删 Notion 行"的事都进 Sync Activity 等用户确认。

## Phone Bridge 集成

### 1. 推送通知 (Q7-a)
凌晨 cron 跑完,若 `Sync Activity` 有 `decision=Pending` 行 → 调 `push.py` 发推。
内容(合成示例):
```
📋 同步待确认 3 项
• trips · 出发时间冲突
• todos · 删除"买菜"?
• locations · 可能重复 "纽约"
[打开 Sync Activity]
```

### 2. MCP 工具(`pb_tools` 里加,或新增 `sync_tools.py`)
- `sync_now(collections=None)` — 立刻跑一次同步(替代等到凌晨)。
- `sync_queue_status()` — 返回 Sync Activity 里 Pending 数量 + 摘要。
- `sync_pause()` / `sync_resume()` — 急停开关。

这几个是 SAFE_TOOL_NAMES,Claude 调用不需要权限提示(读+幂等写)。

### 3. 不做的事
- 不在 chat 顶部加横幅(用户说"推送就够,以后要别的再加")
- 不在 Phone Bridge 里做冲突解决 UI(全在 Notion)

## 错误处理 & 鲁棒性

- **Notion API 限流**(3 req/s):同步器节流,每秒最多 2 个请求。
- **网络失败 / Notion 5xx**:整次 sync 视为失败,不更新 `last_synced_at` → 下一轮重试。**已经应用到一边的写入不回滚**,但因为 ID 映射存在,重试是幂等的(看到 ID 已存在 → 转 update 而非 create)。
- **PB 挂了**:sync 直接退出,日志告警。
- **快照过期**:若 Sync Activity 里某行决定时,对应记录已被第三方再次修改 → cron 应用前重新检测,若两边又不一致了 → 写一条"快照过期"提示,标这条为 resolved,新冲突重新入队。
- **日志**:`/home/dev/phone-bridge/.bridge_data/sync.log`,journald 也能看到。

## 渐进上线(3 个 PR)

| PR | 内容 | 上线信号 |
|---|---|---|
| **PR1** | 管线字段加到 PB + Notion;`reconcile_initial.py` 一次性脚本;`sync_config` collection;备份机制。**不跑 cron。** | 手动跑 reconcile 通过,两边 ID 都对齐,Sync Activity 里只剩用户能裁决的 possible-duplicate 几条。 |
| **PR2** | `notion_sync/` 模块 + systemd timer;**只走"无冲突路径"**(单边变更 + 新建);冲突/删除一律只写日志,**不**入 Sync Activity。 | 连续跑 7 天,日志里冲突 ≤ 2 次/周(验证用户对"冲突频率低"的判断)。 |
| **PR3** | Sync Activity 入队 + decision 应用 + push 通知 + MCP `sync_now` 工具。 | 整套闭环。 |

## 测试策略

- **单元测试**:
  - 匹配算法(title+date fuzzy match)用 fixtures 测各种 edge case
  - Notion ↔ PB 字段类型转换(date / select / relation)用 round-trip 测
  - Sync Activity 决定应用器单测,覆盖所有 6 个 decision 分支(`Pending` 是 no-op,其余 5 个各一个 case)
- **集成测试**:
  - 在 dev 环境跑个 mock PB + 用 Notion 测试 workspace,造场景:单边改、双边改、单边删、新建、重复
- **手动**:PR1 跑完后用户在 Notion 看一眼数据是否如期对齐。

## 开放问题(未来再说)

- **关系字段(relation)的同步**:trips → plans/contacts 这种关联在 Notion 是 relation 字段、在 PB 也是 relation。同步需要先同步被指向方,再同步指向方。MVP 假设两边 schema 已经对齐(因为之前迁移过),只搬运 ID 不重建关系。
- **附件 / 文件字段**:`pages` 表有文件,目前不在同步范围内。
- **`Merge` decision**:留接口不实现,真有需求时再做。
- **多语言字段**:不处理,假设单一语言。
- **schema 漂移检测**:用户在 Notion 加了一列、PB 不知道 → MVP 不处理,加进 Sync Activity 一条 `Schema mismatch` 提示用户手动调整。

## 验收标准

- ✅ 用户能在 Phone Bridge 跟 Claude 说"加个明天的行程",PB 写入,**当晚** Notion 里有了。
- ✅ 用户在 Notion 改某个 trip 的出发时间,**当晚** PB 里看到改动。
- ✅ 用户在 Notion 新建一行 trip,**当晚** PB 里有,且 ID 已绑定。
- ✅ 用户在 Notion 改 + Claude 在 PB 也改的同一条 → 不丢数据,Sync Activity 里出现一条 Pending,用户裁决后下一轮应用。
- ✅ 用户在 Notion 删一行 → 不直接删 PB,进 Sync Activity,用户确认后才删。
- ✅ 早上有待确认项 → 收到推送。
- ✅ 旅游时改 `sync_global.timezone` 到目的地时区,下一个整点 cron 用新时区判断,无需 restart 服务。
- ✅ 加新表只需:Notion 建 DB → `pb_create("sync_config", {...})` 一行 → 下轮 cron 自动同步,**零代码改动**。
- ✅ 单边手动改 Notion(没有 PB 端冲突)→ 自动同步到 PB,Sync Activity 留一条 `Auto-applied` 灰色记录,不打扰你。
