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
- provides exact IBKR contract search/selection, confirmation, start, and stop commands;
- displays controller snapshots, price diagnostics, stages, blockers, and audit events;
- renders the five-stage flowchart and cycle timeline;
- provides trade history and Reconciliation actions;
- disables unsafe or locked controls without deciding the trading strategy.

The GUI does not decide when a BUY or SELL should occur. Graphs and projections are explanatory views. `TradingController` and `StrategyEngine` are authoritative.

The top input lock is an accidental-edit guard. It disables editable configuration and all five workflow buttons while leaving monitoring, tabs, history, and reconciliation views usable. It does not stop the worker or cancel broker orders.

The fixed five-button command bar is the dashboard workflow control surface. The former duplicate Controls group has been removed; the Recovery / audit log occupies the full dashboard width in Simple, Advanced, and Debug modes.

## Controller layer

`app/controller.py` owns the live worker loop and all broker side effects. Public GUI methods enqueue commands. Private worker methods:

- connect/reconnect on a fixed ten-second local-socket cadence and qualify exact contracts;
- refresh market-data and RTH diagnostics;
- maintain current-session RTH ATR observations regardless of whether adaptation is enabled, aggregate bounded OHLC bars incrementally, and apply ready percentages only when adaptation is enabled;
- evaluate BUY/SELL blockers;
- advance the pure strategy state;
- submit, cancel, and poll app-owned orders;
- persist cycle/order/execution/audit state;
- reconcile stored state with broker facts;
- initiate backups, reports, and market-data captures.

The worker uses one serialized thread but schedules responsibilities from independent monotonic deadlines:

| Responsibility | Default cadence | Behavior |
|---|---:|---|
| GUI commands | immediate | `Queue.get()` wakes the worker as soon as a command arrives; it is not tied to a periodic sleep. |
| Broker callbacks/connectivity | 50 ms | Pumps `ib_async`, drains callbacks, updates local/upstream state, and completes post-restoration reconciliation before strategy work is allowed. |
| Strategy | 100 ms | Performs a zero-timeout read of existing subscription/order state and advances the state machine only from a newly identified market-data event. |
| GUI snapshot | 500 ms | Emits the latest in-memory controller state and cached database summary. |
| Database snapshot | 1 s | Refreshes read-heavy recent-event, history-summary, and GUI guard facts. |
| Maintenance | 1 s | Updates stale-cycle housekeeping and owns the human-readable report; the report itself remains internally limited to once per 60 seconds unless forced. |

The order within a due cycle is deliberate: broker callbacks and connectivity are processed first; strategy runs only if that broker cycle confirms readiness; database, GUI, and maintenance work then run on their own deadlines. No strategy advancement, order polling, or new transmission occurs while the upstream link is unavailable. New order submissions fail closed when required broker, exact-contract, database-currency, RTH, recovery, or price facts are uncertain. Commands can bring broker, strategy, and GUI deadlines forward without resetting the database or maintenance clocks. After an enabled local API socket is lost, reconnect attempts continue every ten seconds without an attempt limit; manual Disconnect and shutdown disable that loop.

Cadence separation does not weaken durability. State transitions, order intent/submission facts, fills, recovery results, and resume checkpoints are persisted synchronously where they occur. Only read-heavy display facts are cached. Order preflight deliberately bypasses that cache and rereads the application-owned position ledger and configured hard-risk totals from SQLite.

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
- exact USD/EUR ordinary-`STK` contract search and SMART qualification by positive `conId`;
- market-data subscription and price-source selection;
- actual `pendingTickersEvent` sequencing, subscription identity, and callback timestamps so cached `Ticker` reads cannot impersonate fresh data;
- separate local-socket and upstream IBKR connectivity state driven by broker system messages, including 1100, 1101, 1102, 1300, and 2110;
- contract-specific RTH/liquid-hours interpretation with non-U.S. fail-closed behavior;
- SMART/order-type capability checks, route-specific IBKR market-rule selection, price-band loading, side-aware order-price normalization, and whole-share minimum/step validation;
- native trailing and market order construction;
- strict dedicated what-if checks;
- app-order filtering;
- app-owned broker rejection/error retention, including bounded callback-race association;
- order status, fills, open-order, and recent-execution recovery.

The adapter does not own strategy stages. It returns broker facts or raises a broker error that causes the controller to pause or enter recovery. On code 1101 it discards obsolete market-data handles so future reads issue new subscriptions. On code 1102 it retains the active handles but clears update metadata until a new ticker event arrives. The production adapter requires event identity; if `pendingTickersEvent` cannot be registered, populated cached fields remain diagnostic only and no strategy price is produced.

Scheduled price reads are nonblocking: the adapter inspects the current subscription immediately and returns without sleeping when the timeout is zero. Bounded explicit reads first inspect the same snapshot and wait only if data is still absent, in slices no longer than 50 ms. Periodic order polling likewise consumes cached `Trade` state immediately; a missing cache entry can start a throttled `reqOpenOrders` refresh, with the response handled by a later broker callback cycle. Explicit connect/recovery/cancel paths retain their bounded waits.

When `ContractDetails` advertises market rules, the live adapter does not treat its smallest `minTick` as universally valid. It maps `validExchanges` to `marketRuleIds`, requests the selected rule, chooses the increment at the proposed price, and fails closed if the advertised rule is unavailable. Successful rules are cached for the adapter session.

Live qualification also requires an API-selected positive `conId` and verifies that the resolved symbol, currency, ordinary `STK` type, SMART route, and primary exchange are consistent with the selection. When supplied by IBKR, `validExchanges` must contain SMART and `orderTypes` must contain the `MKT` and `TRAIL` capabilities used by BouncyBot. Contract minimum-size and size-increment metadata constrain whole-share order quantities.

IBKR order errors are filtered by application ownership before they enter the audit stream. A short-lived bounded pending cache bridges the placement callback race; once the app-owned `Trade` is known, errors are attached to both order polling and broker-event persistence.

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

The existing `app_settings` key/value table also stores the portable database contract-currency claim. A new database can change its draft USD/EUR selection before the first cycle. The first persisted cycle makes the single-currency boundary final. Existing USD databases infer the claim from historical cycles, while mixed or conflicting currency evidence fails closed because the application does not convert P/L, risk totals, budgets, reinvestment, or commissions between currencies.

The controller’s database snapshot cadence reduces repeated read-only connections used for recent events, history totals, and top-bar guard display. This cache is diagnostic only and can be up to one cadence old. Trading-state writes and order authorization checks are not deferred to it.

The resume-checkpoint transaction writes the latest connection draft, strategy draft, active cycle, `last_resume_checkpoint` metadata, and its audit event together. The controller normally performs this in the worker after applying safe active-cycle edits without re-evaluating the last market price. A bounded direct-storage fallback uses the same checkpoint ID, and the transaction begins with an immediate write lock so a delayed worker and fallback cannot duplicate the logical checkpoint.

The storage layer also creates restore-validated online backups and audit bundles. Its persisted BUY and SELL fills define the application-owned unsold quantity used by BUY gating, Stop, window-close, and Reconciliation actions. It is not the sole source for live order status; recovery compares it with broker facts.

## Threading and signal model

- The Qt GUI runs in the main thread.
- `TradingController` runs a dedicated Python worker thread.
- GUI commands are queued to the worker and wake its interruptible wait immediately.
- Broker, strategy, GUI, database-snapshot, and maintenance deadlines are independent, but all controller/broker side effects remain serialized on the same worker.
- Worker snapshots and events are emitted back through Qt signals.
- A lightweight headless signal implementation is used only by tests/build validation when `IBKR_BOT_HEADLESS_SIGNALS=1`.
- Broker calls are kept in the worker path to avoid concurrent access from GUI callbacks.
- Market-capture ZIP writing uses a separate bounded writer path after the full post-fill window is available.

## Timekeeping

All app-generated timestamps are recorded and displayed in UTC. This includes cycle rows, order/fill records, audit/decision/broker events, recovery snapshots, readable reports, and market-data captures.

The GUI can show workstation-local time alongside UTC for comparison. Imported timestamps without timezone information are interpreted as UTC for audit alignment rather than as the workstation’s local zone.

The adapter parses the active exact contract's date-specific IBKR `liquidHours` and `timeZoneId` into explicit regular-session open/close boundaries. The controller uses those same boundaries for the base RTH decision, first/last-minute BUY blockers, active-BUY cancellation, and optional pre-close liquidation, so exchange half days are handled consistently. The conservative weekday 09:30–16:00 New York fallback is available only for recognized U.S. equity primary exchanges. A non-U.S. contract with missing, invalid, or unparseable metadata fails closed.

## Order ownership boundary

Every application-created order receives an `OrderRef` beginning with:

```text
IBKRBOT|
```

The prefix identifies the application family but is not the ownership boundary. Open-order recovery, cancellation, error attribution, and execution application require the full reference to match a value already persisted in the local database. An unmatched prefixed Master-feed order is left unowned rather than assigned to the active cycle.

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

A local API socket and the Gateway/TWS upstream IBKR link are independent. Loss of an enabled local socket starts fixed ten-second reconnect attempts that continue indefinitely until success, manual Disconnect, or shutdown. An upstream outage invalidates quote freshness and pauses the worker without pretending the local socket closed or repeatedly tearing it down. After 1101/1102 restoration, app-owned open orders and recent executions are reconciled before normal processing resumes. A fresh post-recovery ticker event is still required before cached quote fields become strategy-usable.

A stored active cycle requires an explicit Start/resume after launch. The application does not automatically recreate missing orders when facts are uncertain.

## Market-data capture architecture

`app/market_data_capture.py` keeps a bounded rolling buffer in RAM. A fill creates a capture session containing the available pre-event window and accumulating post-event rows. The complete package is written to `debug_captures/` only after the post window finishes. No partial capture is flushed on shutdown.

This design avoids continuous disk writes but means a crash or early shutdown loses incomplete capture data. ATR observation history is likewise in-memory session state: it collects only during open RTH, continues while adaptation is disabled, and resets on process restart. Only actual market-data update events enter ATR/volatility history. Re-reading populated cached fields changes diagnostics only; it does not create a sample. The original callback time determines the observation bucket.

## Single-instance boundary

`app/lockfile.py` prevents another process from acquiring the same lock path in the same portable folder. The lock contains the PID and performs a Windows-safe process-existence check before removing a stale lock.

It cannot prevent a copy of the project in another folder from using another database or API client ID. Operational uniqueness still requires deliberate configuration.
