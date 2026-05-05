# Claude Phone Bridge

从手机浏览器实时操控你 PC 上的 Claude Code，支持流式回复、命令确认、推送通知。

- **后端**：FastAPI + `claude-agent-sdk`（本地跑，不经云中转）
- **前端**：单页 HTML/JS，PWA，可安装到手机主屏幕
- **远程访问**：Tailscale（仅 tailnet，免登录密码）
- **推送**：Web Push（页面关了也能收到 Claude 的确认请求）
- **权限**：读操作（Read/Glob/Grep/WebFetch 等）自动放行；写操作（Bash/Edit/Write 等）手机弹按钮确认

## 快速上手

### 1. 装依赖

```bash
pip install -r requirements.txt
```

### 2. 生成 VAPID 密钥（一次性）

VAPID 是 Web Push 的身份签名。`pywebpush` 已经把 `vapid` CLI 带过来了：

```bash
# 1. 生成 private_key.pem 和 public_key.pem
vapid --gen

# 2. 拿 base64 公钥（贴到 .env 的 VAPID_PUBLIC_KEY）
vapid --applicationServerKey

# 3. 把 .pem 私钥转成 base64 字符串（贴到 .env 的 VAPID_PRIVATE_KEY）
python -c "import base64; from py_vapid import Vapid; v=Vapid.from_file('private_key.pem'); print(base64.urlsafe_b64encode(v.private_key.private_numbers().private_value.to_bytes(32,'big')).rstrip(b'=').decode())"
```

> 不需要推送也能用，留空即可，只是出门时手机不会弹通知。

### 3. 配置 `.env`

```bash
cp .env.example .env
# 编辑 .env，主要填：
#   DEFAULT_CWD=你想让 Claude 默认工作的目录
#   VAPID_EMAIL / VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY（可选）
```

`ANTHROPIC_API_KEY` 留空即可 —— SDK 会复用 Claude Code 已登录的账号。

### 4. 启动

```bash
python server.py
```

控制台显示 `Uvicorn running on http://127.0.0.1:8000` 就是好了。

### 5. 本机先跑通

浏览器开 `http://127.0.0.1:8000`，发"读 README.md 一句话总结"，能看到流式回复就成功了。

试试"在当前目录新建 hello.txt 写 'hi from web'" —— 页面会弹一张 🔧 Write 卡片，点 ✅ 允许，文件就会出现在磁盘上。

## 让手机访问（Tailscale）

### 装 Tailscale

1. 电脑装 Tailscale 并登录：[tailscale.com/download](https://tailscale.com/download)
2. 手机装 Tailscale app（App Store / Play Store），用同一个账号登录

### 在 PC 上申请 HTTPS 证书并启动反代

Web Push 需要 HTTPS，而 Tailscale 自带证书功能：

```bash
# 1. 看你这台 PC 在 tailnet 里的完整名字（形如 your-pc.tail-xxxx.ts.net）
tailscale status

# 2. 申请证书（首次会让你确认；证书自动续期）
tailscale cert your-pc.tail-xxxx.ts.net

# 3. 启动 HTTPS 反代到本地的 server.py
tailscale serve --bg --https=443 http://localhost:8000
```

之后手机浏览器开 `https://your-pc.tail-xxxx.ts.net` 就能用了。

> **Windows 上的命令是 `tailscale.exe ...`**，PowerShell 直接可调。

### 装到手机主屏幕

**iOS**（必须装为 PWA 才能用 Web Push，iOS 16.4+）：
1. Safari 打开 `https://your-pc.tail-xxxx.ts.net`
2. 底部分享按钮 → 添加到主屏幕
3. 从主屏幕图标打开 → 点页面右上 🔔 → 允许通知

**Android Chrome**：
1. 浏览器打开 URL，会自动提示"安装"，点同意
2. 从桌面图标打开 → 点 🔔 → 允许通知

## 用法

### 普通对话

直接输入消息发送即可，多轮对话会保留上下文。Markdown 代码块（```` ``` ````）会渲染成代码区。

### 命令

页面右上 ⚙ 菜单：

- **新会话** — 清空 Claude 上下文，等同 `/new`
- **取消当前轮** — 中断 Claude 正在跑的这一轮，等同 `/cancel`
- **切换工作目录…** — 打开一个浏览框，可下钻进 `DEFAULT_CWD` 下的任何子目录，也能新建文件夹。**只能在 `DEFAULT_CWD` 内活动，不能往上跳出**（沙箱边界，写在 server.py 里强制校验）

输入框里也能直接打 `/new`、`/cancel`、`/cwd <相对路径>`，不过 `/cwd` 现在的路径是相对 `DEFAULT_CWD` 的（比如 `subfolder/inner`），不接受跳出根的绝对路径。

### 权限确认

写操作（Bash、Edit、Write、NotebookEdit 等）会弹 🔧 卡片：
- 卡片里能看到完整的工具输入（命令、文件路径等）
- 点 ✅ 允许 / ❌ 拒绝
- 出门时锁屏会推送通知；点通知跳回 PWA 后再点按钮

10 分钟不响应自动拒绝。

如果想调整哪些工具自动放行，改 `server.py` 顶部的 `AUTO_ALLOW` 集合。

## 安全提醒

- Tailscale 仅 tailnet 模式下，**只有你自己 Tailscale 账号下的设备**能访问 —— 这是默认安全模型。**不要**用 `tailscale funnel` 把它暴露到公网，除非你自己加了认证层。
- `.env`、`*.pem`（VAPID 私钥）、`push_subs.json` 都不要提交 git（已在 `.gitignore` 里）。
- `DEFAULT_CWD` 是 Claude 能触达的根边界 —— 浏览框和 `/cwd` 都不能跨出去。如果要让 Claude 操作根之外的代码，调整 `.env` 里的 `DEFAULT_CWD`。
- `push_subs.json` 是运行时生成的推送订阅列表，重启不丢。删了就需要每个设备重新点 🔔 启用推送。

## 架构小结

```
[手机 PWA]  ←HTTPS/WSS→  [Tailscale Serve]  ←HTTP→  [server.py]
   ↑                                                    │
   │  Web Push                                          ↓
   └─────  [pywebpush]  ←──── 推送内容 ──── [Claude Agent SDK]
                                                       │
                                              [Claude Code CLI]
                                                       │
                                              [你的代码]
```

- 单 Python 进程，asyncio
- 单 ClaudeSDKClient（持续会话）
- WebSocket 双工通信，支持多个浏览器 tab 同时连接（事件广播）
- 权限通过 `asyncio.Future` 在 SDK 回调和 WS handler 之间同步

## 常见问题

**首次启动报 `claude` 命令找不到？**  
不会发生 —— `claude-agent-sdk` 自带 bundled CLI。如果真出问题，确认 PATH 里能跑 `python` 和该包安装目录里的 `_bundled/claude.exe` 存在。

**手机访问不了，浏览器一直转圈？**  
检查：① 手机和 PC 都登在同一 Tailscale 账号 ② `tailscale status` 显示 PC 在线 ③ `tailscale serve status` 显示反代规则在 ④ 防火墙没拦本地 8000 端口（不应该会，因为是 localhost）。

**iOS 不弹推送权限？**  
必须先「添加到主屏幕」，从主屏幕图标进入（不是 Safari），并且系统是 iOS 16.4+。如果还是不行，去系统设置 → 通知 → 找到 Claude → 检查通知是否允许。

**Claude 的回复不流式，一次性蹦出来？**  
正常。SDK 是按消息块（block）级别流式 —— 一段文本作为一个 block 整体到达，不是字符级流。如果需要更细，得改 SDK 调用方式。

**点了允许之后没反应？**  
看 PC 终端日志，可能是网络抖动让 WS 断了。前端会自动重连，重连后未完成的 pending 不会自动恢复 —— 只能重发请求。

**收推送但点了打开页面后没看到 pending 卡片？**  
浏览器从冷启动恢复后 WS 会重连，但 server 里 `pending` Future 是内存中的，跨连接不丢；只要这个权限请求还在 600s 内没超时，pending 卡片应当在新连接的页面里看到（前提是页面是关了又开 —— 如果是不同 tab，需要刷新一下）。
