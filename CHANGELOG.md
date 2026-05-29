# CHANGELOG

代码层面的 git 历史在 `git log` 里，这份文档解释**功能演进**和**为什么这么做**。
最早的几次 commit 略，从 2026-05 开始的大改动按主题归档。

---

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
