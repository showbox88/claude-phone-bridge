# Phone Bridge 全栈重构路线图

**Spec 创建**：2026-06-06
**当前状态**：✅ 已确认 · 待开始 Phase -1
**总工时估算**：16~24 天（单人 · 渐进推进，可跨多周）
**主分支**：`main` · 各阶段子分支：`refactor/phase-N-<slug>`

---

## 🔄 续接说明（新窗口必读）

**重启会话时按此清单走**：

1. **读这一节 + §进度追踪表** → 立刻知道在哪个阶段
2. 看进度表里"上次完成报告"的位置 → 看上次报告内容
3. 当前 in-progress 阶段：直接读对应章节的 §动作清单 + §准出闸门
4. 当前 pending 阶段：先调用 `superpowers:writing-plans` 把该章节展开成详细实施计划，再开始动手
5. **每个 Phase 结束时必须**：
   - 更新 §进度追踪表（状态、完成时间、commit SHA、报告内容）
   - 把 §下一步入口 改为下一个 Phase
   - commit + push 当前分支
   - 给用户报告（见 §完成报告模板）

**关键原则**（违反 = 失去"稳妥"保障）：
- 一次只在一个分支上做一个 Phase；不交叉
- 每个 Phase 准出闸门全部 ✅ 才能 merge 到 main 并开始下一个
- staging（dashboard-server）观察期：高风险阶段 48h，其他 24h
- 不混入新功能；新需求开 `feature/*` 分支搁置（重构期 feature freeze）

---

## 📊 进度追踪表（每完成一阶段必须更新此表）

| Phase | 状态 | 分支 | 完成日期 | Commit SHA | 报告位置 |
|---|---|---|---|---|---|
| -1 护栏 | ✅ 已合并 | `refactor/phase-minus1-guardrails` | 2026-06-06 | `3e60173` | CHANGELOG §Phase -1 |
| 0 地基 | ⏳ 待开始 | `refactor/phase-0-foundation` | — | — | — |
| 1 PB 统一 | ⏳ 待开始 | `refactor/phase-1-pb-client` | — | — | — |
| 2 拆包 | ⏳ 待开始 | `refactor/phase-2-app-package` | — | — | — |
| 3 Session 多实例 | ⏳ 待开始 | `refactor/phase-3-session-manager` | — | — | — |
| 4 前端 | ⏳ 待开始 | `refactor/phase-4-frontend-modules` | — | — | — |
| 5 sync | ⏳ 待开始 | `refactor/phase-5-sync-runner` | — | — | — |
| 6 收尾 | ⏳ 待开始 | `refactor/phase-6-polish` | — | — | — |

**状态图例**：⏳ 待开始 · 🚧 进行中 · ⏸ 暂停 · ✅ 已合并 · ❌ 回滚

### 下一步入口

👉 **下一步执行**：Phase 0 · 地基

下次开新窗口直接说"继续重构路线图"或贴这一行即可。

---

## 🎯 核心决策（已确认，不再回滚）

| 决策 | 选择 |
|---|---|
| 范围 | 全 6 阶段（+Phase -1 护栏） |
| Phase 2 vs 3 | **分开做**：先纯搬家，再改 session 语义 |
| 前端构建工具 | **不引入 Vite**，ES Modules 原生 |
| 测试目标 | 核心路径 + 新代码 100%，不强求总覆盖率 |
| 分支策略 | 每阶段独立分支 + smoke + 24~48h staging 观察 |

---

## 🛡️ Phase -1 · 护栏

**为什么先做**：所有后续重构都依赖这套护栏来发现回归。没有它=裸奔。

### 目标
建立"会响的烟雾报警"，保证后续每个阶段的回归在 30 秒内被发现。

### 动作清单
- [ ] 写后端 smoke `tests/smoke_backend.py`：启动 → 登录 → 建 session → WS 发消息 → 看到 turn_done → 查 today-todos → 调一次 `pb_search` → 删 session
- [ ] 写前端 smoke `tests/smoke_frontend.py`（headless playwright）：打开 PWA → 发消息 → 看到回复 → 切 source
- [ ] **生产 baseline 截图**：今天的 PWA 各 modal（usage / weekly / sync / cwd / bell / checkin / source-picker）各截一张，存到 `tests/baseline/`
- [ ] **deploy 回滚演练**：在 dashboard-server 上确认 `git checkout HEAD~1 && deploy` 能 10 分钟内回到上一版
- [ ] feature freeze 规则写入 `CLAUDE.md`：重构期 main 只接受 `refactor:` commit；新需求开 `feature/*` 搁置
- [ ] `refactor/` 分支命名规范写入 `CLAUDE.md`

### 准出闸门
- ✅ 两个 smoke 在 main 当前版本上跑通
- ✅ baseline 截图全部归档
- ✅ 回滚演练成功一次
- ✅ `CLAUDE.md` 包含两条新规则

### 工时
1 天 · 风险：极低

---

## Phase 0 · 地基（settings / paths / 原子 IO / 锁版本）

**目标**：底座到位，后面所有阶段都能踩着走。**零业务逻辑改动**。

### 动作清单
- [ ] 新建 `app/settings.py`（pydantic-settings），收口 67 处 `os.environ.get`；保留全部旧默认值；加 `.env` 自动加载（dotenv）
- [ ] 新建 `app/paths.py`：`BRIDGE_ROOT` / `DATA_DIR` / `UPLOADS_DIR`；消除 5 处硬编码 `/home/dev/phone-bridge/...`
- [ ] 新建 `app/io_utils.py`：`read_json_safe(path, default)` + `write_json_atomic(path, data)`（tempfile + os.replace）
- [ ] 改 `push.py`（push_subs.json）/ `server.py`（today_ack.json）/ `notion_sync/runner.py`（sync_alert_state.json）走原子写
- [ ] 引入 `pip-tools`：`requirements.in` → `pip-compile` 出锁文件
- [ ] 新建 `requirements-dev.txt`：pytest / pytest-httpx / playwright
- [ ] `db.py` 加 `PRAGMA journal_mode=WAL`
- [ ] 修 `server.py:1268` naive `datetime.now()`
- [ ] 67 处 `os.environ.get` 逐文件替换为 `from app.settings import settings`（分 8~10 个 commit）

### 准出闸门
- ✅ smoke 全过
- ✅ `pip-compile` 锁定的版本与今天 prod 实际版本一致（无意外升级）
- ✅ deploy 到 staging，24h 无新 error log
- ✅ `os.environ.get` 全库剩余 < 5 处（剩的必须是 `app/settings.py` 内部使用 + 启动脚本）

### 工时
1~2 天 · 风险：低

---

## Phase 1 · 统一 PB 客户端 + MCP 工具单源

**目标**：消除 5 份 PB 客户端 + 2 份 MCP 工具描述漂移。

### 目标包结构
```
app/integrations/pb/
├── client.py        # PBClient (sync) + AsyncPBClient (to_thread 包装) + 实例 token 缓存
├── token.py         # token-into-env helper（给子进程 Bash 用）
└── ops.py           # 业务级纯函数：search/get/create/update/delete/list_collections/list_records/...

app/agent/mcp_tools/
├── _envelope.py     # _ok/_err 信封
├── pb_tools.py      # 10 个 @tool 装饰器薄层（~150 行）
└── prompts.py       # 工具描述字符串单源

mcp_pb/server.py     # 重写为薄层（~150 行），import app.integrations.pb + prompts
```

### 动作清单
- [ ] 写 `PBClient`（同步 urllib），含 token 实例缓存（按 PB 实际 TTL 自动续）
- [ ] 写 `AsyncPBClient`（await `to_thread`）
- [ ] 在 client 内置 **5xx + 429 exponential backoff**（重试 3 次，退避 1s/2s/4s + jitter）
- [ ] 写 `ops.py` 纯函数
- [ ] 写 `prompts.py`：10 个工具的 description / args_schema 字符串
- [ ] 重写 `pb_tools.py` 为薄装饰器层
- [ ] 重写 `mcp_pb/server.py` 业务部分（保留 OAuth provider 不动）
- [ ] 删 `todos_client.py`，业务收编到 `ops.py:list_today_todos()`
- [ ] 删 `server.py` 里的 `_pb_refresh_token` / `_pb_get_json` / `_pb_refresh_loop`，改用 client
- [ ] 删 `notion_sync/pb_api.py`，改 import `app.integrations.pb`
- [ ] 写 `tests/test_pb_client.py`（pytest-httpx mock）：成功 / 401 重 auth / 429 退避 / 5xx 重试 / token 过期续期

### 准出闸门
- ✅ `tests/test_pb_client.py` 全过
- ✅ smoke 全过（重点验证 MCP `pb_search` 在两个入口行为一致）
- ✅ grep 验证：除 `app/integrations/pb/` 外，无其他文件直接 `urllib.request` + `pocketbase`
- ✅ staging 24h

### 工时
2~3 天 · 风险：中

---

## Phase 2 · 后端拆包 `server.py` → `app/`

**目标**：2400 行单文件 → 包结构。**纯搬家，不改语义**。

### 关键纪律
- 这一阶段是"复制粘贴 + import 路径调整"
- **不**改 Claude session 架构（留 Phase 3）
- **不**改业务逻辑、不改 API 响应格式
- **不**加 CSRF（留 Phase 6）
- 只允许的"非搬家"改动：CORS 显式列源、收 37 处裸 `except` 到具体异常

### 目标包结构
```
app/
├── main.py                # FastAPI app + lifespan + CORS + middleware wiring
├── deps.py                # FastAPI Depends: settings, db, pb_client, auth_state
│
├── api/                   # 46 路由按主题拆 10 文件
│   ├── sessions.py        # /api/sessions/*
│   ├── settings.py        # weekly-report / notion-sync 设置
│   ├── sync.py            # /api/sync/now /targets/* /registry/*
│   ├── uploads.py
│   ├── today_todos.py
│   ├── push.py
│   ├── poi.py             # /api/poi/around + _haversine + 3 个 provider Strategy
│   ├── browse.py          # /api/browse /api/mkdir
│   ├── meta.py            # /api/meta /health /usage
│   └── well_known.py      # OAuth discovery
│
├── auth/
│   ├── state.py           # AuthState
│   ├── cookies.py
│   ├── pages.py           # /setup /login /devices HTML
│   └── middleware.py
│
├── ws/
│   ├── handler.py         # /ws + handle_ws_message + handle_cmd（仍用全局 state）
│   └── broadcast.py
│
├── agent/
│   ├── content.py         # _build_user_content + xlsx/text 解析
│   ├── permission.py      # can_use_tool
│   ├── options.py         # make_options（mode/model/PB/tz 注入）
│   └── turn.py            # run_user_turn + _block_to_event
│
├── persistence/
│   ├── db.py              # 原 db.py
│   └── files.py           # _safe_filename / uploads_dir
│
└── reporting/
    ├── scheduler.py       # scheduler_loop
    └── weekly_report.py
```

### 动作清单
- [ ] 创建包结构（空文件 + `__init__.py`）
- [ ] 按上图把代码搬到对应文件，**一类一 commit**（API 一个 commit、WS 一个、agent 一个…）
- [ ] `gmail_oauth_setup.py` 挪到 `scripts/`
- [ ] POI 三个 provider（Overpass / Foursquare / Amap）抽 `PoiProvider` 抽象 + 公共 `_normalize`
- [ ] CORS：显式列 `https://dashboard-server.tail4cfa2.ts.net` 等明确源
- [ ] 收 37 处裸 `except Exception` 到具体异常；SDK 边界保留 `except Exception: log.exception()`
- [ ] `server.py` 留作 < 200 行的 launcher
- [ ] 写 `tests/replay.py`：录制 100 个 API 调用 + 30 条 WS 消息的 trace，前后行为 diff = 0

### 准出闸门
- ✅ smoke 全过
- ✅ `tests/replay.py` 前后行为完全一致
- ✅ `server.py` 行数 < 200
- ✅ `app/` 下每个文件 < 400 行（agent/turn.py 是唯一例外，可到 ~500）
- ✅ staging **48h**（高风险阶段）

### 工时
3~5 天 · 风险：**高**（搬家最容易出回归）

---

## Phase 3 · Claude session 多实例化 + Notion 鲁棒性

**目标**：修 P0 隐藏炸弹 B（多设备并发就炸）+ E 剩余（Notion 退避）。

### 动作清单
- [ ] 新建 `app/agent/manager.py`：`SessionManager` `Dict[session_id, ClaudeAgent]`
- [ ] WS handler 改造：每条 WS 消息携带 `session_id`，路由到对应 `ClaudeAgent`
- [ ] `set_model` / cwd 切换走 `SessionManager.recreate(session_id)`，不重建全局
- [ ] `init_client` / recreate 之前先 `await turn_lock`（等当前 turn 结束）
- [ ] 老的全局 `state.client` 移除；`AppState` 字段相应清理
- [ ] `notion_sync/notion_api.py` 加 429/5xx 退避 + `Retry-After` 头识别
- [ ] Notion `_throttle` 改 **token bucket**（3 req/s capacity + burst 5）
- [ ] 写 `tests/test_session_manager.py`：两并发 session、permission_request 互不干扰、recreate 时 in-flight turn 安全终止

### 准出闸门
- ✅ `tests/test_session_manager.py` 全过
- ✅ 实测：两台设备同时连接，互发消息 30 分钟无串扰（手工 + 截图）
- ✅ Notion linkage PATCH 墙时从 数十秒 → < 5 秒
- ✅ staging **48h**（这阶段风险最高）

### 工时
2~3 天 · 风险：**高**

---

## Phase 4 · 前端模块化（ES Modules，无构建工具）

**目标**：`app.js` 2873 行 → 25+ 个 ES Modules；修流式 CPU 爆炸 + XSS 风险。

### 目标包结构
```
static/
  app.js                    # ≤ 40 行 boot
  js/
    dom.js, state.js, api.js
    util/{timers,format,yaml}.js
    ws/
      socket.js             # connect/send/reconnect/ping
      handlers.js           # 表驱动 HANDLERS[type]
    render/
      markdown.js           # renderMarkdown + DOMPurify
      message.js, tool.js, perm.js, typing.js, scroll.js
    session/
      list.js, header.js, drawer.js
    composer/
      input.js, attachments.js, send.js
    features/
      checkin.js, cwd-browser.js, usage.js, weekly-report.js
      sync-settings.js, sources.js, bell.js
  css/                      # style.css 拆 12 文件
    tokens.css base.css layout.css appbar.css drawer.css
    messages.css tools-perms.css composer.css picker.css utilities.css
    dialogs/{checkin,usage,sync,weekly,cwd,bell}.css
```

### 动作清单
- [ ] 拆 ES Modules：按目标结构搬代码，**先并存后切换**（旧 app.js 改名 `app.legacy.js`，新入口起来后再删）
- [ ] `state.js`：~80 个 `let` 集中成 store + subscribe（pub-sub 最小实现）
- [ ] `ws/handlers.js`：表驱动 `HANDLERS[type]`，5 处重复的 `currentAssistantBubble=null` 收到 `endStream()` helper
- [ ] **流式增量渲染**：缓冲到段落/代码块边界才整段 markdown 化，期间用 `textContent` 追加
- [ ] vendored **DOMPurify**（无构建，下载到 `static/vendor/`），所有 `innerHTML = markdown` 改 `innerHTML = DOMPurify.sanitize(markdown)`
- [ ] 删 `setupPush` 46 行死代码
- [ ] `style.css` 拆 12 文件，新增 `--space-*` `--radius-*` `--font-*` token；modal/dialog 抽 `.modal` / `.modal-card` / `.modal-row` 共享类
- [ ] `<dialog>.showModal()` 加 polyfill 或 fallback（iOS 14 用 fixed positioning）
- [ ] JS/CSS 版本号合一：`index.html` 引用 `?v=N` 都同步
- [ ] `sw.js` 缓存清单更新所有新模块
- [ ] 统一 fetch wrapper `api.js`：`apiGet/apiPost/apiPatch/apiDelete` + 统一错误 toast；25 处 fetch 全部改用

### 准出闸门
- ✅ playwright smoke 全过
- ✅ 与 Phase -1 baseline 截图对比（人工 5 分钟），视觉无回归
- ✅ iOS 14 / 15 / 17 各跑一遍 PWA，主流程正常
- ✅ 长回答（>5000 字）流式渲染：手机实测 CPU < 50%
- ✅ 离线启动正常（SW 缓存验证）

### 工时
3~5 天 · 风险：中

---

## Phase 5 · `notion_sync/runner.py` 拆解 + 算法升级

**目标**：拆 781 行 runner，修同步竞态 + 性能 + 可观察性。

### 动作清单
- [ ] runner 拆 4 文件：`bootstrap.py` / `decisions.py` / `dispatch.py` / `post_phases.py`
- [ ] 引入 `SyncContext` dataclass，打包 `field_types / overrides / title_field / notion_schema / relation_lookup / relation_targets`
- [ ] `ACTION_HANDLERS = {PbOnlyChange: _apply_pb_to_notion, ...}` 字典分派替换 `isinstance` 链
- [ ] 修 `apply_pending_decisions` 的 `Use Notion` 时序竞态：先 PATCH Notion 再回写 last_edited（保证不会被反向覆盖）
- [ ] `linkage.py` 硬编码列名（"Date" "Day" "Trip" "Dates"）改走 `field_map_overrides`
- [ ] `icons.py` 硬编码 `if collection == ...` 链改为 declarative：`sync_config` 加 `icon_field` / `icon_default` 列，provisioner 兼容
- [ ] `sync.log` 改 `RotatingFileHandler`（10MB × 5）
- [ ] `apply_error` 日志必带 `pb_id` + `notion_id`（从 action 自动提取）
- [ ] `frozen_pairs_for_collection` 改一次性 group-by collection 拉取，减少 N 次 Notion query
- [ ] `relation_lookup` 改成 lazy（用到时才拉单 collection），消除 8×8 = 64 次全表
- [ ] `should_run_now` 改"上次成功 run < 23h"检测，防止跨小时漂移
- [ ] 归档 7 个已完成 backfill/migration 到 `scripts/archive/`：
      `migrate_days_to_stops.py` / `migrate_transactions_to_expenses.py` /
      `migrate_stops_money_to_expenses.py` / `cleanup_todo_titles.py` /
      `backfill_location_timezones.py` / `backfill_stop_timezones.py` / `backfill_child_timezones.py`
- [ ] 删 `config.invalidate()` 死 API（或接上使用）
- [ ] 修 `notify_pending` 反向依赖：移除 `sys.path.insert + import db`，改为 runner 通过 settings 注入 db 路径

### 准出闸门
- ✅ 现有 `tests/notion_sync/` 全过
- ✅ 新增 `tests/notion_sync/test_apply_decisions.py` 覆盖 4 种 decision 路径 + 时序竞态修复
- ✅ 强制 `--force-now` 对全 8 张表跑一次，Sync Activity 输出与 Phase 5 之前比对，行为一致
- ✅ 同步墙时（无新数据时）< 30 秒
- ✅ staging **48h**（让 hourly 定时器跑完几轮）

### 工时
2~3 天 · 风险：中

---

## Phase 6 · 收尾（测试 / 日志 / 安全 / 文档）

### 动作清单

**测试补齐**：
- [ ] `tests/test_session_manager.py`（已在 Phase 3）
- [ ] `tests/test_auth_middleware.py`
- [ ] `tests/test_run_user_turn.py`（用 fake SDK client）
- [ ] `tests/test_build_user_content.py`（xlsx/text 解析）
- [ ] `tests/test_can_use_tool.py`
- [ ] `tests/test_db_crud.py`

**可观察性**：
- [ ] 引入 `structlog` + `contextvars`：自动注入 `session_id` / `request_id` / `cb_id` 到每条 log
- [ ] 统一日志出口：所有模块 `from app.log import get_logger`
- [ ] 留 OTel hook（不强制启用，预留以后接 Sentry）

**安全收尾**：
- [ ] CSRF 双提交 token 中间件，POST 路由强制
- [ ] cookie `SameSite=Strict`
- [ ] Origin 校验中间件
- [ ] 审查 `private_key.pem` / `public_key.pem`，确认用途；无用则删并从 git 历史清除
- [ ] 检查 `.gitignore` 是否含所有 secrets-likely 路径

**文档**：
- [ ] 更新 `CHANGELOG.md`：列出 6 个 phase 的关键变化
- [ ] 重写 `CLAUDE.md`：反映新包结构 + 新规则
- [ ] 更新 `README.md` 架构图
- [ ] 写一份 `docs/architecture.md`：包结构 + 数据流 + 关键决策（替代散落的 PHASE2_PLAN.md 等）

### 准出闸门
- ✅ `pytest -v` 全过
- ✅ 核心数据路径（`run_user_turn` / `can_use_tool` / `auth_middleware` / `PBClient` / `SessionManager` / db CRUD）100% 测试覆盖
- ✅ 一次 CSRF 攻击模拟脚本无法成功
- ✅ 文档抽查 5 处与代码一致

### 工时
2 天 · 风险：低

---

## 📝 完成报告模板

每个 Phase 结束时按这个格式给用户：

```
## ✅ Phase N 完成报告

**分支**：refactor/phase-N-<slug>
**Commit 范围**：<起始 SHA>..<结束 SHA>
**实际工时**：X 天
**Staging 观察**：YYYY-MM-DD HH:MM 起 24/48h，无新 error log

### 完成的动作
- [x] ...
- [x] ...

### 准出闸门状态
- ✅ smoke 全过
- ✅ ...

### 偏离计划
（无 / 或：具体偏离 + 原因 + 影响）

### 下一步
👉 Phase N+1 · <名称>
新窗口启动指令："继续重构路线图，从 Phase N+1 开始"
```

同时更新本文件的 §进度追踪表 + §下一步入口。

---

## 🚨 暂停 / 回滚预案

**暂停**：随时可以 ⏸ 在任意 Phase 中段停。当前分支 push 即可，下次从这个分支继续。

**回滚单 Phase**：staging 观察期发现回归 → `git revert <merge SHA>` + redeploy。回滚不影响已合并的更早 Phase。

**全局回滚**：极端情况下，所有 `refactor/*` 分支都不合并到 main，main 始终保持 Phase -1 之前的状态。

---

## 附录 A：原始审计要点（决策来源）

完整的审计报告由 4 个并行 agent 在 2026-06-06 产出，结论已融入各 Phase。核心结论：

- `server.py` 2400 行实为 7~8 个微服务，单一职责严重违反 → Phase 2
- 全局 `state.client` 单 Claude session 绑定所有 WS → Phase 3
- PB 客户端 5 份重复 + MCP 工具描述 2 份漂移 → Phase 1
- 零 HTTP 重试 / 429 处理 → Phase 1 + 3
- `requirements.txt` 全 `>=` 无锁 → Phase 0
- `app.js` 2873 行 + ~80 个模块级 let + 流式 token 每次重渲整段 markdown → Phase 4
- `notion_sync/runner.py` 781 行 + 同步竞态 + 性能瓶颈 → Phase 5
- 核心数据路径 0 测试 → Phase 6（持续补）

---

**Spec 终结**。后续每次进入新阶段，先调用 `superpowers:writing-plans` 把该 Phase 章节展开成可执行计划。
