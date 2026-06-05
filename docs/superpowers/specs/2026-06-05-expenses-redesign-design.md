# Expenses Redesign — Design Spec

**日期:** 2026-06-05
**项目:** Phone Bridge
**作者讨论:** showbox88 + Claude

## 目标

把现有 `transactions` 表重塑为 `expenses`，让它成为 `stops` 的子表（一对多）。统一所有"花钱"的入口——无论是旅行消费还是日常消费——都存到 `expenses` 里；`stops` 不再直接持有金额字段。

最终能在一处查到：
- 每日 / 每周 / 每月 / 年报开销
- 按 expense_category 的分类统计
- 按日的开销热力图
- 某 trip 的总消费及分类
- 旅游 vs 日常消费对比（trip is null 与否）

## 非目标

- 不实现 Notion 端的 rollup / formula（如有需要后续单独 PR）
- 不改 Gmail 自动抓取（当前不是全自动，agent 触发，后续 PR2 调整 prompt 即可）
- 不引入"预算 / 余额"概念
- 不改 currency 转换的算法（保持 amount × rate = amount_usd，rate 由 agent / UI 填）

## 背景与现状

### `transactions`（当前）

```
transactions {
  description (text, required, max 500)
  amount      (number)              // 默认 USD
  date        (date)
  type        select(1) [支出, 退款]
  category    select(1) [旅行, 订阅服务, 娱乐, 交通, 购物/日用, 餐饮]
  card        select(1) [Chase Sapphire Preferred (7675)]
  confirmation text                   // Gmail 收据 dedup key（unique-when-non-empty）
  source      select(1) [手动, Gmail]
  created/updated (autodate)
}
indexes:
  idx_tx_date, idx_tx_category
  UNIQUE idx_tx_confirmation WHERE confirmation != ''
```

PB-only（不在 sync_config 里），存 11 行旧数据。

### `stops`（当前）

```
stops {
  ...
  amount      (number)
  currency    select(1) [JPY, EUR, USD, CNY, 其他]
  rate        (number)
  amount_usd  (number)
  categories  select(maxSelect=8) [打卡, 酒店, 餐厅, 购物, 体验, 交通, 笔记, 消费]
  ...
}
```

`stops.categories` 是"事件性质"标签（"餐厅"暗示有 location 类型）；不是"开销分类"。

### `days.trip`（当前）

`relation→trips (single)` — 现在是必填关系。

### 关键观察

1. **一次 visit 可有多笔花销**：进公园 = 门票 + 冰淇淋 + 水。如果只在 stop 上挂 amount，要么强制建多个一样的 stop（丑），要么舍弃细分（信息丢失）。所以必须 stop 一对多 expense。
2. **日常消费没有 trip**：买杯咖啡，不在任何 trip 里。但仍然属于某个 `day`（日历日）容器。所以 `days.trip` 应为 optional。
3. **外币不只发生在旅行**：日常也会撞上日亚、EUR 订阅、Steam 海外结算。currency/rate/amount_usd 不能只绑给"旅行 expense"，必须每条 expense 都能填，但允许空（USD 行）。
4. **stops.categories vs expense_category 是不同维度**：前者是"做了什么"（事件类型），后者是"钱花在哪种用途"。强行合并会污染语义。

## 数据模型（目标）

### 1. `expenses`（由 `transactions` 重命名）

```
expenses {
  id                    text
  description           text required, max 500     // 原 transactions.description
  amount                number                     // 原币种金额
  currency              select(1) [USD, JPY, EUR, CNY, 其他]   // 默认 USD
  rate                  number                     // 1 unit foreign ≈ N USD（USD 行可空）
  amount_usd            number                     // 写入侧自动算（USD: = amount；其它: amount × rate）
  date                  date
  type                  select(1) [支出, 退款]
  expense_category      select(1) [旅行, 订阅服务, 娱乐, 交通, 购物/日用, 餐饮, 门票, 住宿, 代付, 其他]
                                                   // 由 transactions.category 改名 + 扩值（旅行场景新增 + 代付）
  card                  select(1) [Chase Sapphire Preferred (7675)]   // 维持原值列表，将来扩
  confirmation          text                       // Gmail dedup key，unique-when-non-empty
  source                select(1) [手动, Gmail, Agent]
                                                   // 新增 Agent 值（Phone Bridge / MCP 工具写入）

  stop                  relation→stops    (single, optional)
  day                   relation→days     (single, optional)
  trip                  relation→trips    (single, optional)   // denormalized convenience

  notion_id             text                       // 管线字段（新增，因为要接同步）
  notion_last_edited    date
  last_synced_at        date
  created               autodate (onCreate)
  updated               autodate (onCreate+onUpdate)
}

indexes:
  CREATE INDEX  idx_expenses_date           ON expenses (date)
  CREATE INDEX  idx_expenses_category       ON expenses (expense_category)
  CREATE INDEX  idx_expenses_stop           ON expenses (stop)
  CREATE INDEX  idx_expenses_day            ON expenses (day)
  CREATE INDEX  idx_expenses_trip           ON expenses (trip)
  CREATE UNIQUE INDEX idx_expenses_confirmation ON expenses (confirmation) WHERE confirmation != ''
  CREATE UNIQUE INDEX idx_expenses_notion_id    ON expenses (notion_id) WHERE notion_id != ''
```

`expense_category` 候选值变更说明：
- 保留：旅行 / 订阅服务 / 娱乐 / 交通 / 购物/日用 / 餐饮
- 新增：门票（景点/演出/活动票）、住宿（酒店/民宿）、**代付**（替他人垫付，例如"代付 Monica Sheng" 一类）、其他
- 旧数据按"原值 →（同名）"映射；不会有数据丢失
- 旧 4 笔 Amazon "代付 Monica" transactions 迁移时由脚本检测 `description LIKE '%代付%'` → 自动改 `expense_category = '代付'`

### 2. `stops`（瘦身）

**移除**字段：`amount`、`currency`、`rate`、`amount_usd`（已迁移到 expenses 之后才能 drop）。
`categories` 保留不动。其它字段都不动。

### 3. `days`（约束放宽）

`days.trip` 由 required relation → **optional** relation。其它字段不动。

## 关键约定

### A. amount_usd 写入侧自动算

写 expense 时按以下规则填 amount_usd：
- `currency == 'USD'` 且 amount_usd 为空 → 自动填 = amount
- `currency != 'USD'` 且 rate > 0 → 自动填 = amount × rate
- `currency != 'USD'` 且 rate 为空 → amount_usd 保留为空（等用户/agent 后补）

实现位置：MCP `pb_create` / `pb_update` 包装层 + 前端 UI + agent prompt。
PB 本身不强制（不写 PB hook，保持简单）。

### B. 退款强制存负数

`type='退款'` 的 expense **amount 必须 < 0**，amount_usd 也对应为负。
查询时 `sum(amount_usd)` 自然得到净支出，不再做 CASE WHEN type 分支。

实现位置：MCP / 前端 / agent 写入时校验；老数据迁移时把 type='退款' 行的 amount 翻成负数（如果已经是正数）。

### C. expense.trip 冗余字段（同 stops 现行做法）

写入侧保证：`expense.trip == expense.day.trip`（若 day 有 trip）。
- 创建 expense 时，agent / MCP / UI 必须根据 day 自动设置 trip
- 更新 day.trip 时，UI / MCP 需要级联更新该 day 下所有 expense.trip（同 stops 的现行约束）

短期：靠写入侧自觉。长期可加 PB hook，不在本次 PR 范围。

### D. relation 仍然不双向同步

延续现有 sync 系统的限制（见 docs/data-model.md §8.1）：expense.stop / expense.day / expense.trip 关系不会被 sync 推到 Notion。Notion 端会有这三个 relation 列但留空。等未来 relation-sync PR 统一解决。

## 迁移策略

### 现有数据（必须保留）

PB 当前规模：
- `transactions`: **11 行**（含 4 笔 Amazon 代付 Monica + 早晚餐 + 加油 + Uber Eats 等）
- `stops`: **68 行**（含今天的 4 笔测试，note 里自带 "测试数据，迟点删除"；这些 note 必须完整保留以便手动清理）

迁移规则：

**a. transactions → expenses（改名 + 字段扩展）**

迁移脚本步骤（migration JS 内，前置条件：days.trip 已先改为 optional，见下方 d）：
1. PB 不支持直接 rename collection，所以分两步：
   - 创建新 collection `expenses`（schema 见上）
   - 对每条 `transactions` 行 tx：
     - 字段直接映射：description / amount / date / type / card / confirmation / source → 原样
     - `category` → `expense_category`（同名）；若 `description LIKE '%代付%'` 则覆盖为 '代付'
     - `currency` → 默认 'USD'（旧数据全是 USD）
     - `rate` → 空
     - `amount_usd` → = amount
     - **day 回填**（脚本自动）：
       1. `day = days.findFirst(date = tx.date)`；若存在，取它
       2. 若不存在 → `trip_match = trips.findFirst(date_start <= tx.date <= date_end)`；
          建一条新 day：`{ name: tx.date (即 'YYYY-MM-DD'), date: tx.date, trip: trip_match }`
       3. `expense.day = day.id`
     - **trip 回填**：`expense.trip = day.trip`（若有）
     - `stop` → 空（无法回溯到具体 stop）
     - notion_id / notion_last_edited / last_synced_at → 空
   - 删除旧 `transactions` collection
2. 退款数据校验：迁移时若 `type='退款'` 且 amount > 0，自动翻负

**b. stops 的 amount/currency/rate/amount_usd → 新 expenses 行**

为每个 `stops.amount > 0` 的行新建一条 expense：
- description = stops.name + 若有 note 则加 " · " + note 前 N 字（截断到 max 500）
- amount = stops.amount
- currency = stops.currency（或 'USD' 默认）
- rate = stops.rate
- amount_usd = stops.amount_usd
- date = stops.date
- type = '支出'（旧 stops 没有退款语义）
- expense_category：由 stops.categories 推断 → 见下表
- source = '手动'（旧数据来源未知，保守标记）
- stop = stops.id
- day = stops.day
- trip = stops.trip
- card / confirmation = 空

categories → expense_category 推断表（取 stops.categories 第一个匹配的）：

| stops.categories 含 | expense_category |
|---|---|
| 餐厅 | 餐饮 |
| 酒店 | 住宿 |
| 交通 | 交通 |
| 购物 | 购物/日用 |
| 体验 | 娱乐 |
| 打卡（无其它） | 门票 |
| 笔记 / 消费（无其它指向） | 其他 |

无匹配 → '其他'。

**c. stops 移除金额字段**

迁移最后一步（确认 b 已成功后）：drop stops.amount / currency / rate / amount_usd。

**d. days.trip 改 optional**

直接 update collection schema，把 `days.trip` 的 required 改 false。已有 day 行不动。

### 测试数据保留确认

今天（2026-06-05）的 4 笔测试 stop（id: `jz7w7xmn6qtelz0` 坐火车, `5c2u8bv4sb1os7v` 冰淇淋, `ooib2vje4194rju` winic tech, `6q1kvi3qk2f1bqw` Ross Business Systems）：
- 它们的 note 字段已包含"测试数据，迟点删除"字样 → 迁移脚本不动 note
- 其中"坐火车"(amount=60 CNY) 和"冰淇淋"(amount=25 CNY) 会按规则 b 生成对应的 expense 行
- 用户后续可按 note 关键字 grep stop + grep expense.description（迁移会把 stop.note 内容嵌入 description）清理

### 回滚

每个 migration JS 必须实现 `down`：
- 删除新 `expenses` collection
- 恢复 `transactions` collection（从 backup snapshot）
- 恢复 stops 的 4 个金额字段（从 backup snapshot）
- days.trip 改回 required

**强制在执行迁移前**：`scripts/backup_collections.py` 或 `notion_sync/backup.py` 创建一份完整 PB snapshot 到 `.bridge_data/backups/<ts>/`。

## 同步接入

复用现有 sync registry 机制（CLAUDE.md "Sync registry"）：
1. 等迁移落地后，前端 → 同步设置 → "+ 新增同步表" → 选 `expenses` collection
2. UI 自动：
   - 给 expenses 加 5 个管线/autodate 字段（其实迁移已加了 notion_id / notion_last_edited / last_synced_at，autodate 也在；provisioner 会幂等检测）
   - 建 Notion DB
   - 写 sync_config 行（title_field='description', date_field='date', auto_sync=true）
   - 把 expenses 加进 Sync Activity 的 collection select
   - 跑 reconcile_initial --only expenses

字段映射不需要 override（snake↔Title 都干净）：
- description → Description
- amount → Amount
- currency → Currency
- rate → Rate
- amount_usd → Amount Usd
- date → Date
- type → Type
- expense_category → Expense Category
- card → Card
- confirmation → Confirmation
- source → Source
- stop / day / trip → 三个 relation（**不会同步**，Notion 端列空着，参考 §"D. relation 不双向同步"）

`sync_global.timezone` / `sync_hour_local` 不变；expenses 走相同的小时 cron。

## 下游代码与 prompt 改动

按文件列出影响：

1. **`pocketbase/pb_migrations/`**（执行顺序很重要——days.trip optional 必须先，因 transactions 迁移会按需新建 day 行）
   - 新 migration: `1779465700_days_trip_optional.js`（放宽 days.trip）
   - 新 migration: `1779465701_create_expenses.js`（建 expenses + 字段）
   - 新 migration: `1779465702_migrate_transactions_to_expenses.js`（复制 transactions 数据 + 按 date 自动建/找 day + 删旧表）
   - 新 migration: `1779465703_migrate_stops_money_to_expenses.js`（拷 stops 金额到 expenses）
   - 新 migration: `1779465704_drop_stops_money_fields.js`（drop stops 的 4 个字段）

2. **`docs/data-model.md`**
   - §2 新增 `expenses` 章节
   - §2.3 stops 删 amount/currency/rate/amount_usd 行
   - §2.2 days.trip 改注释 optional
   - §3 加 Notion expenses DB（DB id / data source id 等先占位，PR2 落地时补）
   - §4.2 不动（没有新类型）
   - §7 新增 7.X 例子："add an expense to a stop"、"日常消费"
   - §10 quick reference: 加 expenses 行（9 个 sync targets 了）

3. **`CHECKIN.md`**
   - "Step 3a Stop upsert" 那段：去掉 stop 上挂 amount 的写法
   - 新增 "Step 3b Expense create"：每笔花销建独立 expense 行，挂 stop / day / trip

4. **`mcp_pb/SMARTNOTE_PROMPT.md`** 同上

5. **`pb_tools.py`**（如有 expense 帮助函数）：增加 `create_expense_for_stop(stop_id, amount, currency, ...)`

6. **前端 `static/index.html` / `app.js`**
   - 如果有"消费"输入框直接绑到 stop.amount → 改成 POST /api/expenses（或 PB pb_create('expenses')）
   - "同步设置" 列表会自动出现 expenses（registry-driven）

7. **`notion_sync/registry.snapshot.yaml`**：跑 `scripts/dump_sync_registry.py` 后会自动包含 expenses

8. **`scripts/reconcile_initial.py`** / **`notion_sync/runner.py`**：
   - 因为 title/date field 由 sync_config 提供（2026-06-04 已重构），新表不需要 hard-code 进 Python dict

9. **`CHANGELOG.md`**: 记一笔

10. **测试**：
    - `tests/notion_sync/test_matching.py` 等已有的不变
    - 新增（可选）：`tests/test_expense_migration.py` 验证 migration 后 transactions 全部出现在 expenses 且字段对应

## 验收清单

PR1 落地后：
- [ ] PB 里 `expenses` collection 存在，schema 与设计文档一致
- [ ] `transactions` collection 不再存在
- [ ] 原 11 条 transactions 全部出现在 expenses 里，金额/日期/分类/confirmation 完整
- [ ] 4 笔 "代付 Monica" 的 expense_category = '代付'
- [ ] 全部旧 transactions 已挂 day（按 date 自动回填或新建）；落在 trips 范围内的也自动挂上 trip
- [ ] 退款行 amount < 0（如有）
- [ ] 原 `stops.amount > 0` 的行（不含 0）每行对应一条 expense，关系挂回 stop / day / trip 正确
- [ ] `stops` collection 上不再有 amount / currency / rate / amount_usd
- [ ] `days.trip` required = false
- [ ] 今天 4 笔测试 stop 的 note "测试数据，迟点删除" 完整保留
- [ ] PB snapshot 已落 `.bridge_data/backups/<ts>/`
- [ ] `deploy` 通过，phone-bridge.service 正常起

PR2 落地后：
- [ ] 在前端 "+ 新增同步表" 选 expenses → 一键搞定
- [ ] Notion 端 Expenses DB 存在，包含全部非 relation 字段
- [ ] 初次 reconcile 把全部 expenses 推到 Notion，每行 pb_id / last_synced_at 已填
- [ ] 在 Notion 改一条 expense.description → 1 小时内（或 sync_now）同步回 PB
- [ ] CHECKIN.md / SMARTNOTE_PROMPT.md 已更新，agent 不再写 stop.amount
- [ ] 实测：录一笔日常消费（无 stop）+ 录一个含 3 笔花销的 stop

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 数据迁移把 transactions 弄丢 | 强制 PB snapshot；migration JS 的 up 操作分阶段提交，每步可 rollback；测试数据先在本地 dev PB 跑一遍 |
| 现有 sync 流程因 stops schema 改了崩 | 迁移完后立即 `--force-now --only stops` 跑一次，确认 categorize 不报错；stops 的 categories 没动，runner 应无感 |
| Notion 端 stops 的 Amount / Currency / Rate / Amount Usd 旧列变孤儿 | 迁移后让用户在 Notion UI 手动删（或 PR2 里 mcp__notion__notion-update-data-source DROP COLUMN）|
| expense_category 老值 vs 新值不兼容 | 全部保留老值并扩值，不破坏现有数据 |
| 今天 4 笔测试数据被误删 | 不删，迁移只 copy；用户后续按 note 关键字手动清理；spec 显式列出这 4 个 id |

## 开放问题（PR2 期间再讨论）

- Notion 端 Expenses DB 要不要加 rollup 列（"按 stop 显示总金额"），还是查询时 group by？暂不在 PR1/PR2 范围。
- 后续是否需要 PB hook 自动维护 `expense.trip = expense.day.trip` 一致性？（暂不做，靠写入侧自觉）
- Gmail 自动抓取目前由 agent 手动触发，PR2 改 prompt 让它写 expenses 而不是 transactions；将来全自动化时再单开 PR。
