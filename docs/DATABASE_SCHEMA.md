# SQLite database and generated data

The application uses one local SQLite database, `bot_state.sqlite`, beside the source project or packaged executable. It runs in WAL mode and opens short-lived connections with foreign keys and a busy timeout enabled.

The database is both operational state and an audit record. It is not a broker statement, account ledger, or replacement for IBKR order/execution facts.

## Portability and file placement

Source mode stores data in the repository root. Packaged mode stores data beside `IBKRTradingBot.exe`.

Related paths:

| Path | Purpose |
|---|---|
| `bot_state.sqlite` | Active database |
| `bot_state.sqlite-wal`, `bot_state.sqlite-shm` | SQLite WAL sidecars while active |
| `backups/` | Rotated online backups and validation result |
| `exports/` | History exports requested by the GUI |
| `audit_exports/` | Audit-bundle ZIP files when the default target is used |
| `debug_reports/` | Human-readable event log and latest state report |
| `debug_captures/` | Completed market-data capture packages |

Do not copy only the main SQLite file while the application is writing. Use an application-created backup or shut down cleanly so WAL state is handled.

## `app_settings`

Key/value storage for editable drafts.

| Column | Type | Meaning |
|---|---|---|
| `key` | `TEXT PRIMARY KEY` | Setting namespace/key |
| `value_json` | `TEXT NOT NULL` | Serialized connection, strategy, or application setting |
| `updated_at` | `TEXT NOT NULL` | UTC update time |

Draft settings are persisted independently of an active cycle. Starting a cycle copies the relevant values into `cycles` so recovery does not depend on later draft edits.

The `last_resume_checkpoint` key records the checkpoint ID, UTC creation time, shutdown reason, active cycle identity/stage, ticker, and whether explicit resume is required. During an accepted app exit or controlled Windows shutdown, this setting is committed in the same transaction as the latest connection/strategy drafts, active cycle row, and audit event.

## `cycles`

One row per strategy cycle. This is the core restart/recovery and completed-history table.

### Identity and lifecycle

- `id`, `cycle_number`, `ticker`, `stage`;
- `created_at`, `updated_at`;
- `account`, `con_id`, `exchange`, `primary_exchange`, `currency`, `rth_only`;
- `recovery_required`, `close_position_market_requested`, `stop_after_current_cycle`, `error_message`.

A blank `account` is valid and means no explicit IBKR order account override.

### Budget and strategy snapshot

- `investment_amount`, `budget`, `reinvest_profits`, `reinvested_profit`;
- manual/order-driving percentages: `initial_drop_pct`, `buy_rebound_trail_pct`, `rise_trigger_pct`, `sell_trailing_stop_pct`;
- ATR toggles, period, bar duration, multipliers, and min/max clamps;
- protective SELL settings;
- slippage planning settings;
- hard risk, delayed-data, what-if, freshness, volatility, and session-timing settings.

`rise_trigger_pct` is the historical persisted name for user-facing **Minimum profit %**. `max_cycles_per_ticker_day` is also retained as a compatibility name; current guard behavior treats it as a total completed-cycle cap for the selected ticker.

### Price and trigger state

- `anchor_price`, `last_price`, `drop_trigger_price`;
- `buy_initial_trail_stop_price`;
- `rise_trigger_price`, `sell_initial_trail_stop_price`.

During blocked ATR warmup, `drop_trigger_price` can remain `NULL` by design.

### BUY state

- `quantity`;
- `buy_order_id`, `buy_perm_id`, `buy_order_ref`, `buy_status`;
- `buy_filled_qty`, `avg_buy_price`, `buy_commission`, `buy_filled_at`.

### Protective SELL state

- `protective_sell_order_id`, `protective_sell_perm_id`, `protective_sell_order_ref`, `protective_sell_status`;
- `protective_sell_initial_stop_price`, `protective_sell_cancel_requested`;
- `protective_sell_filled_qty`, `protective_avg_sell_price`, `protective_sell_commission`, `protective_sell_filled_at`.

### Final SELL and P/L state

- `sell_order_id`, `sell_perm_id`, `sell_order_ref`, `sell_status`;
- `sell_filled_qty`, `avg_sell_price`, `sell_commission`, `sell_filled_at`;
- `gross_pnl`, `net_pnl`.

Indexes support stage/ticker/history/recovery lookups by `ticker`, `stage`, `updated_at`, and `sell_filled_at`.

The application-owned unsold ledger is reconstructed from these persisted cycle fill fields: BUY quantity minus the larger of final-SELL and protective-SELL filled quantity for each unresolved cycle. Cycles explicitly marked manually handled are excluded. BUY gating, Stop, main-window close, and Reconciliation all use this local ledger rather than the account-wide IBKR position.

## `orders`

Append/update audit records for application-created broker orders.

| Column group | Contents |
|---|---|
| Identity | `id`, `cycle_id`, `ticker`, `action`, `order_type` |
| Broker identity | `order_id`, `perm_id`, `order_ref` |
| Requested values | `quantity`, `trailing_percent`, `initial_stop_price` |
| State | `status`, `created_at`, `updated_at` |
| Diagnostics | `raw_json` |

The table is useful for history and recovery comparison, but an order row alone does not prove that an order is still working. Broker facts must be checked.

## `executions`

Recorded fills from live polling or recovery.

| Column group | Contents |
|---|---|
| Identity | `id`, optional `cycle_id`, `ticker`, `order_ref`, `order_id`, `perm_id`, `execution_id` |
| Fill | `side`, `shares`, `price`, `avg_price`, `commission`, `currency`, `executed_at` |
| Diagnostics | `raw_json` |

Execution IDs are indexed and checked to prevent duplicate insertion when the same fill is observed through more than one IBKR path. A recovered execution can exist without a cycle link when local ownership cannot be resolved safely.

## `events`

Human/operator-oriented audit log.

- `id`, `created_at`, `level`;
- optional `ticker`, `cycle_id`;
- `message`, `raw_json`.

The GUI shows recent events. Storage also appends a readable line to `debug_reports/audit_events_readable.log` on a best-effort basis.

Trade-history audit lookup uses the ordered index `(cycle_id, created_at, id)`, so selecting one completed cycle does not scan unrelated global event rows.

## `decision_events`

Structured append-only records of controller/strategy decisions.

- `id`, `created_at`, `event_type`;
- optional `ticker`, `cycle_id`;
- `stage_before`, `stage_after`, `decision_result`, `message`;
- optional `broker_order_id`, `perm_id`;
- `raw_json`.

These records explain why a transition or submission did or did not occur. They are not broker callbacks.

Trade-history audit lookup uses the ordered index `(cycle_id, created_at, id)`.

## `broker_events`

Raw broker/recovery event records.

- `id`, `created_at`, `event_type`;
- optional `ticker`, `cycle_id`;
- `order_ref`, `order_id`, `perm_id`, `execution_id`;
- required `raw_json`.

This table preserves broker facts used for diagnostics. It is indexed by time, order reference, and execution ID.

## Foreign-key behavior

- Orders are deleted with their cycle (`ON DELETE CASCADE`).
- Executions/events/decision events/broker events retain their rows and clear the cycle link (`ON DELETE SET NULL`) where defined.

Normal application operation does not delete completed cycle history as part of rotation.

## Schema migration

`_ensure_schema()` is additive and idempotent:

1. when an existing database is present, make a best-effort pre-schema-check online backup;
2. create any missing tables/indexes;
3. add known missing `cycles` columns with `ALTER TABLE`;
4. preserve unknown/newer row data when deserializing by using known dataclass fields and defaults.

The migration path does not drop tables or rewrite trading history.

## Backups

`backup_database()` uses SQLite’s online backup API after a passive WAL checkpoint. A backup is accepted only when:

- `PRAGMA integrity_check` returns `ok`;
- required core tables exist;
- a temporary restore-candidate copy can also be opened and validated.

The latest validation is written to `backups/latest_restore_validation.json`. Backups are named with a UTC stamp and reason, and the default retention is 50 files.

Backups are requested before/around high-value lifecycle events such as schema checks, order submission, fills, shutdown, and audit export. Backup failure is recorded/handled by the calling path; it does not transform a backup into broker truth.

## Audit bundles

An audit bundle is a ZIP created without contacting IBKR or changing strategy state. It can contain:

- `manifest.json` with creation time and backup validation;
- `snapshot.json` supplied by the controller;
- a validated database backup, or clearly labelled unvalidated fallback when backup creation failed;
- readable reports;
- JSON exports of core SQLite tables (bounded per table);
- recent event records.

Audit bundles can contain sensitive account, order, execution, and strategy information.

## History and derived metrics

Completed-cycle history is read from `cycles` and enriched in memory with display/export metrics such as gross/net percentage, configured percentages, holding time summaries, win rate, completed drawdown, and loss streak.

These derived metrics do not alter stored order/fill facts and are not account-wide performance figures.
