# PocketBase Backend for Smart Note

本地 PocketBase 是 Smart Note 子系统（行程 / 地点 / 消费 / 打卡 / 美食 / 日记）的**唯一真相源**。Notion 作为"编辑面板"，由 `notion_sync.runner`（dashboard-server 上 03:00 ET 自动跑）与 PocketBase 双向同步 8 张表（trips / days / stops / plans / todos / contacts / locations / journal），冲突/删除走 Notion 的 Sync Activity 队列让用户裁决。详见 [`../docs/notion-pb-sync.md`](../docs/notion-pb-sync.md) 和 [`../docs/data-model.md`](../docs/data-model.md)。

## 当前部署（2026-05-27 上线）

| 项 | 值 |
|---|---|
| 主机 | `dashboard-server` (Debian 12, x86_64) |
| PB 版本 | v0.38.2 |
| 二进制 | `/opt/pocketbase/pocketbase` |
| 数据 | `/opt/pocketbase/pb_data/` |
| Service | `pocketbase.service` (systemd, User=dev, listens 127.0.0.1:8090) |
| 公网入口 | `https://dashboard-server.tail4cfa2.ts.net:8450/_/`（Tailscale Serve, tailnet only）|
| Service account | `showbox88@gmail.com`（密码在 `/home/dev/phone-bridge/.env` 的 `POCKETBASE_ADMIN_PASSWORD`）|

## 复制部署的步骤（从零到能用）

```bash
# 1. 下载二进制
sudo mkdir -p /opt/pocketbase/pb_data /opt/pocketbase/pb_migrations /opt/pocketbase/pb_hooks
sudo chown -R dev:dev /opt/pocketbase
cd /opt/pocketbase
curl -fLO https://github.com/pocketbase/pocketbase/releases/download/v0.38.2/pocketbase_0.38.2_linux_amd64.zip
python3 -c "import zipfile; zipfile.ZipFile('pocketbase_0.38.2_linux_amd64.zip').extract('pocketbase', '.')"
chmod +x pocketbase
rm pocketbase_0.38.2_linux_amd64.zip

# 2. 拷贝 migrations + hooks（从本 repo）
cp pb_migrations/*.js /opt/pocketbase/pb_migrations/
cp pb_hooks/days.pb.js /opt/pocketbase/pb_hooks/

# 3. systemd unit（写到 /etc/systemd/system/pocketbase.service，见下方）

# 4. 启动并创建首个 superuser
sudo systemctl enable --now pocketbase
sudo journalctl -u pocketbase --no-pager | grep pbinstall  # 取 install URL，浏览器开建 admin

# 5. 创建专用 service account 给 SDK 用
/opt/pocketbase/pocketbase superuser upsert showbox88@gmail.com '<strong-password>'

# 6. Tailscale Serve 暴露 admin UI 到 tailnet
sudo tailscale serve --bg --https=8450 http://localhost:8090
```

## systemd unit (`/etc/systemd/system/pocketbase.service`)

```ini
[Unit]
Description=PocketBase backend for Smart Note
After=network.target

[Service]
Type=simple
User=dev
Group=dev
WorkingDirectory=/opt/pocketbase
ExecStart=/opt/pocketbase/pocketbase serve --http=127.0.0.1:8090
Restart=on-failure
RestartSec=5
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
```

## Migrations

20+ 张表覆盖整个 Smart Note 子系统（不只是行程·地点·消费）。Migration 文件编号是 unix timestamp，所以执行顺序固定。

| 文件 | Collection | 字段亮点 |
|---|---|---|
| `1779465601_create_trips.js` | trips | title / date_start-end / status / type / budget |
| `1779465602_create_locations.js` | locations | name / type / rating(⭐) / **lat / lng / osm_id / amap_poi_id (后两者 unique idx)** |
| `1779465603_create_days.js` | days | date / amount / currency / rate / amount_usd / score(0-10) / trip-rel / location-rel / **actual_lat / actual_lng** *(注：上面这些事件字段在 21 里被搬走了)* |
| `1779465604_create_foods.js` | foods | dish / rating(❤️) / flavor(multi) / location-rel |
| `1779465605_create_journal.js` | journal | title / mood / type / tags(multi) / related_trip / related_day / **content (editor)** |
| `1779465606_extend_existing.js` | trips/locations/days/foods | **content (editor)** + locations 加 **fsq_id (unique)** |
| `1779465607_create_contacts.js` | contacts | name / email / phone / relationship / birthday / last_contact |
| `1779465608_create_todos.js` | todos | title / due_date / priority / status / executor / executor_ref_id |
| `1779465609_create_daily_briefing.js` | daily_briefing | title / date / type / status + 3 数字字段 + content |
| `1779465610_create_claude_memos.js` | claude_memos | title / category / priority / status / content |
| `1779465611_create_transactions.js` | transactions | description / amount / type / category / card / confirmation (unique on Gmail-source) |
| `1779465612_create_ideas.js` | ideas | title / category / status / tags + **self-relation `related_ideas`** (两步保存) |
| `1779465613_create_plans.js` | plans | title / category / progress / target_date + relation→ideas |
| `1779465614_link_trips_to_plans_contacts.js` | (alter trips) | 加 **`related_plan`→plans** + **`companions`→contacts (多选)** |
| `1779465615_create_pages.js` | pages | 独立长文笔记（个人 profile、roadmap、独立想法），可有 parent（自引用） |
| `1779465616_create_sync_meta.js` | sync_config + sync_global | Notion 同步运行的配置表（per-collection + 全局） |
| `1779465617_add_sync_pipeline_fields.js` | (alter 6 张同步表) | 加 `notion_id` / `notion_last_edited` / `last_synced_at` 三件套 |
| `1779465618_create_stops.js` | **stops** | 原子事件层：name / date / **categories(multi)** / amount / currency / rate / checkin / day-rel / trip-rel / location-rel / contact-rel / journal-rel + 管线字段。stops redesign 的主表 |
| `1779465619_extend_days_for_stops_migration.js` | (alter days) | days 加 `weather` 字段 + 临时 `migrated_to_stop_id`（数据迁移用） |
| `1779465620_extend_journal_for_stops.js` | (alter journal) | 加 `related_stop`、`type` 多 `Reminder` 选项、补齐管线字段（加入双向同步） |
| `1779465621_drop_legacy_days_fields.js` | (alter days) | days 删掉 12 个旧字段（reserved/checkin/amount/currency/rate/amount_usd/activity_type/score/location/actual_lat/actual_lng/migrated_to_stop_id），全部由 stops 表承载 |

**Schema 来源**：1-14 是 1:1 翻译自 Notion 同名库的初版；15+ 是 phone-bridge 自身演进（pages, sync 管线, stops redesign）。详细 schema 真相源在 [`../docs/data-model.md`](../docs/data-model.md)。Day.Amount(USD) 公式用 `pb_hooks/days.pb.js` 替代（stops redesign 之后这个钩子已经不被触发，但保留没动）。

## Hooks

`pb_hooks/days.pb.js`：替代 Notion 的 `Amount(USD)` formula。在 days 记录 create/update 时自动算 `amount_usd = amount * rate`（rate 空时 = amount 本身），保留 2 位小数。

⚠️ **PB v0.38 JS hook 坑**：每个 callback 跑在独立 goja VM，顶层 function 不共享。helper 必须**内联到每个 hook 内部**，不能抽顶层 function 复用（症状是静默失败 + 无日志）。

## 跟 phone-bridge 的集成

`server.py` 启动时认证一次，得到 token 存到 `os.environ["PB_TOKEN"]`，每 30 分钟后台 refresh。Claude SDK 在 chat/code 模式都用 Bash + curl 调本地 PB。

打卡处理流程详见根目录 [`CHECKIN.md`](../CHECKIN.md)。

## 备份策略（待办）

PB 数据是 SQLite 单文件 (`pb_data/data.db`)，备份简单：
- 短期：`cp data.db backups/data.$(date +%F).db` cron 每日
- 中期：Litestream 流式复制到 S3-like 存储
- 长期：等 Notion sync 上线后，Notion 本身也是备份（虽然 schema 有损）
