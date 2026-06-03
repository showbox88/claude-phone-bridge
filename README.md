# Claude Phone Bridge

从手机浏览器实时操控你 PC 上的 Claude Code，支持流式回复、命令确认、推送通知、多设备切换。

- **后端**：FastAPI + `claude-agent-sdk`（本地跑，不经云中转）
- **前端**：单页 HTML/JS，PWA，可安装到手机主屏幕
- **远程访问**：Tailscale（仅 tailnet，免登录密码）
- **身份认证**：密码 + TOTP（一次登录、长期 cookie，每次请求滑动续期）
- **推送**：Web Push（页面关了也能收到 Claude 的确认请求）
- **权限**：读操作（Read/Glob/Grep/WebFetch 等）自动放行；写操作（Bash/Edit/Write 等）手机弹按钮确认
- **多设备**：手机里维护多个 source（家里电脑 / 办公电脑 / VPS）随时切换
- **会话管理**：Chat / Code 双模式 + 标题/内容搜索 + 编辑标题 + 删除

完整功能演进的历史在 [`CHANGELOG.md`](./CHANGELOG.md)。当前部署到 dashboard-server 的配置在 [`CLAUDE.md`](./CLAUDE.md)。

## 目录结构

```
phone-bridge/
├── server.py             # FastAPI 主进程
├── db.py                 # SQLite 会话 / 消息 / 用量持久化
├── auth.py               # bcrypt 密码 + TOTP + 设备 cookie
├── push.py               # Web Push 推送封装
├── pb_tools.py           # in-process MCP server (pb_* + sync_* 工具)
├── static/               # PWA 前端（HTML/JS/CSS/SW/icons）
├── pocketbase/           # Smart Note 后端（migrations + hooks，详见 pocketbase/README.md）
├── mcp_pb/               # MCP server, 让 claude.ai 云端读写 PocketBase
├── notion_sync/          # PB ↔ Notion 双向同步运行库（runner + codec + 决策应用器）
├── scripts/              # 一次性脚本：setup_notion_sync_db.py / reconcile_initial.py / migrate_days_to_stops.py
├── deploy/               # systemd units: notion-sync.{service,timer} + install_systemd.sh
├── docs/                 # 架构文档（data-model.md = 真相源；notion-pb-sync.md = sync 运维）
├── CHECKIN.md            # 打卡功能的协议规则（Claude 看这个写 PB）
├── PHASE2_PLAN.md        # 打卡 Phase 2 实施计划（历史）
├── CLAUDE.md             # 给在这个仓库工作的 Claude 看的运维笔记
└── CHANGELOG.md          # 历史变更记录
```

## 快速上手

### 1. 装依赖

```bash
pip install -r requirements.txt
```

### 2. 生成 VAPID 密钥（可选；不用推送可跳过）

```bash
vapid --gen                                  # 生成 private_key.pem / public_key.pem
vapid --applicationServerKey                 # 拿 base64 公钥 → VAPID_PUBLIC_KEY
python -c "import base64; from py_vapid import Vapid; v=Vapid.from_file('private_key.pem'); print(base64.urlsafe_b64encode(v.private_key.private_numbers().private_value.to_bytes(32,'big')).rstrip(b'=').decode())"
# 输出贴到 VAPID_PRIVATE_KEY
```

### 3. 配置 `.env`

```bash
cp .env.example .env
```

主要填：

| 变量 | 说明 |
|---|---|
| `DEFAULT_CWD` | Claude 默认工作目录（沙箱根） |
| `PORT` | 默认 8000；dashboard-server 部署用 8001 |
| `BRIDGE_NAME` | 在手机 source picker 里显示的名字 |
| `VAPID_*` | Web Push 三件套，可选 |
| `FOURSQUARE_KEY` / `AMAP_KEY` | 打卡 POI 搜索数据源，可选 |

`ANTHROPIC_API_KEY` 留空即可，SDK 会复用本机已登录的 Claude Code 账号。

### 4. 启动

```bash
python server.py
```

控制台显示 `Uvicorn running on http://127.0.0.1:8000`（或 8001）就是好了。

### 5. 首次设置密码 + TOTP

浏览器开 `http://127.0.0.1:8000/setup`，按页面指引：

1. 设密码（bcrypt 哈希存到 `.bridge_auth.json`）
2. 扫 QR 进 Google Authenticator / Authy / 1Password 等，注册 TOTP
3. 之后访问 `/` 用「密码 + 6 位 OTP」登录，cookie 默认 30 天，每次请求自动续期

> `.bridge_auth.json` 包含密码哈希和 TOTP secret，**已在 `.gitignore` 里**，绝不要提交。

### 6. 验证本机能用

登录后发"读 README.md 一句话总结"，能看到流式回复就成功。

试一句"在当前目录新建 hello.txt 写 'hi from web'" —— 页面会弹一张 🔧 Write 卡片，点 ✅ 允许，文件就会出现在磁盘。

## 让手机访问（Tailscale）

### 装 Tailscale

1. PC 装 Tailscale 并登录：[tailscale.com/download](https://tailscale.com/download)
2. 手机装 Tailscale app，用同一账号登录

### 申请 HTTPS 证书并启动反代

Web Push 需要 HTTPS，Tailscale 自带证书：

```bash
tailscale status                                # 取你的 tail-xxx.ts.net 主机名
tailscale cert your-pc.tail-xxxx.ts.net         # 申请证书（自动续期）
tailscale serve --bg --https=443 http://localhost:8001
```

之后手机浏览器开 `https://your-pc.tail-xxxx.ts.net` 即可。Windows 上换成 `tailscale.exe ...`。

### 装到手机主屏幕（PWA）

**iOS**（iOS 16.4+ 才能用 Web Push）：
1. Safari 打开 URL
2. 分享 → 添加到主屏幕
3. 从主屏幕图标启动 → 点右上 🔔 → 允许通知

**Android Chrome**：浏览器会自动提示「安装」→ 同意 → 从桌面图标进入 → 点 🔔 启用推送。

## 用法

### Source picker（多电脑切换）

首次启动会让你「添加电脑」—— 输入名字 + URL（如 `https://home-pc.tail-xxx.ts.net`）。可加多个，顶栏点 `▼` 切换。每个 source 是独立的 WS 连接和会话上下文。

### Chat / Code 两种 workspace

drawer 顶部 segmented 切换：

- **Code**：Claude 用 SDK 全套工具，可读写磁盘、跑命令（默认）
- **Chat**：单纯对话，不绑工作目录，适合问答

Code 会话和 Chat 会话在 drawer 里分开维护，互不干扰。

### 会话管理（drawer）

桌面端默认收起，点顶栏 ≡ 展开（状态记到 localStorage）；手机端 ≡ 弹出。drawer 里：

- 顶部 segmented：Chat / Code
- **搜索框**：实时按标题 + 消息正文匹配，命中文本高亮，匹配段在标题下方显示
- **新建会话**：按当前 workspace 模式建
- **会话列表**：每条 hover 显示 ✎（编辑标题）+ 🗑（删除）

### 输入框 / 附件

输入框左边 `⬆` 按钮展开向上菜单：📍 打卡、📷 拍照、🖼 从相册、📋 粘贴截图、📄 选择文件、🔗 附加电脑文件路径（仅 Code 模式）。

### 命令

页面右上 ⋯ 菜单：

- **重命名当前** — 改当前会话标题
- **切换工作目录** — 浏览框选 cwd（沙箱在 `DEFAULT_CWD` 内）
- **查看使用量** — 累计 token / cost 统计

输入框里也能打 `/new`、`/cancel`、`/cwd <相对路径>`。

### 权限确认

写操作（Bash、Edit、Write、NotebookEdit）会弹 🔧 卡片：完整工具输入 + ✅/❌ 按钮。出门时锁屏会推送通知，点通知跳回 PWA 再点按钮。10 分钟无响应自动拒绝。

哪些工具自动放行 → 改 `server.py` 顶部的 `AUTO_ALLOW` 集合。

### 顶栏版本徽章

源名后面有个 `vN`（如 `v33`）灰色小徽章 —— 是 `static/app.js?v=N` 的缓存版本号。每次代码更新会 bump，看徽章数字就知道有没有刷到新代码。

## 打卡（Smart Note 集成）

按下 ⬆ → 📍 打卡 → modal 取 GPS、列附近 POI、填消费/评分/短评 → 一条 fenced `checkin` 块自动发到聊天。Claude 看到 `CHECKIN.md` 里描述的协议，自动 curl 本地 PocketBase（trips / locations / days），完成全部写入。

- 打卡协议规则：[`CHECKIN.md`](./CHECKIN.md)
- 后端 schema 和部署：[`pocketbase/README.md`](./pocketbase/README.md)
- Phase 2 计划：[`PHASE2_PLAN.md`](./PHASE2_PLAN.md)
- claude.ai 云端读写 PB：[`mcp_pb/README.md`](./mcp_pb/README.md)

## Notion 双向同步（2026-06 上线）

PocketBase 是真相源；Notion 作为"编辑面板"双向同步 8 张表（trips / days / stops / plans / todos / contacts / locations / journal）。

- **触发**：systemd timer `notion-sync.timer` 每小时检查，仅在配置时区的 03:00 真跑（默认 America/New_York）
- **单边变更 + 新建**：静默同步，不打扰你
- **双边都改了 / 一边消失了**：写一条 Notion **Sync Activity** DB（`decision=Pending`），冻结这一行;Phone Bridge 自动创建一个 chat session 「📋 同步待确认 N 项」放到 sidebar 顶部
- **你设决定**：在 Notion Sync Activity DB 把 `decision` 改成 `Use Notion / Use PB / Delete both / Keep both`，下次 cron 自动执行
- **MCP 工具**：phone bridge chat 里可以让 Claude 调 `sync_now / sync_queue_status / sync_pause / sync_resume`

完整架构、数据流、运维 cookbook 在 [`docs/notion-pb-sync.md`](./docs/notion-pb-sync.md);schema/字段映射的真相源在 [`docs/data-model.md`](./docs/data-model.md)。

## 安全提醒

- Tailscale tailnet 模式下，只有你自己 Tailscale 账号下的设备能访问 —— 这是默认安全模型。**不要**用 `tailscale funnel` 暴露公网（`mcp_pb` 除外，那个有 Bearer token 自己当 auth gate）。
- `.env`、`.bridge_auth.json`、`*.pem`、`push_subs.json` 都不提交 git（已在 `.gitignore` 里）。
- `DEFAULT_CWD` 是 Claude 能触达的根边界 —— 浏览框和 `/cwd` 都不能跨出去。
- 登录 cookie 滑动 30 天续期，丢手机的话 ssh 上去删 `.bridge_auth.json` 里的 `devices` 数组、所有 session 失效。

## 架构

```
[手机 PWA]  ←HTTPS/WSS→  [Tailscale Serve]  ←HTTP→  [server.py]
   ↑                                                    │
   │  Web Push                                          ↓
   └─────  [pywebpush]  ←──── 推送内容 ──── [Claude Agent SDK]
                                                       │
                                              [Claude Code CLI]
                                                       │
                                              [你的代码 / PocketBase / ...]
```

- 单 Python 进程，asyncio
- 一个 ClaudeSDKClient 对应一个 active session，切换会话自动 swap
- WebSocket 双工，多浏览器 tab 可同时连（事件广播）
- 权限通过 `asyncio.Future` 在 SDK 回调和 WS handler 之间同步
- SQLite (`.bridge_data/`) 持久化会话 / 消息 / 用量

## 常见问题

**首次启动报 `claude` 命令找不到？**  
不会发生 —— `claude-agent-sdk` 自带 bundled CLI。如果真出问题，确认该包安装目录里的 `_bundled/claude(.exe)` 存在。

**手机访问转圈？**  
① 手机和 PC 同 Tailscale 账号 ② `tailscale status` PC 在线 ③ `tailscale serve status` 反代规则在 ④ 本机 8000/8001 端口没被防火墙拦。

**iOS 不弹推送权限？**  
必须先「添加到主屏幕」，从主屏幕图标进入（不是 Safari），iOS 16.4+。

**点了允许之后没反应？**  
WS 可能断了。前端会自动重连，重连后未完成的 pending 不会自动恢复。

**刷新后看到旧代码？**  
看顶栏 source 名后面的 `vN` 徽章 —— 跟服务器最新 `?v=` 对得上就是新代码；对不上就硬刷（Ctrl+Shift+R / iOS 长按刷新选「无缓存重新载入」）。

**手机端 drawer 默认是不是关闭的？**  
桌面端默认收起、点 ≡ 展开（状态持久化到 localStorage）；手机端 ≡ 弹出 overlay。
