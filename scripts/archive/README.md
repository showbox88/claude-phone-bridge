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

If you find yourself needing to run one again, that probably means
you're undoing the migration — talk to past you first.
