# Architecture

The application separates GUI rendering, state-machine decisions, broker integration, and durable storage so that trading rules remain testable and broker side effects remain centralized.

```text
PySide6 GUI (Qt main thread)
        |
        | queued commands / snapshots / signals
        v
TradingController (worker thread)
        |
        +--> StrategyEngine (pure state transitions)
        |
        +--> BotStorage (short-lived SQLite connections)
        |
        +--> MarketDataCaptureManager (bounded RAM + async ZIP writer)
        |
        +--> BrokerAdapter interface
                  |
                  v
          IbAsyncTwsAdapter
                  |
                  v
        TWS or IB Gateway socket API
```

## Entry point and portable process boundary

`main.py` creates the Qt application, applies a stable light palette, acquires the single-instance lock, creates the controller/window, connects Qt's session-management commit signal, and starts the Qt event loop. A Windows-controlled session termination calls the GUI's non-interactive checkpoint handler through a direct Qt connection. The handler saves resume state but does not stop the worker or exit from inside the session callback; this keeps the application usable if another program cancels shutdown. If shutdown proceeds, normal event-loop cleanup stops the worker and releases the process lock.

`app/paths.py` defines the portable application directory:

- packaged mode: the directory containing the executable;
- source mode: the repository root.

The database and generated folders are derived from this location. The application has no external database service and no daemon component.

## GUI layer

`app/gui.py` is the operator interface. It:

- collects connection and strategy settings;
- provides contract search, confirmation, start, and stop commands;
- displays controller snapshots, price diagnostics, stages, blockers, and audit events;
- renders the five-stage flowchart and cycle timeline;
- provides trade history and Reconciliation actions;
- disables unsafe or locked controls without deciding the trading strategy.

The GUI does not decide when a BUY or SELL should occur. Graphs and projections are explanatory views. `TradingController` and `StrategyEngine` are authoritative.

The top input lock is an accidental-edit guard. It disables editable configuration and all five workflow buttons while leaving monitoring, tabs, history, and reconciliation views usable. It does not stop the worker or cancel broker orders.

The fixed five-button command bar is the dashboard workflow control surface. The former duplicate Controls group has been removed; the Recovery / audit log occupies the full dashboard width in Simple, Advanced, and Debug modes.

## Controller layer

`app/controller.py` owns the live worker loop and all broker side effects. Public GUI methods enqueue commands. Private worker methods:

- connect/reconnect and qualify contracts;
- refresh market-data and RTH diagnostics;
- maintain current-session RTH ATR observations regardless of whether adaptation is enabled, aggregate bounded OHLC bars incrementally, and apply ready percentages only when adaptation is enabled;
- evaluate BUY/SELL blockers;
- advance the pure strategy state;
- submit, cancel, and poll app-owned orders;
- persist cycle/order/execution/audit state;
- reconcile stored state with broker facts;
- initiate backups, reports, and market-data captures.

The worker-loop order is deliberate: dispatch broker callbacks, verify the local socket, verify the separate Gateway/TWS-to-IBKR link, reconcile once after an upstream restoration, consume actual market-data events, advance the strategy, then poll or submit broker actions. No strategy advancement, order polling, or new transmission occurs while the upstream link is unavailable. New order submissions fail closed when required broker, RTH, recovery, or price facts are uncertain.

## Strategy layer

`app/strategy.py` implements the broker-neutral five-stage state machine. It receives a `CycleState`, current settings/price, order acceptance, fills, or safe setting updates and returns:

- the next `CycleState`;
- zero or more `StrategyAction` objects.

It does not import Qt, connect to IBKR, or write SQLite. The controller validates and executes its actions. This boundary enables deterministic unit tests and CSV simulations.

`app/models.py` contains serializable dataclasses, enums, validation, percentage/price formulas, ATR calculations, and compatibility names. The persisted field `rise_trigger_pct` is the user-facing **Minimum profit %**.

## Broker adapter layer

`app/ib_adapter.py` defines `BrokerAdapter` and implements it with `ib_async`. Responsibilities include:

- TWS/Gateway connection;
- managed-account discovery for display/explicit routing validation;
- contract search and qualification;
- market-data subscription and price-source selection;
- actual `pendingTickersEvent` sequencing, subscription identity, and callback timestamps so cached `Ticker` reads cannot impersonate fresh data;
- separate local-socket and upstream IBKR connectivity state driven by broker system messages, including 1100, 1101, 1102, 1300, and 2110;
- RTH/liquid-hours interpretation;
- native trailing and market order construction;
- what-if checks;
- app-order filtering;
- order status, fills, open-order, and recent-execution recovery.

The adapter does not own strategy stages. It returns broker facts or raises a broker error that causes the controller to pause or enter recovery. On code 1101 it discards obsolete market-data handles so future reads issue new subscriptions. On code 1102 it retains the active handles but clears update metadata until a new ticker event arrives. The production adapter requires event identity; if `pendingTickersEvent` cannot be registered, populated cached fields remain diagnostic only and no strategy price is produced.

## Storage layer

`app/storage.py` owns the portable SQLite schema and data access. It stores:

- draft connection/strategy settings;
- active and completed cycles;
- app-created order records;
- deduplicated executions;
- operator/audit events;
- decision events and snapshots;
- broker callback/recovery events.

SQLite connections are short-lived and scoped to each storage operation. Schema changes are additive and idempotent so an older portable database can be opened without destructive migration.

The resume-checkpoint transaction writes the latest connection draft, strategy draft, active cycle, `last_resume_checkpoint` metadata, and its audit event together. The controller normally performs this in the worker after applying safe active-cycle edits without re-evaluating the last market price. A bounded direct-storage fallback uses the same checkpoint ID, and the transaction begins with an immediate write lock so a delayed worker and fallback cannot duplicate the logical checkpoint.

The storage layer also creates restore-validated online backups and audit bundles. Its persisted BUY and SELL fills define the application-owned unsold quantity used by BUY gating, Stop, window-close, and Reconciliation actions. It is not the sole source for live order status; recovery compares it with broker facts.

## Threading and signal model

- The Qt GUI runs in the main thread.
- `TradingController` runs a dedicated Python worker thread.
- GUI commands are queued to the worker.
- Worker snapshots and events are emitted back through Qt signals.
- A lightweight headless signal implementation is used only by tests/build validation when `IBKR_BOT_HEADLESS_SIGNALS=1`.
- Broker calls are kept in the worker path to avoid concurrent access from GUI callbacks.
- Market-capture ZIP writing uses a separate bounded writer path after the full post-fill window is available.

## Timekeeping

All app-generated timestamps are recorded and displayed in UTC. This includes cycle rows, order/fill records, audit/decision/broker events, recovery snapshots, readable reports, and market-data captures.

The GUI can show workstation-local time alongside UTC for comparison. Imported timestamps without timezone information are interpreted as UTC for audit alignment rather than as the workstation’s local zone.

The adapter parses the active contract's date-specific IBKR `liquidHours` and timezone into explicit regular-session open/close boundaries. The controller uses those same boundaries for the base RTH decision, first/last-minute BUY blockers, and active-BUY cancellation before close, so exchange half days are handled consistently. Only when IBKR contract metadata is unavailable does the adapter supply the existing conservative weekday 09:30–16:00 New York fallback.

## Order ownership boundary

Every application-created order receives an `OrderRef` beginning with:

```text
IBKRBOT|
```

Open-order recovery and cancellation filter by this prefix. Manual orders and orders from other software are outside the management boundary unless they incorrectly reuse the prefix.

The position boundary is different: IBKR exposes an account-level stock position, but does not label individual shares by originating application. The application therefore reconstructs its own unsold quantity from persisted BUY and SELL fills. External/manual long positions do not block a new application BUY.

## Account routing boundary

`ConnectionSettings.account` is optional:

- blank: the adapter does not set `Order.account`; TWS/Gateway selects the account;
- explicit: the order carries that account and live BUY preflight validates it against managed accounts.

The account IDs shown in the status bar are display/recovery facts. A displayed account is not silently copied into the routing override.

## Guard and recovery boundary

Configured guards prevent new actions but do not rewrite broker history. The controller differentiates:

- expected guard/timing pauses and ordinary strategy waits, shown as caution states with recovery-changing buttons disabled;
- app/broker inconsistencies or ambiguous recovery, shown as error/manual-review states with only context-appropriate actions enabled.

Read-only broker refresh and audit export remain available in both cases. A recovery probe is a point-in-time snapshot; normal order polling updates or removes a matching cached row when a newer broker state arrives. A later explicit broker probe is not hidden.

A local API socket and the Gateway/TWS upstream IBKR link are independent. An upstream outage invalidates quote freshness and pauses the worker without pretending the local socket closed. After 1101/1102 restoration, app-owned open orders and recent executions are reconciled before normal processing resumes. A fresh post-recovery ticker event is still required before cached quote fields become strategy-usable.

A stored active cycle requires an explicit Start/resume after launch. The application does not automatically recreate missing orders when facts are uncertain.

## Market-data capture architecture

`app/market_data_capture.py` keeps a bounded rolling buffer in RAM. A fill creates a capture session containing the available pre-event window and accumulating post-event rows. The complete package is written to `debug_captures/` only after the post window finishes. No partial capture is flushed on shutdown.

This design avoids continuous disk writes but means a crash or early shutdown loses incomplete capture data. ATR observation history is likewise in-memory session state: it collects only during open RTH, continues while adaptation is disabled, and resets on process restart. Only actual market-data update events enter ATR/volatility history. Re-reading populated cached fields changes diagnostics only; it does not create a sample. The original callback time determines the observation bucket.

## Single-instance boundary

`app/lockfile.py` prevents another process from acquiring the same lock path in the same portable folder. The lock contains the PID and performs a Windows-safe process-existence check before removing a stale lock.

It cannot prevent a copy of the project in another folder from using another database or API client ID. Operational uniqueness still requires deliberate configuration.
