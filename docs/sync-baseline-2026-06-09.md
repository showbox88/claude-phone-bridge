# Phase 5 sync baseline (2026-06-09)

Forensic snapshot before Phase 5 refactor. Later tasks compare against this.

## pytest tests/notion_sync/ baseline

- Pass count: **62**
- Wall time: 0.19s
- Test names:
  - test_backup.py: 2 tests
    - test_backup_writes_json_per_base_collection
    - test_backup_creates_timestamped_subdir
  - test_changeset.py: 10 tests
    - test_no_change_when_neither_side_moved
    - test_pb_only_change
    - test_notion_only_change
    - test_both_changed
    - test_pb_new_unlinked
    - test_notion_new_unlinked
    - test_notion_vanished_pb_thinks_linked
    - test_pb_vanished_notion_thinks_linked
    - test_mixed_set
    - test_iso_t_separator_normalized
  - test_codec.py: 26 tests
    - test_snake_to_title_basic
    - test_title_to_snake_basic
    - test_pb_text_to_notion_rich_text
    - test_pb_number_to_notion
    - test_pb_bool_to_notion_checkbox
    - test_pb_date_to_notion
    - test_pb_datetime_to_notion
    - test_pb_select_single_to_notion
    - test_pb_select_multi_to_notion
    - test_pb_empty_text_to_notion_empty
    - test_notion_rich_text_to_pb
    - test_notion_title_to_pb
    - test_notion_number_to_pb
    - test_notion_checkbox_to_pb
    - test_notion_date_to_pb
    - test_notion_date_none_to_pb
    - test_notion_select_to_pb
    - test_notion_multi_select_to_pb
    - test_roundtrip_text
    - test_roundtrip_multi_select
    - test_snake_to_title_handles_consecutive_underscores
    - test_rich_text_str_guards_non_dict_items
    - test_notion_type_phone_number
    - test_notion_type_title_overrides_text
    - test_notion_type_email
    - test_notion_type_url_empty_returns_none
    - test_notion_type_select_unknown_falls_back
  - test_matching.py: 11 tests
    - test_normalize_title_lowercases_and_strips
    - test_normalize_handles_chinese
    - test_bigram_jaccard_identical
    - test_bigram_jaccard_similar
    - test_bigram_jaccard_unrelated
    - test_best_match_exact
    - test_best_match_fuzzy_title_same_date
    - test_best_match_different_date_penalized
    - test_best_match_no_candidates
    - test_best_match_below_threshold
    - test_best_match_date_format_difference_does_not_penalize
    - test_best_match_iso_with_t_separator_normalizes
  - test_runner_guard.py: 12 tests
    - test_runs_at_configured_hour_in_local_tz
    - test_does_not_run_off_hour
    - test_respects_paused
    - test_handles_tokyo
    - test_handles_missing_config_safely
    - test_bad_timezone_returns_false
    - test_daylight_savings_us_winter
    - test_runs_at_either_of_two_hours
    - test_second_hour_alone_works
    - test_invalid_second_hour_ignored
    - test_paused_overrides_both_hours

Note: the original plan listed test_config.py, test_icons.py, test_linkage.py,
and test_provisioner.py — those files do not exist in the current tree. The
five present files (backup, changeset, codec, matching, runner_guard)
account for all 62 collected tests.

## --force-now wall-clock (perf gate < 30s)

- real: **0m27.701s**
- user: 0m1.641s
- sys: 0m0.074s

Under 30s gate: **YES** (27.7s, 2.3s headroom).

## Per-collection sync output

From `.bridge_data/sync.log` for the forced run starting `2026-06-09T19:17:09Z`
(run_end `2026-06-09T19:17:37Z`). All collections steady-state (no changes,
no conflicts, no deletes):

| Collection | NoChange | applied | conflicts | deletes | frozen_skipped | decisions_applied |
|---|---|---|---|---|---|---|
| trips     | 1  | 0 | 0 | 0 | 0 | 0 |
| days      | 73 | 0 | 0 | 0 | 0 | 0 |
| plans     | 8  | 0 | 0 | 0 | 0 | 0 |
| todos     | 35 | 0 | 0 | 0 | 0 | 0 |
| contacts  | 1  | 0 | 0 | 0 | 0 | 0 |
| locations | 13 | 0 | 0 | 0 | 0 | 0 |
| stops     | 73 | 0 | 0 | 0 | 0 | 0 |
| journal   | 2  | 0 | 0 | 0 | 0 | 0 |
| expenses  | 26 | 0 | 0 | 0 | 0 | 0 |
| foods     | 12 | 0 | 0 | 0 | 0 | 0 |

Linkages pass after collections:

- stops_patched: 0, no_change_stops: 73
- days_patched:  0, no_change_days:  73

run_end aggregate:

- applied: 0, conflicts: 0, deletes: 0, pending: 0
- stops_patched: 0, no_change_stops: 73
- days_patched:  0, no_change_days:  73

No tracebacks. No warnings. No `apply_error` events. Note: the registry
currently shows 10 enabled sync_config collections (the plan said "8
enabled" — actual count is 10, see YAML).

## sync_global state

See `sync-baseline-2026-06-09.yaml` for the full snapshot of `sync_global`
and all `sync_config` rows (104 lines).
