# Phase 5 · `notion_sync/runner.py` 拆解 + 算法升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拆 780 行 `notion_sync/runner.py` 成 4 个职责明确的文件；修两个 race condition；把硬编码的 linkage/icons 改成 declarative 走 `sync_config`；把性能瓶颈（relation_lookup 全表扫 8×8、frozen_pairs N 次查询）改成 lazy/group-by；日志轮转 + 错误带 ID；归档 7 个已完成的 migration scripts。

**Architecture:**
- **拆分边界**：`runner.py` 拆成 `bootstrap.py`（CLI + main + should_run_now）、`decisions.py`（apply_pending_decisions + _apply_one_decision）、`dispatch.py`（sync_collection + 4 个 _apply_* + ACTION_HANDLERS）、`post_phases.py`（cleanup_resolved_activity + notify_pending + alert state）。
- **数据流封装**：原本 `sync_collection` 内的 6+ 个跨函数参数（`field_types / overrides / overrides_inv / title_field / notion_schema / relation_lookup / relation_targets`）打包成 `SyncContext` dataclass。
- **Race condition 修复**：`Use Notion` decision 先 PATCH Notion 的 `last_synced_at`（让 Notion 的 last_edited_time 推进），再读 last_edited 回写 PB——保证下次 sync 不会因为反向覆盖把刚 applied 的 decision 当成新冲突。
- **配置 declarative 化**：原 `icons.py` 用 `if collection == 'days': ...` 长 if-elif 链 → 改成 `sync_config` 新增 `icon_field` / `icon_default` 列，运行时读配置。`linkage.py` 原硬编码列名 ("Date", "Day", "Trip", "Dates") → 改走 `field_map_overrides` 反查。
- **性能优化**：`build_relation_lookup` 现在 sync 开始时为所有 target collection 一次性拉全表（8×8=64 次 PB list）→ 改成 lazy（首次 used 时才拉单 collection）。`frozen_pairs_for_collection` 原循环里调用 → 改成 sync 开始一次性 group-by collection 取 frozen rows。
- **可观察性**：`apply_error` 日志现在缺 `pb_id`/`notion_id`，靠人工读 action 内部字段——改成 `_action_ids(a)` helper 一致提取；`sync.log` 改 `RotatingFileHandler(10MB × 5)` 防止磁盘炸盘。

**Tech Stack:** Python 3.11、SQLite (PocketBase)、Notion HTTP API、`@dataclass`、`logging.handlers.RotatingFileHandler`、pytest。无新 dep。

**Branch:** `refactor/phase-5-sync-runner` (已创建，从 `22fc4ad`)
**Parent spec:** [2026-06-06-refactor-roadmap.md](../specs/2026-06-06-refactor-roadmap.md) §Phase 5
**Roadmap 风险标识：** 中（同步是数据安全要害——任何 race 修不对会导致数据丢失/双写）

---

## File Structure (Target)

```
notion_sync/
  __init__.py
  runner.py                 # ≤ 50 行 thin shim，re-exports for systemd unit + scripts
  bootstrap.py              # 新：CLI argparse / load env / build clients / should_run_now / setup logging
  decisions.py              # 新：apply_pending_decisions + _apply_one_decision + _load_snap helper
  dispatch.py               # 新：sync_collection + 4 个 _apply_* + ACTION_HANDLERS + _action_ids
  post_phases.py            # 新：cleanup_resolved_activity + notify_pending + _render_pending_markdown + _alert_*
  context.py                # 新：SyncContext dataclass
  activity.py               # 不动
  backup.py                 # 不动
  changeset.py              # 改：frozen_pairs_for_all 加 group-by 模式
  codec.py                  # 不动
  config.py                 # 改：删 invalidate() 死 API
  icons.py                  # 改：icon_for(collection, row) 读 sync_config.icon_field
  linkage.py                # 改：update_date_linkages 走 field_map_overrides 反查
  logger.py                 # 改：sync.log handler 换 RotatingFileHandler
  notion_api.py             # 不动（Phase 3 已加退避）
  pb_api.py                 # 不动
  provisioner.py            # 改：sync_config schema 加 icon_field / icon_default + last_successful_run_at 列
  registry.snapshot.yaml    # 不改（用户手动 dump）
  transform.py              # 改：build_relation_lookup 改 lazy

scripts/
  archive/                  # 新目录
    README.md
    migrate_days_to_stops.py
    migrate_transactions_to_expenses.py
    migrate_stops_money_to_expenses.py
    cleanup_todo_titles.py
    backfill_location_timezones.py
    backfill_stop_timezones.py
    backfill_child_timezones.py
  reconcile_initial.py      # 不动
  dump_sync_registry.py     # 不动

tests/
  notion_sync/
    test_apply_decisions.py # 新：4 种 decision + race 修复测试
    test_context.py         # 新：SyncContext dataclass smoke
    test_action_handlers.py # 新：ACTION_HANDLERS 分派完整性
    test_changeset.py       # 已有
    test_config.py          # 已有
    test_icons.py           # 改：覆盖新 declarative path
    test_linkage.py         # 改：覆盖 overrides 反查
    test_provisioner.py     # 改：覆盖新 sync_config 列
    test_runner_guard.py    # 改：覆盖 ≥23h gate
```

**Out of scope (留给 Phase 6 / 后续):**
- structlog 引入（Phase 6）
- 全量 trace ID/contextvars（Phase 6）
- 同步算法纯函数化（保持 stateful PB+Notion 客户端注入模式）

---

## Pre-Flight Notes

### 调用者审计

`notion_sync/runner.py` 外部调用：
- systemd `notion-sync.service` 跑 `.venv/bin/python -m notion_sync.runner`
- `scripts/reconcile_initial.py` `from notion_sync.runner import sync_collection`
- `tests/notion_sync/test_runner_guard.py` `from notion_sync.runner import should_run_now`

**约束**：`runner.py` 必须保留 `sync_collection`、`should_run_now`、`main`、`apply_pending_decisions`、`cleanup_resolved_activity`、`notify_pending` 的 re-export，让上面 3 个 caller 不动一个字符。Phase 5 拆分之后 runner.py 变 thin shim。

### Race condition 完整描述

**Bug**：`apply_pending_decisions` 里 `Use Notion` 分支当前流程：
```python
# CURRENT (buggy)
current_page = nc.retrieve_page(notion_id)
notion_last_edited = current_page["last_edited_time"]
pb.update_record(collection, pb_id, notion_snap | {
    "notion_last_edited": notion_last_edited,
    "last_synced_at": now_iso_datetime(),
})
```

问题：PB 行用了 Notion 当前的 `last_edited_time`，但 Notion 这边什么都没动。下次 sync runner 来比对：
- PB.notion_last_edited == Notion.last_edited_time ✓
- 但 Notion.last_synced_at 字段没更新

→ 看起来 PB 这边的 last_synced_at 单边推进了，触发"PB-side change detected"，生成假 conflict。

**Fix**：先 PATCH Notion 的 `last_synced_at`（这会推进 last_edited_time），再读，再写 PB：
```python
# FIXED
page = nc.update_page(notion_id, properties={
    "last_synced_at": {"date": {"start": now_iso_date()}},
})
notion_last_edited = page["last_edited_time"]
pb.update_record(collection, pb_id, notion_snap | {
    "notion_last_edited": notion_last_edited,
    "last_synced_at": now_iso_datetime(),
})
```

PATCH 同时更新 Notion 的 `last_synced_at`，下次 sync 看 Notion/PB 两边都同时间，进入 NoChange 分支。

### should_run_now 跨小时漂移

**Bug**：原代码逻辑大致是 `now.hour == sync_hour_local`。问题：如果系统时钟漂移 / NTP 调整 / 服务重启在边界附近，可能跳过整轮 sync。

**Fix**：改"自上次成功 run 已过 ≥ 23h"窗口检测——记录每次 run 完成时的 timestamp 到 `sync_global.last_successful_run_at`，下次启动时 `now - last_successful_run_at > timedelta(hours=23)` 才跑。23h 而不是 24h 是为了让连续 N 天每天同小时跑时有 1h 缓冲。

### 验证策略
- **Per task**：相关单测 + smoke
- **后端 smoke**：`tests/smoke_backend.py`（确认 import path 没坏）
- **Sync 全套**：`pytest tests/notion_sync/ -v` 全过
- **Final**：`python -m notion_sync.runner --force-now` 跑 8 张表 + 看 Sync Activity 输出
- **Staging**：48h soak（让 hourly tick 至少跑 2 轮）

---

## Task 0: 预备 - baseline + import 审计 + 备份

**Files:** Create `docs/sync-baseline-2026-06-09.md` + `docs/sync-baseline-2026-06-09.yaml`

- [ ] **Step 1: 锁定测试基线**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/ -v 2>&1 | tail -15'
```

记录 pass count（baseline）。所有后续 task 都必须保持等同 pass。

- [ ] **Step 2: 跑 dump sync registry**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/python scripts/dump_sync_registry.py > /tmp/sync_registry_baseline.yaml'
scp dashboard-server:/tmp/sync_registry_baseline.yaml docs/sync-baseline-2026-06-09.yaml
```

这是 Phase 5 开始前的 `sync_config` 状态。所有 declarative 改动后跟它 diff 必须只多新增列（icon_field/icon_default/last_successful_run_at），不动既有数据。

- [ ] **Step 3: 跑一次 --force-now 8 表，抓 Sync Activity 输出**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && set -a; . ./.env; set +a; time .venv/bin/python -m notion_sync.runner --force-now 2>&1 | tail -40'
```

记录到 `docs/sync-baseline-2026-06-09.md`：
- run_start / run_end 之间的耗时（这是性能 baseline，目标 < 30s）
- 每张表的 `last_sync_summary` 字段值（多少 changed/new/conflict/vanished）

- [ ] **Step 4: Commit baseline doc**

```bash
git add docs/sync-baseline-2026-06-09.md docs/sync-baseline-2026-06-09.yaml
git commit -m "$(cat <<'EOF'
docs(phase5): record sync baseline pre-refactor

Snapshots for Phase 5 forensic comparison:
- sync_config registry state (8 enabled collections)
- --force-now run timing + per-collection last_sync_summary
- pytest tests/notion_sync/ pass count

Phase 5 changes must preserve this as-is (with the exception of new
icon_field/icon_default/last_successful_run_at columns added by
the provisioner update).
EOF
)"
```

---

## Task 1: `SyncContext` dataclass

**Files:**
- Create: `notion_sync/context.py`
- Create: `tests/notion_sync/test_context.py`

打包 6 个跨函数参数，降低 `sync_collection` / `apply_pending_decisions` / 4 个 `_apply_*` 的 signature 噪音。

- [ ] **Step 1: Write `notion_sync/context.py`**

```python
"""Sync execution context.

Bundle the 6+ piece of per-collection state that flows through the sync
pipeline. Replaces the long kwarg-list signatures that previously
threaded these through every `_apply_*` function.

Constructed once at the start of `sync_collection`; immutable for the
duration of one collection's sync.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyncContext:
    """Per-collection state for one sync pass.

    Built by `sync_collection` once it has resolved overrides + notion
    schema + relation indexes for the target collection.
    """
    collection: str
    field_types: dict
    overrides: dict           # PB field name → Notion column name
    overrides_inv: dict       # reverse: Notion column → PB field
    title_field: str
    notion_schema: dict
    # Lazy relation indexes — None until first use. After Task 9,
    # `relation_lookup` will be a LazyRelationLookup instance.
    relation_lookup: object = None  # LazyRelationLookup | dict | None
    relation_targets: dict | None = None


def make_context(*,
                 collection: str,
                 field_types: dict,
                 overrides: dict,
                 title_field: str,
                 notion_schema: dict,
                 relation_lookup: object = None,
                 relation_targets: dict | None = None) -> SyncContext:
    """Convenience constructor that computes overrides_inv."""
    return SyncContext(
        collection=collection,
        field_types=field_types,
        overrides=overrides,
        overrides_inv={v: k for k, v in overrides.items()},
        title_field=title_field,
        notion_schema=notion_schema,
        relation_lookup=relation_lookup,
        relation_targets=relation_targets,
    )
```

- [ ] **Step 2: Write `tests/notion_sync/test_context.py`**

```python
"""SyncContext dataclass smoke tests."""
import pytest

from notion_sync.context import SyncContext, make_context


def test_make_context_computes_overrides_inv():
    ctx = make_context(
        collection="days",
        field_types={"start": "Date"},
        overrides={"start": "Start"},
        title_field="title",
        notion_schema={"Start": {"type": "date"}},
    )
    assert ctx.overrides_inv == {"Start": "start"}


def test_context_is_frozen():
    ctx = make_context(
        collection="trips", field_types={}, overrides={},
        title_field="name", notion_schema={},
    )
    with pytest.raises(Exception):
        ctx.collection = "days"


def test_context_defaults_relation_to_none():
    ctx = make_context(
        collection="stops", field_types={}, overrides={},
        title_field="title", notion_schema={},
    )
    assert ctx.relation_lookup is None
    assert ctx.relation_targets is None
```

- [ ] **Step 3: Run tests**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/test_context.py -v 2>&1 | tail -10'
```

Expected: 3/3 pass.

- [ ] **Step 4: Commit**

```bash
git add notion_sync/context.py tests/notion_sync/test_context.py
git commit -m "$(cat <<'EOF'
refactor(notion_sync): add SyncContext dataclass

Phase 5 Task 1. Bundles the 6+ piece of per-collection state
(field_types/overrides/overrides_inv/title_field/notion_schema/
relation_lookup/relation_targets) into one frozen dataclass.

Not yet wired into runner.py — Task 14 wires sync_collection +
_apply_* functions to take a SyncContext.

3 tests cover overrides_inv, immutability, default-None relations.
EOF
)"
```

---

## Task 2: `_ACTION_ID_GETTERS` table replaces isinstance chain

**Files:**
- Modify: `notion_sync/runner.py` (replace `_action_ids` body)
- Create: `tests/notion_sync/test_action_handlers.py`

替换 `_action_ids` 内部的 8-branch isinstance 链。Table 是 `{Action class: (pb_id_getter, notion_id_getter)}`。

- [ ] **Step 1: Read current `_action_ids` + changeset dataclass shapes**

```bash
sed -n '121,145p' notion_sync/runner.py
grep -nA5 "^@dataclass\|^class " notion_sync/changeset.py
```

记下 8 个 Action 类的字段名（每个 dataclass 的 attribute），写测试时用准确名字。

- [ ] **Step 2: 改 `_action_ids` 实现**

`notion_sync/runner.py` 顶部 imports 后加：

```python
from notion_sync.changeset import (
    NoChange, PbOnlyChange, NotionOnlyChange, BothChanged,
    PbNew, NotionNew, NotionVanished, PbVanished,
)

# Action ID extraction table. Maps each Action class to a pair of
# (pb_id_getter, notion_id_getter) lambdas. Replaces a long isinstance
# chain so adding a new Action class fails loudly (test_every_action_class
# guards) instead of silently returning (None, None).
_ACTION_ID_GETTERS = {
    NoChange:        (lambda a: getattr(a, "pb_id", None), lambda a: getattr(a, "notion_id", None)),
    PbOnlyChange:    (lambda a: a.pb_id, lambda a: a.notion_id),
    NotionOnlyChange:(lambda a: a.pb_id, lambda a: a.notion_id),
    BothChanged:     (lambda a: a.pb_id, lambda a: a.notion_id),
    PbNew:           (lambda a: a.pb_id, lambda a: None),
    NotionNew:       (lambda a: None,    lambda a: a.notion_id),
    NotionVanished:  (lambda a: a.pb_id, lambda a: None),
    PbVanished:      (lambda a: None,    lambda a: a.notion_id),
}
```

替换原 `_action_ids` 函数体：
```python
def _action_ids(a) -> tuple[str | None, str | None]:
    """Return (pb_id, notion_id) for any Action. Returns (None, None)
    for unknown types (logged elsewhere)."""
    pair = _ACTION_ID_GETTERS.get(type(a))
    if pair is None:
        return (None, None)
    pb_getter, notion_getter = pair
    return (pb_getter(a), notion_getter(a))
```

**Adapt field names**: if changeset.py 里 `PbNew` 的字段叫 `pb_row` 不是 `pb_id`，把对应 lambda 改成 `lambda a: a.pb_row["id"]` 等等。Step 1 grep 出来的是真相。

- [ ] **Step 3: Write `tests/notion_sync/test_action_handlers.py`**

```python
"""_ACTION_ID_GETTERS dispatch + _action_ids coverage."""
from notion_sync.changeset import (
    NoChange, PbOnlyChange, NotionOnlyChange, BothChanged,
    PbNew, NotionNew, NotionVanished, PbVanished,
)
from notion_sync.runner import _action_ids, _ACTION_ID_GETTERS


def test_every_action_class_in_table():
    """No future Action class accidentally slips through ID extraction."""
    expected = {
        NoChange, PbOnlyChange, NotionOnlyChange, BothChanged,
        PbNew, NotionNew, NotionVanished, PbVanished,
    }
    assert set(_ACTION_ID_GETTERS.keys()) == expected


def test_action_ids_unknown_returns_none_tuple():
    class FakeAction:
        pass
    assert _action_ids(FakeAction()) == (None, None)


# Per-Action tests — adapt construction args to actual dataclass fields.
# Step 1 of Task 2 shows the real signatures via grep on changeset.py.
# Below is a template; the implementer adjusts to actual fields.

def test_action_ids_pb_only_change():
    # Construct with whatever args changeset.PbOnlyChange's __init__ wants.
    # Most likely: pb_id, notion_id, pb_row, notion_page (or similar)
    pass  # IMPLEMENTER: fill after reading changeset.py
```

The implementer reads changeset.py first, sees the real fields, and writes 6-8 concrete cases. Don't ship `pass` — those need real assertions.

- [ ] **Step 4: Run tests**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/test_action_handlers.py tests/notion_sync/test_changeset.py -v 2>&1 | tail -15'
```

Expected: existing test_changeset still all green + new tests pass.

- [ ] **Step 5: Commit**

```bash
git add notion_sync/runner.py tests/notion_sync/test_action_handlers.py
git commit -m "$(cat <<'EOF'
refactor(notion_sync): _action_ids uses _ACTION_ID_GETTERS table

Phase 5 Task 2. Replaces the 8-branch isinstance chain in _action_ids
with a {Action class: (pb_getter, notion_getter)} dict lookup. New
Action types must be added to the table or _action_ids returns
(None, None) — test_every_action_class_in_table guards.

The sync_collection apply dispatch still uses inline isinstance chains
for now (Task 14 will move it to ACTION_HANDLERS = {cls: apply_fn}).
EOF
)"
```

---

## Task 3: Fix `Use Notion` race + add `test_apply_decisions.py`

**Files:**
- Modify: `notion_sync/runner.py:_apply_one_decision` (`Use Notion` branch)
- Create: `tests/notion_sync/test_apply_decisions.py`

数据安全要害——TDD 写测先红再绿。

- [ ] **Step 1: 写 failing test 描述当前 race**

`tests/notion_sync/test_apply_decisions.py`:

```python
"""Coverage for apply_pending_decisions race + 4 decision paths.

Phase 5 Task 3. Use Notion race fix MUST be characterized by a test
first to lock in correct behavior.

Bug: current code reads Notion's last_edited_time WITHOUT first
patching Notion, then writes that timestamp into PB along with the
notion_snap. Next sync run sees PB.last_synced_at advance but
Notion.last_synced_at unchanged → false conflict.

Fix: PATCH Notion's last_synced_at first, then read the resulting
last_edited_time, then write PB. Both sides end up time-synced.
"""
from unittest.mock import MagicMock

from notion_sync.runner import _apply_one_decision


def _make_row(decision: str, *, pb_id: str = "pb1", notion_id: str = "n1",
              notion_snap: str = '{"name": "alpha"}',
              pb_snap: str = '{"id": "pb1", "name": "alpha"}') -> dict:
    """Construct a Sync Activity row with the given decision + snapshots."""
    return {
        "id": "sa_row_id",
        "properties": {
            "decision":  {"select": {"name": decision}},
            "pb_id":     {"rich_text": [{"plain_text": pb_id}]},
            "notion_id": {"rich_text": [{"plain_text": notion_id}]},
            "notion_snapshot": {"rich_text": [{"plain_text": notion_snap}]},
            "pb_snapshot":     {"rich_text": [{"plain_text": pb_snap}]},
        },
    }


def test_use_notion_patches_notion_before_writing_pb():
    """The race fix: nc.update_page must run BEFORE pb.update_record,
    AND pb receives the updated last_edited_time from update_page's response."""
    pb = MagicMock()
    nc = MagicMock()
    nc.update_page.return_value = {
        "id": "n1",
        "last_edited_time": "2026-06-09T10:00:00.000Z",
    }
    row = _make_row("Use Notion", notion_snap='{"name": "alpha"}')

    _apply_one_decision(
        row, pb=pb, nc=nc, collection="trips",
        field_types={}, overrides={}, overrides_inv={},
        title_field="name", notion_schema={},
    )

    # update_page called with last_synced_at property
    assert nc.update_page.called
    update_args = nc.update_page.call_args
    props = update_args.kwargs.get("properties") or (
        update_args.args[1] if len(update_args.args) > 1 else {})
    assert "last_synced_at" in props

    # pb.update_record received the new last_edited_time
    assert pb.update_record.called
    pb_call = pb.update_record.call_args
    # data is the 3rd positional arg or a kwarg
    if len(pb_call.args) >= 3:
        data = pb_call.args[2]
    else:
        data = pb_call.kwargs.get("data") or pb_call.kwargs
    assert data.get("notion_last_edited") == "2026-06-09T10:00:00.000Z"


def test_use_pb_writes_both_sides(monkeypatch):
    """Use PB pushes PB snapshot back to Notion + records last_synced_at."""
    pb = MagicMock()
    nc = MagicMock()
    nc.update_page.return_value = {"id": "n1", "last_edited_time": "2026-06-09T11:00:00.000Z"}

    # Stub transform.pb_record_to_notion_props so we don't need full schema
    from notion_sync import runner as runner_mod
    monkeypatch.setattr(runner_mod, "pb_record_to_notion_props",
                        lambda *a, **kw: {"Title": {"title": [{"text": {"content": "alpha"}}]}})

    row = _make_row("Use PB", pb_snap='{"id": "pb1", "name": "alpha"}')

    _apply_one_decision(
        row, pb=pb, nc=nc, collection="trips",
        field_types={"name": "text"},
        overrides={}, overrides_inv={},
        title_field="name",
        notion_schema={"Title": {"type": "title"}},
    )

    assert nc.update_page.called
    assert pb.update_record.called


def test_delete_both_calls_both_sides_idempotently():
    pb = MagicMock()
    nc = MagicMock()
    pb.delete_record.side_effect = Exception("already gone")  # tolerated
    row = _make_row("Delete both")

    # Should NOT raise even though delete_record errors
    _apply_one_decision(
        row, pb=pb, nc=nc, collection="trips",
        field_types={}, overrides={}, overrides_inv={},
        title_field="name", notion_schema={},
    )

    pb.delete_record.assert_called_once_with("trips", "pb1")
    nc.update_page.assert_called_once_with("n1", archived=True)


def test_keep_both_is_a_noop():
    pb = MagicMock()
    nc = MagicMock()
    row = _make_row("Keep both")

    _apply_one_decision(
        row, pb=pb, nc=nc, collection="trips",
        field_types={}, overrides={}, overrides_inv={},
        title_field="name", notion_schema={},
    )

    assert not pb.update_record.called
    assert not pb.delete_record.called
    assert not nc.update_page.called
```

- [ ] **Step 2: Run — confirm `test_use_notion_patches_notion_before_writing_pb` FAILS**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/test_apply_decisions.py::test_use_notion_patches_notion_before_writing_pb -v 2>&1 | tail -10'
```

Expected: FAIL. The current code calls `nc.retrieve_page` not `nc.update_page`. The assertion `nc.update_page.called` is False.

- [ ] **Step 3: Fix `Use Notion` branch in `notion_sync/runner.py`**

Find the `if decision == "Use Notion":` block (around line 330):

```python
    if decision == "Use Notion":
        notion_snap = _load_snap("notion_snapshot")
        if not pb_id or not notion_id or not notion_snap:
            raise RuntimeError("Use Notion requires both IDs + notion_snapshot")
        # PATCH Notion's last_synced_at FIRST so its last_edited_time
        # advances. Reading retrieve_page() without patching first leaves
        # PB with the old timestamp; the next sync sees PB ahead and flags
        # a false conflict.
        try:
            page = nc.update_page(notion_id, properties={
                "last_synced_at": {"date": {"start": now_iso_date()}},
            })
            notion_last_edited = page.get("last_edited_time", "")
        except Exception:
            notion_last_edited = ""
        pb.update_record(collection, pb_id, notion_snap | {
            "notion_last_edited": notion_last_edited,
            "last_synced_at": now_iso_datetime(),
        })
        log_event("decision_applied", collection=collection,
                  decision=decision, pb_id=pb_id, notion_id=notion_id)
        return
```

- [ ] **Step 4: Run tests — all 4 should PASS**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/test_apply_decisions.py -v 2>&1 | tail -10'
```

Expected: 4/4 pass.

- [ ] **Step 5: Run full sync test suite (preserve baseline)**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/ -v 2>&1 | tail -15'
```

Expected: Task 0 baseline pass count + 4 new test_apply_decisions + 3 new test_context + N new test_action_handlers = all green.

- [ ] **Step 6: Commit**

```bash
git add notion_sync/runner.py tests/notion_sync/test_apply_decisions.py
git commit -m "$(cat <<'EOF'
fix(notion_sync): Use Notion decision PATCHes Notion before reading last_edited

Phase 5 Task 3. Race condition fix.

Legacy code read Notion's last_edited_time WITHOUT first patching
anything, then wrote that timestamp into PB along with the notion_snap.
Next sync run saw PB.last_synced_at advance but Notion.last_synced_at
unchanged → false conflict reappearing in Sync Activity.

Fix: PATCH Notion's last_synced_at first (which advances its
last_edited_time), then read the new last_edited from the response,
then write PB. Both sides end up timestamped at the same sync pass.

4 new tests cover Use Notion race, Use PB, Delete both idempotency,
Keep both no-op. The race-fix test fails on legacy and passes after.
EOF
)"
```

---

## Task 4: `linkage.py` walks `field_map_overrides` instead of hardcoded names

**Files:**
- Modify: `notion_sync/linkage.py`
- Modify: `tests/notion_sync/test_linkage.py`

原 `update_date_linkages` 硬编码 "Date" / "Day" / "Trip" / "Dates" Notion 列名——加新表得动代码。改成从 `field_map_overrides` 反查。

- [ ] **Step 1: Read current `update_date_linkages`**

```bash
sed -n '62,133p' notion_sync/linkage.py
```

理解里面对硬编码列名的使用。

- [ ] **Step 2: 改签名 + 实现**

```python
def update_date_linkages(
    pb, nc, *,
    collection: str,
    notion_db_id: str,
    overrides: dict | None = None,
) -> int:
    """Update parent-relation linkages (Day/Trip/Dates) based on the
    Date field of each page.

    Column names are resolved via `overrides` (PB field → Notion column).
    If overrides doesn't map a field, falls back to the title-cased PB
    name (legacy default).
    """
    overrides = overrides or {}
    col_date  = overrides.get("date",  "Date")
    col_day   = overrides.get("day",   "Day")
    col_trip  = overrides.get("trip",  "Trip")
    col_dates = overrides.get("dates", "Dates")
    # ... rest of legacy logic using these resolved variables instead of literals
```

调用方在 `runner.py:sync_collection` 处补 `overrides=cfg_row.get("field_map_overrides") or {}` 参数（Task 14 split 时统一改）。**临时 hack**: 在 Task 4 commit 里调用方暂用 `overrides=cfg_row.get("field_map_overrides") or {}` 直接传值；Task 14 切到 SyncContext 时统一。

- [ ] **Step 3: 加 test case**

`tests/notion_sync/test_linkage.py` 加：

```python
from unittest.mock import MagicMock

from notion_sync.linkage import update_date_linkages


def test_update_date_linkages_uses_overrides_for_column_name():
    """When overrides maps a PB field to a custom Notion column, linkage
    queries that column, not the hardcoded default."""
    pb = MagicMock()
    nc = MagicMock()
    nc.query_database.return_value = []  # no rows to update
    pb.list_all.return_value = []        # no trips to link to

    update_date_linkages(pb, nc,
                        collection="trips",
                        notion_db_id="db1",
                        overrides={"date": "Departure"})

    # Inspect nc.query_database call to confirm it filtered/sorted on
    # 'Departure' rather than 'Date'.
    args = nc.query_database.call_args
    serialized = repr(args)
    assert "Departure" in serialized or "Departure" in str(args.kwargs)
```

- [ ] **Step 4: Run tests**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/test_linkage.py -v 2>&1 | tail -10'
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add notion_sync/linkage.py notion_sync/runner.py tests/notion_sync/test_linkage.py
git commit -m "$(cat <<'EOF'
refactor(notion_sync): linkage resolves column names via field_map_overrides

Phase 5 Task 4. update_date_linkages no longer hardcodes 'Date'/'Day'/
'Trip'/'Dates' literals. Reads overrides dict first, falls back to
title-cased PB field name (legacy default).

To add a sync target with renamed Notion columns: set overrides in
sync_config — no code change.
EOF
)"
```

---

## Task 5: `icons.py` declarative via `sync_config.icon_field` / `icon_default`

**Files:**
- Modify: `notion_sync/icons.py` (`icon_for` accepts new kwargs)
- Modify: `notion_sync/provisioner.py` (add icon_field/icon_default to schema)
- Modify: `tests/notion_sync/test_icons.py`
- Modify: `tests/notion_sync/test_provisioner.py`

- [ ] **Step 1: 改 `icon_for(collection, row, *, icon_field=None, icon_default=None)`**

`notion_sync/icons.py` 修改 `icon_for`：

```python
def icon_for(collection: str, row: dict, *,
             icon_field: str | None = None,
             icon_default: str | None = None) -> dict | None:
    """Resolve a Notion icon for a row.

    For the 6 legacy-supported collections (days/trips/stops/todos/
    foods/expenses), uses the domain-aware mappings below. For any
    other collection:
      - if icon_field is set: look up row[icon_field] and emit that emoji
      - else if icon_default: emit it
      - else: None
    """
    # Legacy domain mappings preserved
    if collection == "days": return icon_for_day()
    if collection == "trips": return icon_for_trip()
    if collection == "stops": return icon_for_stop(row.get("category"))
    if collection == "todos": return icon_for_todo(row)
    if collection == "foods": return icon_for_food(row)
    if collection == "expenses": return icon_for_expense(row.get("expense_category"))
    # Declarative path for newer collections
    if icon_field:
        v = row.get(icon_field)
        if v:
            return _emoji(str(v)[:2])
    if icon_default:
        return _emoji(icon_default)
    return None
```

- [ ] **Step 2: 改 `provisioner.py:provision_new_target`**

在 `sync_config` schema bootstrap 加两列（如果还没有）：

```python
# Inside provision_new_target or wherever sync_config schema is set up:
# Add columns (idempotent): icon_field (text), icon_default (text)
```

注意：现有 `sync_config` rows 不能丢失数据，新增列即可。

- [ ] **Step 3: 改 `runner.py` 调用 `icon_for`**

`sync_collection` 里读 `cfg_row.get("icon_field")` / `cfg_row.get("icon_default")` 并传到每个 `icon_for(...)` 调用点。

- [ ] **Step 4: 测试**

`tests/notion_sync/test_icons.py` 加 declarative case:

```python
def test_icon_for_unknown_collection_uses_icon_default():
    res = icon_for("custom_table", {"foo": "bar"}, icon_default="📦")
    assert res == _emoji("📦")


def test_icon_for_unknown_collection_uses_icon_field():
    res = icon_for("custom_table", {"emoji": "🐶"}, icon_field="emoji")
    assert res is not None
    # Should contain the dog emoji prefix
```

`tests/notion_sync/test_provisioner.py` 加 case：sync_config 创建后包含 icon_field / icon_default 列（even 为 NULL 也 OK）。

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/test_icons.py tests/notion_sync/test_provisioner.py -v 2>&1 | tail -10'
```

Expected: legacy tests pass + new declarative path covered.

- [ ] **Step 5: Commit**

```bash
git add notion_sync/icons.py notion_sync/provisioner.py notion_sync/runner.py tests/notion_sync/test_icons.py tests/notion_sync/test_provisioner.py
git commit -m "$(cat <<'EOF'
refactor(notion_sync): icons declarative via sync_config.icon_field/default

Phase 5 Task 5. icon_for accepts icon_field/icon_default kwargs.
6 legacy collections (days/trips/stops/todos/foods/expenses) keep
their domain mappings; new collections fall through to the
declarative path: read row[icon_field] OR icon_default.

provisioner.provision_new_target adds icon_field + icon_default
columns to sync_config schema on bootstrap.
EOF
)"
```

---

## Task 6: `sync.log` `RotatingFileHandler`

**Files:** Modify `notion_sync/logger.py`

- [ ] **Step 1: Read current `logger.py`**

```bash
cat notion_sync/logger.py
```

- [ ] **Step 2: 改 file handler**

如果当前 logger 直接用 `open().write()` 不走 logging module，改成 stdlib logging。如果走 `FileHandler`，换成 `RotatingFileHandler`：

```python
from logging.handlers import RotatingFileHandler

# replace existing handler with:
_handler = RotatingFileHandler(
    str(LOG_PATH),
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,               # sync.log + sync.log.1 .. .5
    encoding="utf-8",
)
```

保持原有 formatter / log_event() 接口不变。

- [ ] **Step 3: Sanity check**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/python -c "from notion_sync.logger import log_event; log_event(\"test_event\", foo=\"bar\"); print(\"OK\")"'
```

应该 print OK 不报错。

- [ ] **Step 4: Commit**

```bash
git add notion_sync/logger.py
git commit -m "refactor(notion_sync): sync.log rotates at 10MB × 5 files

Phase 5 Task 6. Use logging.handlers.RotatingFileHandler so sync.log
never grows unbounded. 10MB × 5 = 50MB max disk."
```

---

## Task 7: `apply_error` always carries `pb_id` + `notion_id`

**Files:** Modify `notion_sync/runner.py` (every `log_event("apply_error", ...)` site)

- [ ] **Step 1: 搜所有 apply_error 调用点**

```bash
grep -nE 'log_event\("apply_error' notion_sync/runner.py
```

- [ ] **Step 2: 每个调用点都加 ID kwargs**

模式：
```python
# before
log_event("apply_error", collection=collection, action=type(a).__name__, error=str(e))
# after
pb_id, notion_id = _action_ids(a)
log_event("apply_error", collection=collection, action=type(a).__name__,
          pb_id=pb_id, notion_id=notion_id, error=str(e))
```

`decision_apply_error` 已经从 row 里取 ID，保持不变。

- [ ] **Step 3: Commit**

```bash
git add notion_sync/runner.py
git commit -m "feat(notion_sync): apply_error logs include pb_id + notion_id

Phase 5 Task 7. Uses _action_ids() helper (from Task 2 table) so every
apply_error log line includes the IDs needed to find the offending row
in PB or Notion. Lets us grep sync.log by either ID."
```

---

## Task 8: `frozen_pairs_for_collection` group-by 优化

**Files:**
- Modify: `notion_sync/changeset.py` (add `frozen_pairs_for_all`)
- Modify: `notion_sync/runner.py:main` (call group-by version once)

原 8 个 collection sync × 1 frozen_pairs query = 8 queries. 改成一次 group-by 拉。

- [ ] **Step 1: Read current `frozen_pairs_for_collection`**

```bash
grep -nA20 "def frozen_pairs_for_collection" notion_sync/changeset.py
```

- [ ] **Step 2: 加 `frozen_pairs_for_all`**

```python
def frozen_pairs_for_all(nc, collections: list[str]) -> dict[str, tuple[set, set]]:
    """One-shot group-by version of frozen_pairs_for_collection.

    Returns {collection: (frozen_pb_ids, frozen_notion_ids)}. Single
    Notion query filtered by collection IN [...], grouped in Python.
    """
    if not collections:
        return {}
    db_id = settings.notion_sync_activity_db_id
    filt = {"and": [
        {"or": [{"property": "collection", "select": {"equals": c}}
                for c in collections]},
        {"property": "applied_at", "date": {"is_empty": False}},
    ]}
    rows = nc.query_database(db_id, filter_=filt)
    result: dict[str, tuple[set, set]] = {c: (set(), set()) for c in collections}
    for row in rows:
        props = row.get("properties", {})
        c = (props.get("collection", {}).get("select") or {}).get("name", "")
        pb_id = "".join(rt.get("plain_text", "") for rt in props.get("pb_id", {}).get("rich_text", []))
        notion_id = "".join(rt.get("plain_text", "") for rt in props.get("notion_id", {}).get("rich_text", []))
        if c in result:
            if pb_id: result[c][0].add(pb_id)
            if notion_id: result[c][1].add(notion_id)
    return result
```

保留原 `frozen_pairs_for_collection` 函数定义（不删，可能 reconcile_initial 还在用）。

- [ ] **Step 3: 改 `runner.py:main` 调用**

```python
# Before enumerating cfg_rows for sync:
collections = [r["collection"] for r in enabled_cfg_rows]
frozen_all = frozen_pairs_for_all(nc, collections)

# Pass into each sync_collection:
for cfg_row in enabled_cfg_rows:
    frozen = frozen_all.get(cfg_row["collection"], (set(), set()))
    sync_collection(cfg_row, pb, nc, frozen_pairs=frozen)
```

`sync_collection` 接受新可选 kwarg `frozen_pairs=None`. None 时回退到 `frozen_pairs_for_collection` 单查（向后兼容）.

- [ ] **Step 4: Test**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/test_changeset.py -v 2>&1 | tail -5'
```

Expected: existing tests pass. Optionally add a quick test for `frozen_pairs_for_all` shape.

- [ ] **Step 5: Commit**

```bash
git add notion_sync/changeset.py notion_sync/runner.py
git commit -m "perf(notion_sync): frozen_pairs single group-by query

Phase 5 Task 8. New frozen_pairs_for_all(nc, collections) makes one
Notion query filtered by 'collection IN [...]' and groups in Python,
replacing N individual queries.

8 collections → 7 fewer queries per sync run. main() pre-fetches at
start; sync_collection accepts frozen_pairs kwarg (None falls back
to legacy single-collection query for back-compat)."
```

---

## Task 9: `relation_lookup` lazy

**Files:**
- Modify: `notion_sync/transform.py` (add `LazyRelationLookup` class)
- Modify: `notion_sync/runner.py` (uses lazy in sync_collection)

- [ ] **Step 1: Read current `build_relation_lookup`**

```bash
grep -nA20 "def build_relation_lookup" notion_sync/transform.py
```

- [ ] **Step 2: 加 `LazyRelationLookup`**

```python
class LazyRelationLookup:
    """Memoized per-target relation index.

    Replaces eager build_relation_lookup. First call to `get(target)`
    triggers a PB list_all for that target; subsequent calls return
    cached. Collections without relation columns incur zero fetches.
    """
    def __init__(self, pb):
        self._pb = pb
        self._cache: dict[str, dict] = {}

    def get(self, target_collection: str) -> dict:
        if target_collection not in self._cache:
            rows = self._pb.list_all(target_collection)
            self._cache[target_collection] = {r["id"]: r for r in rows}
        return self._cache[target_collection]


# Back-compat shim for reconcile_initial.py and other callers wanting
# a pre-baked dict:
def build_relation_lookup(pb, target_names: list[str]) -> dict[str, dict]:
    lazy = LazyRelationLookup(pb)
    return {name: lazy.get(name) for name in target_names}
```

- [ ] **Step 3: 改 `sync_collection`**

不再 eager build_relation_lookup。改成传 `LazyRelationLookup(pb)` 给 `_apply_*` 调用点。`_apply_*` 内部需要 relation 时调 `lookup.get(target)`.

注意：`_apply_*` 函数当前的 `relation_lookup: dict` 参数现在变成 `relation_lookup: LazyRelationLookup`. 内部 `relation_lookup[target]` 改 `relation_lookup.get(target)`. Search for that pattern:

```bash
grep -n "relation_lookup\[" notion_sync/runner.py notion_sync/transform.py
```

- [ ] **Step 4: Test**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/ -v 2>&1 | tail -15'
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add notion_sync/transform.py notion_sync/runner.py
git commit -m "perf(notion_sync): relation_lookup lazy (LazyRelationLookup class)

Phase 5 Task 9. Replaces eager build_relation_lookup (which fetched
all 8 relation targets on every sync run = 64 list_all calls) with
LazyRelationLookup that fetches each target only on first .get().

Collections without relation columns now incur zero relation fetches.

build_relation_lookup kept as a back-compat shim for reconcile_initial.py."
```

---

## Task 10: `should_run_now` ≥23h gate

**Files:**
- Modify: `notion_sync/runner.py:should_run_now`
- Modify: `notion_sync/runner.py:main` (write last_successful_run_at on success)
- Modify: `notion_sync/provisioner.py` (add column)
- Modify: `tests/notion_sync/test_runner_guard.py`

- [ ] **Step 1: 在 sync_global 加 `last_successful_run_at` 列**

`provisioner.py` 里的 sync_global schema bootstrap 加 `last_successful_run_at` (date, optional)。已有 sync_global row 通过 PB admin / migration 手动加。

- [ ] **Step 2: 改 `should_run_now`**

```python
def should_run_now(sync_global: dict, *, now_utc: datetime | None = None) -> bool:
    """Decide whether to run this hourly tick.

    Returns True iff:
    - sync_global.paused is not True
    - current hour in sync_global.timezone == sync_hour_local
    - AND it's been ≥ 23h since last_successful_run_at
      (prevents double-run within wall-clock day; tolerates clock drift)
    """
    if sync_global.get("paused"):
        return False
    now = now_utc or datetime.now(timezone.utc)
    tz_name = sync_global.get("timezone") or "UTC"
    try:
        local_now = now.astimezone(ZoneInfo(tz_name))
    except Exception:
        return False  # bad timezone — skip silently
    sync_hour = int(sync_global.get("sync_hour_local", 9))
    if local_now.hour != sync_hour:
        return False
    last_run = sync_global.get("last_successful_run_at")
    if last_run:
        try:
            last_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            if now - last_dt < timedelta(hours=23):
                return False
        except ValueError:
            pass  # malformed — treat as no last run
    return True
```

- [ ] **Step 3: `main()` writes on successful completion**

```python
# Near the end of main(), after all sync_collection / post_phases done:
try:
    pb.update_record("sync_global", sync_global_row["id"], {
        "last_successful_run_at": now_iso_datetime(),
    })
except Exception as e:
    log_event("last_run_update_failed", error=str(e))
```

- [ ] **Step 4: 加 test cases**

```python
from datetime import datetime, timedelta, timezone

def test_should_run_now_false_within_23h_of_last_run():
    now = datetime(2026, 6, 9, 9, 30, tzinfo=timezone.utc)
    last = (now - timedelta(hours=2)).isoformat()
    cfg = {"sync_hour_local": 9, "timezone": "UTC", "last_successful_run_at": last}
    assert should_run_now(cfg, now_utc=now) is False


def test_should_run_now_true_after_23h_gap():
    now = datetime(2026, 6, 9, 9, 30, tzinfo=timezone.utc)
    last = (now - timedelta(hours=24)).isoformat()
    cfg = {"sync_hour_local": 9, "timezone": "UTC", "last_successful_run_at": last}
    assert should_run_now(cfg, now_utc=now) is True


def test_should_run_now_true_no_last_run_recorded():
    """First-ever run: no last_successful_run_at set → run if hour matches."""
    now = datetime(2026, 6, 9, 9, 30, tzinfo=timezone.utc)
    cfg = {"sync_hour_local": 9, "timezone": "UTC"}
    assert should_run_now(cfg, now_utc=now) is True


def test_should_run_now_false_wrong_hour():
    now = datetime(2026, 6, 9, 10, 30, tzinfo=timezone.utc)
    cfg = {"sync_hour_local": 9, "timezone": "UTC"}
    assert should_run_now(cfg, now_utc=now) is False
```

- [ ] **Step 5: Run + commit**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/test_runner_guard.py -v 2>&1 | tail -10'
git add notion_sync/runner.py notion_sync/provisioner.py tests/notion_sync/test_runner_guard.py
git commit -m "fix(notion_sync): should_run_now adds ≥23h-since-last-success gate

Phase 5 Task 10. Replaces brittle 'now.hour == sync_hour' single gate
with combined 'matches hour AND ≥23h since last_successful_run_at'.

Prevents double-run within the same wall-clock day (quick restart) and
tolerates clock drift across hour boundaries.

main() writes last_successful_run_at on successful completion."
```

---

## Task 11: 删 `config.invalidate()` 死 API

**Files:** Modify `notion_sync/config.py`; verify zero callers.

- [ ] **Step 1: Check callers**

```bash
grep -rn "config\.invalidate\|invalidate()" notion_sync/ scripts/ tests/ app/
```

- [ ] **Step 2: Delete + commit**

If zero callers:
```bash
# Remove the function definition from config.py
git add notion_sync/config.py
git commit -m "chore(notion_sync): remove unused config.invalidate() API

Phase 5 Task 11. Dead code — never called. Configuration is loaded
fresh from PB on each runner main() invocation; no cache invalidation
needed."
```

If there ARE callers: keep the function but rename to something more honest (e.g. `_invalidate_for_test_only`) and ensure caller flow isn't broken. Document in the commit message why we kept it.

---

## Task 12: 修 `notify_pending` 反向依赖

**Files:** Modify `notion_sync/runner.py:notify_pending`.

- [ ] **Step 1: Read current import hack**

```bash
sed -n '664,712p' notion_sync/runner.py
```

`notify_pending` 内大约长这样:
```python
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
import db as _db
```

- [ ] **Step 2: 改成清晰 import**

```python
# At top of runner.py (or notify_pending's host module after Task 14 split):
from app.paths import DATA_DIR
import sqlite3

# In notify_pending:
def notify_pending(nc: NotionClient) -> int:
    """..."""
    db_path = DATA_DIR / "bridge.db"
    if not db_path.exists():
        return 0
    con = sqlite3.connect(str(db_path))
    try:
        # ... existing logic, but use `con` directly
        cur = con.execute("SELECT id, title, ... FROM sessions WHERE ...")
        rows = cur.fetchall()
        # ... rest
    finally:
        con.close()
    return notified
```

Remove the `sys.path.insert(0, ...)` line and `import db as _db`.

- [ ] **Step 3: Sanity**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && .venv/bin/python -c "from notion_sync.runner import notify_pending; print(notify_pending.__name__)"'
```

Expected: prints `notify_pending`.

- [ ] **Step 4: Commit**

```bash
git add notion_sync/runner.py
git commit -m "refactor(notion_sync): notify_pending uses app.paths.DATA_DIR not sys.path hack

Phase 5 Task 12. Removes the sys.path.insert + dynamic import db
pattern. Uses app.paths.DATA_DIR + sqlite3 directly. Cleaner module
boundary; notion_sync no longer reaches into app.* by abusing sys.path."
```

---

## Task 13: 归档 7 个 backfill / migration scripts

**Files:** `git mv` 7 scripts.

- [ ] **Step 1: 归档**

```bash
mkdir -p scripts/archive
git mv scripts/migrate_days_to_stops.py scripts/archive/
git mv scripts/migrate_transactions_to_expenses.py scripts/archive/
git mv scripts/migrate_stops_money_to_expenses.py scripts/archive/
git mv scripts/cleanup_todo_titles.py scripts/archive/
git mv scripts/backfill_location_timezones.py scripts/archive/
git mv scripts/backfill_stop_timezones.py scripts/archive/
git mv scripts/backfill_child_timezones.py scripts/archive/
ls scripts/archive/  # confirm 7 files
```

- [ ] **Step 2: 加 `scripts/archive/README.md`**

```markdown
# Archived migration scripts

These ran once and aren't expected to run again. Kept for forensic
reference + pattern examples for future migrations.

| Script | What it did | Phase |
|---|---|---|
| migrate_days_to_stops.py | Split days table into days+stops | 2026-06-03 stops redesign |
| migrate_transactions_to_expenses.py | Reshape transactions → expenses child of stops/days | 2026-06-05 expenses redesign |
| migrate_stops_money_to_expenses.py | Move money fields from stops → expenses | 2026-06-05 |
| cleanup_todo_titles.py | One-off title normalization on todos | (manual cleanup) |
| backfill_location_timezones.py | Set timezone on existing locations rows | 2026-06-05 timezone design |
| backfill_stop_timezones.py | Same, for stops | 2026-06-05 |
| backfill_child_timezones.py | Same, for days/expenses/foods | 2026-06-05 |

If you find yourself needing one of these to run again, that probably
means you're undoing the migration — talk to past you first.
```

- [ ] **Step 3: Commit**

```bash
git add scripts/archive/
git commit -m "chore(scripts): archive 7 completed migration/backfill scripts

Phase 5 Task 13. Moved to scripts/archive/ with a README index. These
won't run again; archiving clarifies which scripts in scripts/ top-level
are still active operational tools."
```

---

## Task 14: Split `runner.py` into 4 modules

**Files:**
- Create: `notion_sync/bootstrap.py`
- Create: `notion_sync/decisions.py`
- Create: `notion_sync/dispatch.py`
- Create: `notion_sync/post_phases.py`
- Modify: `notion_sync/runner.py` → thin shim

最大单 task. 把 780 行 runner 拆 4 文件，runner.py 变 thin shim 保 import 兼容.

### 边界

- **`bootstrap.py`**:
  - `main() -> int` (CLI entry, argparse, env loading)
  - `should_run_now(sync_global, *, now_utc=None) -> bool`
  - `now_iso_date / now_iso_datetime` helpers
  - logging setup

- **`decisions.py`**:
  - `apply_pending_decisions(pb, nc, *, ctx: SyncContext) -> int`
  - `_apply_one_decision(row, *, pb, nc, ctx: SyncContext) -> None`
  - `_load_snap(properties, prop_name) -> dict` helper

- **`dispatch.py`**:
  - `sync_collection(cfg_row, pb, nc, *, frozen_pairs=None) -> dict`
  - `_apply_pb_to_notion` / `_apply_notion_to_pb` / `_apply_pb_new` / `_apply_notion_new`
  - `ACTION_HANDLERS = {PbOnlyChange: _apply_pb_to_notion, ...}` 4-entry dict
  - `_action_ids` / `_ACTION_ID_GETTERS` (from Task 2)

- **`post_phases.py`**:
  - `cleanup_resolved_activity(nc, *, days=90) -> int`
  - `notify_pending(nc) -> int`
  - `_render_pending_markdown(rows) -> str`
  - `_alert_state_path / _alert_already_sent / _save_alert_state`

- **`runner.py`** (final shim):
  ```python
  """Thin shim — Phase 5 split runner.py into bootstrap/decisions/
  dispatch/post_phases. Preserves the public surface so:
  - systemd unit `python -m notion_sync.runner` works (calls bootstrap.main)
  - scripts/reconcile_initial.py `from notion_sync.runner import sync_collection` resolves
  - tests' `from notion_sync.runner import should_run_now` resolves

  Phase 6 cleanup: callers should migrate to direct imports from
  notion_sync.bootstrap / .dispatch / etc., then this shim can shrink.
  """
  from notion_sync.bootstrap import (
      main, should_run_now, now_iso_date, now_iso_datetime,
  )  # noqa: F401
  from notion_sync.decisions import apply_pending_decisions  # noqa: F401
  from notion_sync.dispatch import (
      sync_collection, _action_ids, _ACTION_ID_GETTERS,
  )  # noqa: F401
  from notion_sync.post_phases import (
      cleanup_resolved_activity, notify_pending,
  )  # noqa: F401

  if __name__ == "__main__":
      raise SystemExit(main())
  ```

### Steps

- [ ] **Step 1: Move helpers + main into `bootstrap.py`**

Copy from runner.py: `now_iso_date`, `now_iso_datetime`, `should_run_now`, `main` (with all its imports + argparse + env loading). Module-level docstring describes that bootstrap holds the entrypoint + scheduling guard.

Quick sanity:
```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/python -c "from notion_sync.bootstrap import main, should_run_now; print(\"OK\")"'
```

- [ ] **Step 2: Move decision helpers into `decisions.py`**

Copy `apply_pending_decisions`, `_apply_one_decision`, `_load_snap`. Update signatures: each now takes a `ctx: SyncContext` instead of 6 individual kwargs. Build ctx in the caller (bootstrap or dispatch) and pass through.

The `Use Notion` race fix from Task 3 should be in this file's `_apply_one_decision`.

- [ ] **Step 3: Move dispatch + 4 _apply_* into `dispatch.py`**

Copy `sync_collection`, `_apply_pb_to_notion`, `_apply_notion_to_pb`, `_apply_pb_new`, `_apply_notion_new`, `_action_ids`, `_ACTION_ID_GETTERS`.

Also build `ACTION_HANDLERS`:
```python
# Apply-function dispatch table. sync_collection uses
# `ACTION_HANDLERS.get(type(action))` instead of an isinstance chain.
ACTION_HANDLERS = {
    PbOnlyChange:     _apply_pb_to_notion,
    NotionOnlyChange: _apply_notion_to_pb,
    PbNew:            _apply_pb_new,
    NotionNew:        _apply_notion_new,
}
```

In `sync_collection`, replace the isinstance branches:
```python
# before
for a in actions:
    if isinstance(a, PbOnlyChange):
        _apply_pb_to_notion(a, ...)
    elif isinstance(a, NotionOnlyChange):
        ...

# after
for a in actions:
    handler = ACTION_HANDLERS.get(type(a))
    if handler is None:
        # NoChange / Conflict / Vanished — non-apply paths handled separately
        ...
        continue
    try:
        handler(a, ctx=ctx, pb=pb, nc=nc)
    except Exception as e:
        pb_id, notion_id = _action_ids(a)
        log_event("apply_error", collection=ctx.collection,
                  action=type(a).__name__,
                  pb_id=pb_id, notion_id=notion_id, error=str(e))
```

- [ ] **Step 4: Move post phases into `post_phases.py`**

Copy `cleanup_resolved_activity`, `notify_pending`, `_render_pending_markdown`, `_alert_state_path`, `_alert_already_sent`, `_save_alert_state`.

Note: `notify_pending` should already have the Task 12 sys.path-hack fix applied — preserve that.

- [ ] **Step 5: 改 `runner.py` 成 thin shim**

Final content (replace entire file):

```python
"""Thin shim — Phase 5 split runner.py into bootstrap/decisions/
dispatch/post_phases. Preserves the public surface so:
- systemd unit `python -m notion_sync.runner` works
- scripts/reconcile_initial.py `from notion_sync.runner import sync_collection` resolves
- tests' `from notion_sync.runner import should_run_now` resolves

Phase 6 cleanup: callers should migrate to direct imports from
notion_sync.bootstrap / .dispatch / etc., then this shim can shrink.
"""
from notion_sync.bootstrap import (
    main, should_run_now, now_iso_date, now_iso_datetime,
)  # noqa: F401
from notion_sync.decisions import apply_pending_decisions  # noqa: F401
from notion_sync.dispatch import (
    sync_collection, _action_ids, _ACTION_ID_GETTERS,
)  # noqa: F401
from notion_sync.post_phases import (
    cleanup_resolved_activity, notify_pending,
)  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run ALL sync tests + smoke**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/ -v 2>&1 | tail -20'
```

Expected: baseline pass count + 3 new test files (test_context + test_apply_decisions + test_action_handlers) all green. No import errors.

```powershell
$env:BASE = "https://dashboard-server.tail4cfa2.ts.net"
$env:BRIDGE_COOKIE = "bridge_session=..."
python tests/smoke_backend.py
```

Expected: 5/5 green.

- [ ] **Step 7: --force-now spot check**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && set -a; . ./.env; set +a; .venv/bin/python -m notion_sync.runner --force-now --only trips 2>&1 | tail -10'
```

Expected: same shape `last_sync_summary` as Task 0 baseline trips row. No tracebacks.

- [ ] **Step 8: Commit**

```bash
git add notion_sync/bootstrap.py notion_sync/decisions.py notion_sync/dispatch.py notion_sync/post_phases.py notion_sync/runner.py
git commit -m "$(cat <<'EOF'
refactor(notion_sync): split runner.py into bootstrap/decisions/dispatch/post_phases

Phase 5 Task 14. 780-line runner.py → 4 focused files + 13-line shim:

- bootstrap.py: main + CLI + should_run_now + iso datetime helpers
- decisions.py: apply_pending_decisions + _apply_one_decision (takes
  SyncContext from Task 1)
- dispatch.py: sync_collection + 4 _apply_* + ACTION_HANDLERS dict
  + _action_ids + _ACTION_ID_GETTERS (Tasks 2 + 7)
- post_phases.py: cleanup_resolved_activity + notify_pending +
  _render_pending_markdown + _alert_* helpers

runner.py: 13-line shim re-exporting the public surface so systemd,
reconcile_initial.py, and tests don't change imports.

All sync tests + smoke green. --force-now --only trips matches Task 0
baseline last_sync_summary.
EOF
)"
```

---

## Task 15: 部署 + force-now 全 8 表 + 验证 Sync Activity

**Files:** none (verification only).

- [ ] **Step 1: Deploy**

```powershell
deploy
```

Expected: health 1-attempt pass.

- [ ] **Step 2: Force-now 8 tables**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && set -a; . ./.env; set +a; time .venv/bin/python -m notion_sync.runner --force-now 2>&1 | tail -30'
```

Expected:
- Elapsed time < 30s (perf gate). Compare to Task 0 baseline.
- No tracebacks.
- All 8 tables show `last_sync_summary`.

- [ ] **Step 3: USER verifies Sync Activity in Notion**

User opens Notion's "Sync Activity" DB and checks:
- No bogus new conflicts created by the refactor (compared to Task 0 baseline)
- Pending rows that existed before this Phase 5 run still actionable (decision values intact)
- Any `Use Notion` decisions applied during this pass: next sync pass shows NoChange (race fix validation — would have shown false conflict on legacy code)

If anything looks wrong: revert via `.bak` snapshot left by deploy tool, hotfix.

- [ ] **Step 4: Full test run on VM**

```bash
ssh dashboard-server 'cd /home/dev/phone-bridge && PYTHONPATH=. .venv/bin/pytest tests/notion_sync/ tests/test_session_manager.py tests/test_notion_api_backoff.py tests/test_pb_client.py tests/test_settings.py tests/test_io_utils.py tests/test_static_assets.py -v 2>&1 | tail -10'
```

Expected: all green. Phase 5 adds 3 new test files; total should be 41+ pass.

---

## Task 16: 48h staging soak

让 hourly cron tick 至少 2 轮。中间任意时间检查 journal：

```bash
ssh dashboard-server 'sudo journalctl -u notion-sync.service --since "12 hours ago" | grep -iE "error|exception|traceback" | head -10'
```

Expected: 空（或只是 PB 重试退避 INFO 行）。

24-48h 后检查累积日志：

```bash
ssh dashboard-server 'sudo journalctl -u notion-sync.service --since "48 hours ago" | grep -iE "error|exception|traceback" | head -10'
ssh dashboard-server 'tail -50 /home/dev/phone-bridge/.bridge_data/sync.log'
```

如果 0 errors + Sync Activity 看起来正常，进入 Task 17。

---

## Task 17: Phase 5 完成报告 + merge to main

- [ ] **Step 1: Write CHANGELOG entry**

Insert above the Phase 4 entry in `CHANGELOG.md`:

```markdown
## 2026-06-XX — Phase 5 · `notion_sync/runner.py` 拆解 + 算法升级

**Branch:** `refactor/phase-5-sync-runner` (~18 commits)
**实际工时:** 约 X 小时

### 落地的事
- `notion_sync/runner.py` 780 行 → 13 行 shim + 4 模块 (bootstrap / decisions / dispatch / post_phases)
- `notion_sync/context.py` 新增 SyncContext dataclass + make_context 工厂
- `_action_ids` 改 `_ACTION_ID_GETTERS` 字典分派；`ACTION_HANDLERS` apply-fn 分派替代 8-branch isinstance
- **Use Notion race fix**：PATCH Notion → 读 last_edited → 写 PB（替代 retrieve → 写 PB），保证 Notion/PB last_synced_at 同时间，下次 sync 不假冲突
- linkage 走 field_map_overrides 反查列名（不再硬编码 "Date"/"Day"/"Trip"/"Dates"）
- icons declarative：`sync_config.icon_field` + `icon_default`（6 个 legacy collection 保留 domain；新 collection 走声明路径）
- sync.log RotatingFileHandler 10MB × 5
- apply_error 日志带 pb_id + notion_id (via _action_ids)
- frozen_pairs 改一次性 group-by（8 collection 8 query → 1 query）
- relation_lookup 改 LazyRelationLookup（消除 64 次全表 → 仅按需）
- should_run_now 加"≥23h since last_successful_run_at"，防漂移 + 防重启双跑
- notify_pending 用 app.paths.DATA_DIR + sqlite3 替代 sys.path hack
- 删 config.invalidate() 死代码
- 归档 7 个完成的 migration/backfill scripts 到 scripts/archive/

### 闸门
- ✅ 现有 tests/notion_sync/ 全过 + 3 new test files (context / apply_decisions / action_handlers)
- ✅ smoke 5/5
- ✅ --force-now 8 张表跑 < 30s（Task 0 baseline 对比）
- ✅ Sync Activity 输出 schema 兼容（无新假冲突）
- ✅ 48h staging soak journal 0 ERROR

### 偏离计划 / Regressions
(fill in after execution)

### 量化
- runner.py: 780 → 13 行 shim
- 新增 5 个 notion_sync 模块: context.py + bootstrap.py + decisions.py + dispatch.py + post_phases.py
- 测试: 3 个新文件覆盖 race + dataclass + dispatch

### 修的隐藏炸弹
- `Use Notion` decision 静默 race → 半小时内重复 false conflict
- `should_run_now` 跨小时漂移 → 整轮 sync 漏跑
- relation_lookup eager 8×8 = 64 次 PB list_all 每次 sync
- sync.log 无限增长撑爆 VM 磁盘的潜在风险

### 下一步
👉 Phase 6 · 收尾（测试补齐 / structlog / CSRF / 文档）
新窗口续接指令："继续重构路线图，从 Phase 6 开始"
```

- [ ] **Step 2: Update roadmap progress table**

```markdown
| 5 sync | ✅ 已合并 | `refactor/phase-5-sync-runner` | 2026-06-XX | `<merge-SHA>` | CHANGELOG §Phase 5 |
| 6 收尾 | 🚧 待开始 | `refactor/phase-6-polish` | — | — | — |
```

更新"下一步入口"段指向 Phase 6.

- [ ] **Step 3: Commit on branch**

```bash
git add CHANGELOG.md docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(changelog): Phase 5 completion report"
```

- [ ] **Step 4: Merge to main**

```bash
git checkout main
git merge --no-ff refactor/phase-5-sync-runner -m "Merge branch 'refactor/phase-5-sync-runner'

Phase 5 · notion_sync runner 拆解 + 算法升级

runner.py 780 → 13-line shim + 4 new modules (bootstrap/decisions/
dispatch/post_phases). SyncContext dataclass. Fixed Use Notion race
+ should_run_now drift gate. Perf: lazy relation_lookup + group-by
frozen_pairs. Declarative icons + linkage via sync_config /
field_map_overrides. Log rotation. Archived 7 completed migrations.

详见 CHANGELOG §Phase 5。"
git log --oneline -3  # capture merge SHA
```

- [ ] **Step 5: Update roadmap with merge SHA + push**

```bash
# Edit roadmap to fill the actual merge commit SHA
git add docs/superpowers/specs/2026-06-06-refactor-roadmap.md
git commit -m "docs(roadmap): mark Phase 5 ✅ merged at <SHA>; Phase 6 next"
git push origin main
```

---

## Self-Review

**1. Spec coverage:**

| Spec 动作 | Plan task |
|---|---|
| runner 拆 4 文件 | Task 14 |
| SyncContext dataclass | Task 1 |
| ACTION_HANDLERS dict 分派 | Task 2 + Task 14 (sync_collection 用 dict 分派) |
| 修 `Use Notion` race | Task 3 |
| linkage 走 overrides | Task 4 |
| icons declarative | Task 5 |
| sync.log RotatingFileHandler | Task 6 |
| apply_error 带 IDs | Task 7 |
| frozen_pairs group-by | Task 8 |
| relation_lookup lazy | Task 9 |
| should_run_now <23h | Task 10 |
| 归档 7 scripts | Task 13 |
| 删 config.invalidate | Task 11 |
| 修 notify_pending sys.path | Task 12 |
| 现有 sync tests 全过 | Task 15 |
| test_apply_decisions.py | Task 3 |
| --force-now 8 表 baseline 对比 | Task 0 + Task 15 |
| 同步墙时 < 30s | Task 15 |
| 48h staging soak | Task 16 |

✅ 全覆盖。

**2. Placeholder scan:**

- Task 2 step 3 的测试模板里有一个 `pass  # IMPLEMENTER: fill after reading changeset.py` — 这是显式 actionable instruction，不是 silent placeholder. 执行者先 grep changeset.py 拿到真字段名再写测试。
- Task 5 step 3 改 sync_collection 调用 icon_for 的具体 site 没列出——执行者会 grep `icon_for(` 找所有调用点改。这是 mechanical work，不是设计 placeholder.
- Task 14 拆 runner 的具体 line ranges 没列——执行者要读 runner.py 找原函数体粘到新文件。同样 mechanical.

可接受。

**3. Type consistency:**

- `SyncContext` 字段（Task 1）→ `_apply_*` 函数签名（Task 14）一致
- `_ACTION_ID_GETTERS` keys（Task 2）→ `ACTION_HANDLERS` keys（Task 14）部分重叠（前者全 8 类，后者只 4 类有 apply fn）；这是设计：NoChange/BothChanged/Vanished 走非 apply 路径
- `LazyRelationLookup.get()`（Task 9）→ `_apply_*` 内部 `relation_lookup.get(target)` 调用一致
- `frozen_pairs_for_all` 返回 `dict[str, tuple[set, set]]`（Task 8）→ `sync_collection(frozen_pairs=tuple[set, set])` 调用一致

All consistent.

**4. Order dependencies:**

- Task 0 baseline 第一
- Task 1 (SyncContext) → Task 14 用
- Task 2 (_action_ids dict) → Task 7 用 + Task 14 移到 dispatch.py
- Task 3 (race fix) → 独立, 数据安全优先
- Tasks 4-13 → 独立 (no inter-dep except 共享 sync_config schema)
- Task 14 (split) → 依赖 Tasks 1+2 基础
- Task 15 → 在 Task 14 后跑 --force-now 验证
- Task 16 → 48h 等
- Task 17 → merge

**5. Honest scope:**

- 18 tasks (0-17)
- ~5 days wall-clock if 48h soak counted; ~2-3 active days
- 风险中：data corruption 可能（mostly 在 Task 3 race fix）
- Mitigations: 每 task 独立 commit + 独立 revert + 48h soak + Task 0 forensic baseline

---

**Plan complete.**
