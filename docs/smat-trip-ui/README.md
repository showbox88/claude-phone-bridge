# Smat Trip UI — phone-bridge 的旅行界面外挂

> **定位**：本 UI 是 phone-bridge 的一个外加功能。phone-bridge 是录入主入口
> （Claude 对话记录 + Notion 双向同步），Smat Trip UI 是同一份 PocketBase 数据的
> "旅行切片"可视化壳 + 旅途快捷操作面板（打卡 / 拍照 / 记账 / 浏览行程）。
>
> 代码仓库独立：`github.com/showbox88/Smart-Trip`（分支 `feature/pb-datasource`），
> 文档放在 phone-bridge 这边，因为数据、部署、运维都挂在 phone-bridge 生态下。

## 入口

| 项 | 值 |
|---|---|
| URL | `https://dashboard-server.tail4cfa2.ts.net:8451`（tailnet-only） |
| 登录 | 当前**免登录**（代理注入 PB token；登录代码保留，双开关可启用，见 ARCHITECTURE.md） |
| 数据 | phone-bridge 的 PocketBase（`127.0.0.1:8090`），collections：trips / days / stops / locations / expenses |
| 照片 | VM 文件夹 `/home/dev/smat-trip/media/`，PB 只存路径（`stops.photos` json） |

## 能做什么（Phase 3a，2026-06-10 验收通过）

- **浏览**：行程卡片 / 日历 / 每日行程 / Today 页 / 地图
- **打卡**：Today 页或 DayPage 附近打卡；新建 stop（自动建/复用 locations、写 checkin + 设备时区、标"打卡"分类）或给已有 stop 补打卡/改时间
- **拍照**：原图直传（≤25 MB 不压缩），路径写 `stops.photos`，最新在前
- **记账**：写 `expenses`（USD、amount_usd 客户端算、中文类别映射、source=手动）
- **删除 stop**：真删 PB 记录；关联 expenses 故意保留（财务记录不随 stop 消失）

## 数据安全护栏

- 差量写入：只 PATCH 变化字段，从不整行覆盖
- stop 备注只"填空"不覆盖（防止盖掉 Claude 写的备注）
- 删除不靠"数组缺失"推断，必须显式按钮触发
- `VITE_PB_WRITES=off` 重新构建即可整体退回只读
- PB 本身有 Litestream 实时副本 + 周加密归档兜底（见 `runbooks/pb-restore.md`）

## 与 Notion 的关系

UI 写 PB 后，phone-bridge 的 notion-sync（每天 03:00 / 15:00 ET）自动把变化推到
Notion；UI 新增的字段（如 stops.photos）**不在**同步映射里，Notion 端无感。
UI 删除 stop 会触发同步引擎的 Delete? 裁决流程（Sync Activity 出待确认行），属正常设计。

## 日常运维

```bash
# 服务状态 / 访问日志
sudo systemctl status smat-trip
journalctl -u smat-trip -f

# 发版（在 Smat Trip 仓库根目录，Windows 上）
npm run build:pb-vm
scp -r dist dashboard-server:/home/dev/smat-trip/   # server.js 没改就不用重启

# 出问题回退只读：.env.pb-vm 改 VITE_PB_WRITES=off → 重新构建 + scp
```

详细架构见 [ARCHITECTURE.md](./ARCHITECTURE.md)，路线图见 [PLAN.md](./PLAN.md)。
