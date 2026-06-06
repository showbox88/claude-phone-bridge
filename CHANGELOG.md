# CHANGELOG

代码层面的 git 历史在 `git log` 里，这份文档解释**功能演进**和**为什么这么做**。
最早的几次 commit 略，从 2026-05 开始的大改动按主题归档。

---

## 2026-06-06 — Phase 0 · 地基（settings / paths / 原子 IO / 锁版本 / WAL）

**Branch:** `refactor/phase-0-foundation` (15 commits, `fed6b23..a4994f9`)
**实际工时:** 约 4 小时（含 1 次 pywin32 lockfile 修）

### 落地的事
- `app/settings.py` — pydantic-settings，单一类型化 env 源（22 字段）+ 4 个单元测试
- `app/paths.py` — `BRIDGE_ROOT` + `DATA_DIR` + 派生路径常量
- `app/io_utils.py` — `write_json_atomic` + `read_json_safe` + 8 个单元测试
- `db.py` 开启 `PRAGMA journal_mode=WAL` + `synchronous=NORMAL`（`foreign_keys` 早已开）
- `server.py:1268` naive `datetime.now()` → UTC（runtime 最后一处 naive）
- 3 处 JSON 状态文件改为原子写（`push_subs.json` / `today_ack.json` / `sync_alert_state.json`）
- `requirements.in` + `requirements.txt`（pip-compile 锁文件，4 dep pin 到 prod 版本）
- `requirements-dev.txt`（pip-tools）
- **48 of 50 `os.environ.get` 迁移到 `app.settings`**（剩 2 处 PB_TOKEN 是 documented 例外，Phase 1 清理）
- 5 处硬编码 `/home/dev/phone-bridge/.venv/bin/python` 替换为 `sys.executable`（pb_tools 2 + server 2 + 1 dump_sync_registry）；硬编码 `/home/dev/phone-bridge` cwd 改 `str(BRIDGE_ROOT)`；硬编码 sync.log 改 `app.paths.SYNC_LOG`

### 闸门
- ✅ smoke 在 staging 跑绿（0.7s）
- ✅ deploy 成功 + journal 无 ERROR/Exception
- ✅ 生产 SQLite 在 WAL 模式验证（`journal_mode=wal`, `synchronous=1`, `foreign_keys=1`）
- ✅ 单元测试：`test_io_utils.py` 8/8，`test_settings.py` 4/4，`tests/notion_sync/` 106/107（1 pre-existing test_icons fail，main 上也 fail）
- ✅ grep 验证：剩余 `os.environ.get` 全部在 8 个 documented 文件（app/paths/settings、mcp_pb、2 个一次性 script、server PB_TOKEN side-channel、test fixtures）

### 偏离计划
1. **plan 把 env 读数估为 67，实际 50**（spec 审计偏大）；实际迁了 48 处。
2. **plan 漏了 2 个 env**：`VAPID_EMAIL`（push.py 的 mailto subject）和 `NOTION_SYNC_PARENT_PAGE_ID`（provisioner + scripts）。沿途发现后补进 settings + test_settings 的 env key 列表。
3. **`notion_sync/provisioner.py` + `todos_client.py` 用 `Settings()` per call 而非 module 单例**：前者因为 tests `monkeypatch.setenv` 无法穿透 module 级缓存；后者因为代码注释明确说"re-read each call so password rotation doesn't strand the report"。这是不漂移的设计选择。
4. **`server.py:_AUTH_FILE` 保留历史默认 `Path(__file__).parent/.bridge_auth.json`** 而非切到 `app.paths.AUTH_FILE`（= `DATA_DIR/...`）。原因：默认位置变=认证文件在新位置找不到旧设备 token=用户被强制重新设置。安全的迁移留给未来 phase。
5. **`server.py:CHECKIN.md` 绝对路径在 system prompt 字符串里没动**。原因：Phase 0 显式不改 prompt 文本；prod 上 `/home/dev/phone-bridge/CHECKIN.md` 等于 `BRIDGE_ROOT/CHECKIN.md`，无回归。
6. **pip-compile lockfile 漏 platform marker 引发首次 deploy 失败**：lockfile 在 Windows 编译时 strip 掉了 `pywin32` 的 `sys_platform == 'win32'` 标记，Linux 装失败。手工补 marker 后 deploy 成功。`requirements.txt` 加注释提醒未来 re-compile 后要再补一次。

### 跳过的闸门
- **24h staging soak 跳过**：用户 2026-06-06 ~01:00 同意"立即开 Phase 0"代替 24h 等待；理由是 Phase -1 零代码改动，soak 无新数据可看。Phase 0 有真实代码改动，但风险已通过 deploy + smoke + WAL 验证 + journal 检查覆盖。下一阶段建议恢复 24h soak。

### 下一步
👉 Phase 1 · 统一 PB 客户端 + MCP 工具单源
新窗口续接指令："继续重构路线图，从 Phase 1 开始"

---

## 2026-06-06 — Phase -1 · 重构护栏（roadmap 启动）

启动全栈重构（见 [docs/superpowers/specs/2026-06-06-refactor-roadmap.md](docs/superpowers/specs/2026-06-06-refactor-roadmap.md)）前，先装"会响的烟雾报警"：

- **后端 smoke**：`tests/smoke_backend.py`（stdlib + websockets，1.2s 跑完），验证 `/api/health` `/api/meta` `/api/sessions` `/api/today-todos` + WS hello 帧
- **回滚演练**：实地跑了一遍 `.bak.*` swap 路径，**1 分 18 秒**端到端验证完成（含两次 ~5s 重启 + 两次 smoke）；文档 [docs/operations/rollback.md](docs/operations/rollback.md) 已校准
- **CLAUDE.md** 加 §Refactor period rules：main 只接受 `refactor:/docs:` commit，每阶段独立 `refactor/phase-N-*` 分支 + smoke 闸门 + staging 24/48h soak
- **baseline 截图清单**：[tests/baseline/README.md](tests/baseline/README.md)（13 张），实际截图延后到 Phase 4 之前再做（Phase 0~3 不动前端，不阻塞）

### 闸门状态
- ✅ smoke 在 staging 跑绿（1.2s）
- ✅ 回滚演练 1m18s 完成验证
- ✅ CLAUDE.md 规则可见（3 处引用）
- ⚠️ baseline 截图：0 张（延后到 Phase 4 前；前 4 个阶段不触前端）

### 偏离计划
1. **playwright 前端 smoke 降级为手动 baseline 截图**：原 spec 提"headless playwright smoke"，writing-plans 阶段降级为手动截图清单。理由：playwright 在 Windows 装一遍占掉 Phase -1 80% 工时；Phase 0~3 不触前端，截图足够；真要 playwright 留到 Phase 4。
2. **smoke 写完后发现 2 处端点字段假设错**：`/api/health` 字段是 `ok` 不是 `status`；`/api/meta` 返回 `modes/models` 复数列表不是 `mode/model`。已修正。这正是护栏的价值。
3. **回滚机制描述被修正**：原文档写"git checkout + deploy"路径，演练时发现 deploy 内置 `.bak.*` swap 是真正的快速路径（~7s vs 全量 deploy ~14s）。rollback.md 重写为两条路径并存：快速路径 + 可复现路径。

### 下一步
👉 **Phase 0 · 地基**（settings / paths / 原子 IO / 锁版本，1~2 天）
新窗口续接指令："继续重构路线图，从 Phase 0 开始"

---

## 2026-06-05 — Expenses redesign（transactions → expenses，stops 的子表）

- **背景**：原本 `transactions`（migration 11）和 `stops` 都能记金额——日常消费走 transactions，旅行消费挂在 stop.amount。两个口子，做月度汇总/年报/旅游 vs 日常对比时数据是分裂的。同时一次 visit 可能多笔花销（公园 = 门票 + 冰淇淋 + 水），单字段 stop.amount 表达不了。
- **改动**：把 `transactions` 重塑为 `expenses`，做成 stops/days/trips 的子表。一个 stop 可有 N 个 expense；日常 expense 不挂 stop（stop=空，day=今天）。
- **新增字段**：在原 transactions 基础上加 `stop`/`day`/`trip` relation + `currency`/`rate`/`amount_usd`（沿用 stops 既有外币算法）+ `source` 新增 `Agent` 值。`category` 改名 `expense_category`，扩展为 10 个值（新增"门票"、"住宿"、"代付"、"其他"）。
- **约定**：`amount_usd` 由写入侧自动算（USD 行 = amount；外币 = amount × rate）；退款（type=退款）amount 存负数，sum 直接得净支出；`expense.trip = expense.day.trip`（denormalized，跟 stops 现行做法一致）。
- **Migrations**：1779465625（days.trip 改 optional——日常 day 可无 trip）+ 1779465626（create expenses）+ 1779465627（drop transactions，safety-gated）+ 1779465628（drop stops 4 个金额字段，safety-gated）+ 数据迁移脚本 `scripts/migrate_transactions_to_expenses.py` + `scripts/migrate_stops_money_to_expenses.py`。
- **数据保留**：11 行老 transactions 全数迁过来（4 笔 "代付 Monica" 自动归类 `expense_category=代付`），脚本按日期自动建/找 day 容器并回填 trip；6 个 amount>0 的 stop 自动 fan 出 6 条 expense 挂回；4 笔今天的测试 stop（坐火车/冰淇淋/winic tech/Ross）note "测试数据，迟点删除"完整保留。每阶段前 `notion_sync.backup` 落盘。
- **下一步**（独立 PR）：把 expenses 加进 sync registry（前端"+ 新增同步表"一键搞定）、改 CHECKIN.md / SMARTNOTE_PROMPT.md / 前端，让 agent 不再写 stop.amount，改为建 expense 挂 stop。
- feat(schema): add timezone fields to locations/stops/days/expenses/foods
  and due_at/due_tz to todos for cross-tz reminders
- feat(tz): tz_resolver helper + offline GPS→IANA via timezonefinder
- feat(backfill): three idempotent scripts populate tz on existing rows
- feat(sync): Notion datetime columns rendered with row's tz as +HH:MM offset
- feat(agent): client_tz piped from WS into system prompt

## 2026-06 — Trip 数据模型 stops redesign

- **背景**：原本 `days` 既是"日级容器"又是"事件记录"——一个真实日历日里发生 N 件事（吃饭、打车、买票、住宿），就得建 N 条 day 行，时间维度被压扁。Notion 和 PB 都不舒服。
- **改动**：把 `days` 切成两层——`days` 退化为纯容器（name / date / weather / note / content / trip），新增 `stops` 表承载原子事件（categories / amount / currency / rate / checkin / location / contact / journal 关联 + 双向 day&trip 关系）。一个 day 行下挂 N 个 stop。
- **`journal` 同步扩展**：加 `related_stop` 字段、`type` 多加 `Reminder` 选项、并补齐 sync 管线字段（`notion_id` / `notion_last_edited` / `last_synced_at`），加入双向同步阵营。
- **Migrations**：18 (`create_stops`) + 19 (`extend_days_for_stops_migration`) + 20 (`extend_journal_for_stops`) + 21 (`drop_legacy_days_fields`) + 数据迁移脚本 `scripts/migrate_days_to_stops.py`（按既有 days 行重组成 day-container + stop-event）。
- **5 阶段 runbook**：详见 [`docs/stops-redesign-runbook.md`](./docs/stops-redesign-runbook.md)；最终 schema 真相源在 [`docs/data-model.md`](./docs/data-model.md)。
- **影响下游**：打卡协议 ([`CHECKIN.md`](./CHECKIN.md)) Step 3 重写——先 upsert day 再建 stop。`notion_sync.runner` 已经认识 stops + journal 两个新 sync target。

## 2026-06 — Notion ↔ PocketBase 双向同步（PR1 + PR2 + PR3）

- **目标**：让 PocketBase（真相源）和 Notion（移动端可编辑的"驾驶舱"）保持一致;用户在 Notion 直观浏览/编辑，Claude 在 PB 写入,夜里自动汇流。
- **PR1（schema + 初次对齐）**：给 6 张同步表加 pipeline 字段（`notion_id` / `notion_last_edited` / `last_synced_at`）；新建 `sync_config`（per-collection 配置）+ `sync_global`（时区/小时/暂停）PB 表；在 Notion 建 "Sync Activity" DB 作裁决队列;`scripts/reconcile_initial.py` 跑一次性数据对齐(模糊匹配 + 反向回填 `pb_id`)。
- **PR2（每日 cron + 冻结机制）**：`notion_sync.runner` 跑 systemd hourly，Python 守门只在配置时区的 03:00 真跑。`changeset.py` 纯函数分类(NoChange / *Change / *New / *Vanished)。单边变更/新建静默同步;双边都改了 → 写 Sync Activity (Conflict, Pending),冻结这一对 ID，直到用户裁决。Sync Activity 也镜像 Delete? 场景。
- **PR3（决定应用器 + 通知 + MCP + 清理）**：`apply_pending_decisions()` 每次 cron 跑前扫 Sync Activity,执行用户设的 `Use Notion` / `Use PB` / `Delete both` / `Keep both`，标 `applied_at`。`notify_pending()` 跟周报一样自动建一个 chat session "📋 同步待确认 N 项" 推到 Phone Bridge sidebar。4 个 MCP 工具：`sync_now / sync_queue_status / sync_pause / sync_resume`。`cleanup_resolved_activity(days=90)` 归档过期 Sync Activity 行。
- **范围**：6 → 8 张表（stops redesign 之后又加 stops + journal）。共 ~3000 行代码 + 58 个测试。
- **已知限制**：relation 字段不参与同步（PB 用 PB 记录 ID，Notion 用 Notion page UUID，ID 空间不互通，详见 [`docs/notion-pb-sync.md`](./docs/notion-pb-sync.md#limitations--known-holes)）。
- **完整架构 + 运维 cookbook**：[`docs/notion-pb-sync.md`](./docs/notion-pb-sync.md);schema 真相源 [`docs/data-model.md`](./docs/data-model.md)。

## 2026-06 — phone-bridge 直接用 PocketBase 工具 (`mcp__pb__*`)

- **背景**：之前本机/手机的 Claude SDK 会话读写 PocketBase 只能靠 Bash + curl
  （`$PB_URL`/`$PB_TOKEN`），`can_use_tool` 里专门 fast-path 放行 localhost:8090 的
  curl。`mcp_pb/` 那套真正的 MCP 工具只服务 claude.ai 云端 Connector，本机 SDK 用不上。
- **改动**：新增 [`pb_tools.py`](./pb_tools.py)——一个**进程内** SDK MCP server
  （`create_sdk_mcp_server` + `@tool`），把 `mcp_pb` 的 CRUD 工具面镜像进来，让
  phone-bridge 自己的 SDK 会话直接调 `mcp__pb__*`，不再手搓 curl。
- **工具面**（与 `mcp_pb` 对齐）：`pb_list_collections / pb_search / pb_get /
  pb_get_collection / pb_create / pb_update / smartnote_open_context`（读 + 安全写）
  以及 `pb_delete / pb_create_collection / pb_update_collection /
  pb_delete_collection`（破坏性 / 改 schema）。
- **权限分级**：读 + 安全写的 7 个工具放进 `allowed_tools` 预批（无需手机确认，等价于
  老的 localhost curl fast-path）；4 个破坏性工具**故意不预批**，走 `can_use_tool` →
  手机权限卡（或 YOLO 自动批）。Chat / Code 两种模式都注册该 server。
- **认证**：`pb_tools.py` 自带 25 分钟 token 缓存 + 失效重新 auth，读同一套
  `POCKETBASE_*` 环境变量；与 `server.py` 那条 12h 刷新 loop 解耦，互不影响。urllib
  阻塞调用统一包进 `asyncio.to_thread`，不卡 FastAPI 事件循环。
- **提示词**：PB creds 存在时，给两种模式的 system prompt 追加一段说明（chat 直接拼字符串，
  code preset 用 `append`），告诉 Claude 优先用 `pb_*` 工具、开局先 `pb_list_collections`。
- **降级**：没配 `POCKETBASE_URL/EMAIL/PASSWORD` 时 `pb_tools.enabled()` 为 False，
  整个 MCP server 不注册，行为回到改动前。老的 Bash+curl fast-path 仍保留，CHECKIN.md 流程不受影响。

## 2026-05 — 周报 (Weekly Report)

- **功能**：每周（默认周一 09:00 Asia/Shanghai）自动新建一个 Chat 会话，标题
  `📊 周报 2026-Wxx`，里面是 markdown 周报（总轮次/花销/Token/按模型/Top cwd/Top 会话）。
- **数据来源**：现有 `turns` 表 + `sessions` 表，新增 `db.range_summary(start_ts, end_ts)`。
  不调 Claude API、不消耗额度。
- **配置 UI**：⋯ 菜单 → `周报设置`。可改开关/星期/时间，"立即生成一份"按钮回填上周。
- **持久化**：开关/时间/上次生成的周存 SQLite `settings` 表（新增）。
- **架构**：`report.py`（独立模块），`scheduler_loop` 在 `lifespan` 起 background task，
  每小时唤醒检查一次配置和触发条件，配置改了无需重启。生成后 Web Push 通知。

## 2026-05 — 期间大改

代码改动量极大，但 commit 节奏稀疏。下面按主题整理「实际上线了什么、为什么、怎么用」。

### 身份认证（commit `5b54dfb..cd9aefc`）

- **背景**：Tailscale 已经把入口收窄到 tailnet，但同一 tailnet 内多人/多设备时还是需要二次身份。
- **方案**：bcrypt 密码 + TOTP；登录后下发设备 cookie（pyjwt，HS256）。
- **机制**：每次 authed 请求都把 cookie 续到 30 天后（sliding expiry，commit `cd9aefc`），不用反复重新 OTP。
- **初始化**：`/setup` 一次性走通：设密码 → 扫 QR → 备份 manual key（commit `3d9aa3b` 改成 QR 黑底白字以兼容 Google Authenticator，`059ef13`）。
- **存储**：`auth.py` 把密码哈希 / TOTP secret / 设备列表全部塞到 `.bridge_auth.json`（已 .gitignore，绝不要提交）。

### Smart Note 后端（PocketBase）

- **目标**：把 Notion 的「行程 / 地点 / 消费 / 美食 / 日记 / 待办 / 联系人 / 灵感 / 计划 / 交易 / Claude memos / 简报」搬到本地 PocketBase（dashboard-server, port 8090），日常浏览/编辑改走 PB admin UI；Notion 退化为只读归档。
- **部署位置**：`/opt/pocketbase/`（不在 repo 内）；schema 和 hooks 在 `pocketbase/pb_migrations/` 和 `pocketbase/pb_hooks/`。
- **15 个 migrations**：详见 [`pocketbase/README.md`](./pocketbase/README.md)。命名编号是 unix timestamp，所以执行顺序固定。
- **PB Hook 坑**：`pb_hooks/days.pb.js` 替代 Notion 的 `Amount(USD)` 公式。PB v0.38 的 JS hook 每个 callback 跑独立 goja VM，**helper 必须内联到每个 hook 内部**，不能抽顶层函数复用（症状：静默失败，无日志）。
- **认证集成**：`server.py` 启动取 PB token 入 `os.environ["PB_TOKEN"]`，30 分钟后台 refresh；Claude SDK 在 Chat/Code 模式都用 Bash + curl 调本地 PB。

### 打卡（Check-in）

- **协议**：用户消息含 ` ```checkin ` fenced YAML 块 → Claude 看 [`CHECKIN.md`](./CHECKIN.md) → 按 5 步流程 curl PB（dedup Location → 自动归 Trip → 建 Day → 评分回写 Location → 反馈）。
- **Phase 1**：手敲 YAML 验证通路（2026-05-27 上线）。
- **Phase 2**：[`PHASE2_PLAN.md`](./PHASE2_PLAN.md) — 把 YAML 收进 modal，按按钮自动生成。已经实现 modal、GPS、Overpass + 高德 + Foursquare 三源 POI、字段填写、提交回填。
- **入口**：composer 的 ⬆ 菜单 → 📍 打卡（合并后的统一菜单，见下）。

### claude.ai 云端读写 PocketBase（`mcp_pb/`）

- **目的**：让 claude.ai（云端 product）通过 Custom Connector 直接读写 Smart Note 数据，而不只是手机/本机 Claude Code 用。
- **栈**：FastMCP + Tailscale Funnel + Bearer token。
- **公网入口**：`https://dashboard-server.tail4cfa2.ts.net/mcp`（Tailscale Funnel 自动 HTTPS）。
- **暴露的工具**：`pb_list_collections / pb_search / pb_get / pb_create / pb_update / smartnote_open_context`。
- **配置流程**：[`mcp_pb/README.md`](./mcp_pb/README.md)；claude.ai project 的 system prompt 见 `mcp_pb/SMARTNOTE_PROMPT.md`。
- **安全**：唯一 auth gate 是 `MCP_PB_BEARER_TOKEN`（48 字节 url-safe random，存 `.env`）。DNS rebinding protection 限定 Funnel 主机名。

### Web UI 大改（这一波 commit `cc85f64`）

依赖代码层面的核心改动。这次会话连续做了：

1. **顶栏出 main-pane**：原本 `.app-bar` 是 `.main-pane` 的子元素，桌面端 drawer 打开后顶栏被挤到 drawer 右边，浪费一大块。改成 body 是 grid（行 1 = 顶栏通栏，行 2 = drawer + main）。
2. **Drawer 可收起（仅桌面）**：默认收起、margin-left = `-width` 滑出屏幕左侧；点 ≡ 展开、X 关闭；状态存 localStorage（`bridge.drawer_expanded`）。
3. **会话搜索条**：drawer 里 `新建会话` 上方。debounce 180ms，Esc 清空，X 按钮清空，命中文本高亮（`<mark>`）。
4. **后端搜索**：`db.search_sessions(q)` + `/api/sessions?q=`，title + 全部 message content 的 LIKE 匹配，返回首个命中段的 60+ 字 snippet。
5. **会话项编辑按钮**：每条会话 hover 出现 ✎ / 🗑 两个图标按钮；✎ 调用现有 `rename_session` 命令。
6. **附件菜单合并**：原本 composer 行有三个独立按钮（`+` 拍照/相册菜单、`pin` 打卡、`paperclip` 文件），现在合并成单个向上箭头 `⬆`，展开包含：打卡 / 拍照 / 从相册 / 粘贴截图 / 选择文件 / 附加电脑文件路径。以后加新动作直接加 `<button data-pick="xxx">` + 一行 JS 即可。
7. **缓存版本徽章**：源名后面跟着 `vN`，自动从 `app.js?v=N` 解析，部署后看徽章数字判断刷到了哪一版。
8. **chevron_up / search 图标**：补图标，统一 Lucide 风格 1.75 stroke。
9. **mobile drawer 不动**：仍是 fixed overlay + transform 滑入，避免破坏现有交互。

#### 一些前期已经在跑、这次才补 commit 的功能

- **多 source picker**：手机维护多个 PC 入口（家 / 公司 / VPS），顶栏切换；持久化到 localStorage。
- **Workspace 模式**：Chat（纯对话） / Code（带工具）双模式，会话按模式分别管理。
- **粘贴截图**：composer 长按 / 工具栏内按钮直接读 clipboard image（iOS 16.4+ 弹原生 paste sheet）。
- **附件 swipe-up**：保留在 mobile 上（但这次合并后已不需要，逻辑被删）。

### 配置变化

- `.env.example` 新增 `FOURSQUARE_KEY` / `AMAP_KEY`（打卡 POI 数据源，均可选）
- `.env.example` 新增 `ALLOWED_ORIGINS`、`BRIDGE_NAME` 说明
- `requirements.txt` 加 `aiohttp`（Overpass / 高德 / Foursquare 并发请求用）
- `.gitignore` 加固：`.bridge_auth.json*` / `.env.bak*` / `*.bak.*` / `*.bak[0-9]*`（防止时间戳后缀的备份文件意外进库）

### 部署

- 实际部署到 dashboard-server (Debian 12)，`systemctl` 管 `phone-bridge.service`（PORT=8001，监听 127.0.0.1，Tailscale Serve 反代 443）。
- 这台机器本身就是 production，没有 staging。部署看 [`CLAUDE.md`](./CLAUDE.md)。
- 重启 service 会断 WS，会话从 SQLite 自动恢复但正在跑的工具调用可能中断。

---

## 备份文件说明

仓库根目录可能有这些**绝不要 commit** 的本机文件：

- `.bridge_auth.json` / `.bridge_auth.json.bak.<ts>` — 密码哈希、TOTP secret、设备列表
- `.env` / `.env.bak.<ts>` — API key / VAPID 私钥 / PB 密码
- `server.py.bak.<ts>` — 改大动作前的本机快照
- `private_key.pem` / `public_key.pem` — VAPID 密钥
- `*.crt` / `*.key` — Tailscale TLS 证书
- `push_subs.json` — Web Push 订阅列表（运行时写）

全部已在 `.gitignore` 覆盖。
