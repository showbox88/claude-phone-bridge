# Smat Trip UI — 架构

## 总览

```
手机/电脑 (Tailscale 设备)
   │  https://dashboard-server.tail4cfa2.ts.net:8451  (Serve, tailnet-only)
   ▼
Tailscale Serve :8451 ──► 127.0.0.1:8101  smat-trip.service (node server.js)
                              │
              ┌───────────────┼─────────────────────┐
              │               │                     │
        静态 dist/       /api/* 同源代理          /media/*
        (vite build,    + 免登录时注入            GET 出图
         SPA fallback)   Authorization            POST 原图上传
                              │                     │
                              ▼                     ▼
                    PocketBase 127.0.0.1:8090   /home/dev/smat-trip/media/
                    (phone-bridge 的库)          <collection>/<recordId>/<ts>_<name>
                              │
                    notion-sync (03:00/15:00 ET) ⇄ Notion
                    Litestream 10s WAL → CT 103 → 周归档 Oracle
```

## VM 上的文件

| 路径 | 内容 |
|---|---|
| `/home/dev/smat-trip/server.js` | 零依赖 node 服务器（静态 + 代理 + media），源码在 Smart-Trip 仓库 `deploy/pb-vm/server.js` |
| `/home/dev/smat-trip/dist/` | vite 构建产物（`npm run build:pb-vm` 后 scp 上来） |
| `/home/dev/smat-trip/media/` | 照片原图（**TODO：备份计划 Phase 4，每日 rsync → CT 103**） |
| `/home/dev/smat-trip/.env` | `PB_TOKEN`（10 年期 superuser impersonate token，chmod 600）+ `PB_INJECT_TOKEN=on` |
| `/etc/systemd/system/smat-trip.service` | `User=dev`，`EnvironmentFile=-/home/dev/smat-trip/.env`，开机自启 |

## 鉴权双开关（计划书 D1）

| 模式 | UI 构建（.env.pb-vm） | VM `.env` | 行为 |
|---|---|---|---|
| 免登录 | `VITE_PB_LOGIN=off` | `PB_INJECT_TOKEN=on` | 打开即用，代理注入 token，浏览器零凭据；前端用 `useAuthPb.js` 里的 `NO_LOGIN_USER` 常量显示固定名字 |
| 登录防护（当前，Phase 6 自 2026-06-10） | `VITE_PB_LOGIN=on` | `PB_INJECT_TOKEN=off` | 登录页 + `/auth/gate` 口令网关；server.js 比对 `UI_PASSPHRASE`，过了把 `.env` 里 10 年期 `PB_TOKEN` + superuser 记录返回浏览器 |

**两个开关必须同步切**：注入开着时登录形同虚设；注入关着不登录就没数据。

**前端显示别名（`useAuthPb.js` 的 `DISPLAY_ALIAS`）**：登录防护模式下，PB superuser 真实邮箱（如 `sdk@phone-bridge.local`）会通过 `DISPLAY_ALIAS` 映射成对外显示的名字/邮箱。**只影响 React state.user 的 name/email 两个字段** —— PB authStore.record、token、API 调用、`state.user.id` 全部不变。要给新增 superuser 起对外别名时改这一个常量即可。

⚠️ **关键运维知识：改 PB superuser 的 email 会自动轮换该 record 的 `tokenKey`**，这会让 `.env` 里那个 10 年期 `PB_TOKEN`（JWT 签名跟 tokenKey 绑定）瞬间失效，gate 登录会返回 502。补救：要么从 Litestream 快照里取回原 `tokenKey` UPDATE 回去（保留旧 token），要么用密码重新签发一个新 PB_TOKEN 写回 `.env`。**对 superuser 记录做任何 PATCH 之前先想清楚。**

## 前端数据层（Smart-Trip 仓库，feature/pb-datasource 分支）

- `src/lib/dataSource.js` — 构建时数据源开关（`VITE_DATA_SOURCE=pb`）+ 登录开关
- `src/adapters/pbAdapter.js` — 读：PB trips/days/stops(+locations)/expenses → V2 UI 形状；
  全量拉取 + 内存缓存；时区显示链 stop.timezone → 浏览器本地（绝不回退 UTC）
- `src/adapters/pbWrites.js` — 写：差量同步器 `syncDayStopsToPb`（checkin/photos/note 填空/expenses upsert）、
  `createPbStop`（含 locations 按名复用）、`deletePbStops`（显式删除）、`/media` 上传
- `src/hooks/pb/*` — useAuth / useTripsV2 / useDays 的 PB 变体，模块级条件导出，UI 组件零改动
- 所有 UI 写入汇聚点：`saveDayToDB`（utils/dayHelpers）和 `updateDayStops`（hooks/pb/useDaysPb）

## 字段映射（与 SMARTNOTE_PROMPT 约定一致）

| UI 概念 | PB 落点 |
|---|---|
| 打卡时间（time/period/checkedIn） | `stops.checkin`（墙上时间按 stop.timezone 转 UTC）；无时区时补设备时区 |
| 照片（stop.photo） | `stops.photos` json 追加（最新在前），文件在 /media |
| 消费（price/expenseCategory） | `expenses` upsert（filter: stop + source=手动），amount_usd 客户端算 |
| stop 数量徽标/城市/相册 | 读侧聚合（expenses 合计、photos 第一张、locations 城市） |

## 已知边界（截至 3a）

- 新建 day（PB 没有的日期）尚不支持（3b）
- 行程/天的元数据编辑（标题/颜色/封面）本地生效、不写 PB（3b）
- Google 地点图片暂存外链 URL，未下载缓存（3b 改 /media 缓存）
- `days.pb.js` hook 绑错 collection（应为 expenses），现状无害，待 phone-bridge 侧自行修复
- media 文件夹尚无备份（Phase 4）
