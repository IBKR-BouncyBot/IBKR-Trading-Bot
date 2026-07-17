# v3.0.8 GUI guards, ATR-entry warmup, and app-owned position scope

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

## Workflow input lock

The top-bar lock is an operator interaction lock. While engaged, it disables:

- editable connection and strategy controls;
- ticker search/selection/confirmation controls; and
- all five workflow buttons: Connect, Search/select ticker, Confirm ticker/get price, Start strategy, and Stop strategy.

History, reconciliation, view-mode selection, and tab navigation remain available. Unlocking does not blindly enable the buttons; it immediately reruns the normal command-state rules so each button returns to its appropriate Ready, Done, Blocked, or Error state.

## Trading status blocker display

The top-bar **Trading** box now receives structured blocker data from the controller. The visible value shows the affected side and first blocker, for example `BUY blocked: ATR 3/15 +1`. Hovering the Trading box shows every active blocker and its full explanation.

The evaluated blocker set includes, where applicable:

- disconnected broker API;
- closed regular trading hours when RTH-only operation is enabled;
- no usable current strategy price;
- ATR warmup not ready;
- stale or non-live market data;
- session open/close windows;
- recent volatility limits;
- spread, price-gap, minimum-price, loss, cycle-count, and loss-streak limits;
- unsold app-owned shares of the same ticker; and
- a retained failure from the most recent what-if, preflight, protective-cancel, or order-submission attempt.

The display is informational. The existing order-submission checks remain authoritative and fail closed.

## ATR warmup and Stage 1

When both **Use ATR adaptive percentages** and **Block new BUY until ATR has enough RTH data** are enabled, the initial-drop percentage is deliberately not evaluated before ATR is ready.

During warmup:

1. Stage 1 remains `WAIT_INITIAL_DROP`.
2. Each usable RTH price becomes the latest reference/anchor.
3. `drop_trigger_price` remains unset.
4. No BUY order identity or quantity is armed.

On the first tick after ATR becomes ready, the controller applies the ATR-derived percentages and creates a fresh Stage-1 anchor from that tick. That tick cannot itself trigger a BUY. A later tick must make the configured ATR-derived drop from the fresh anchor before the normal BUY rebound/market-order path can begin.

This prevents a price decline that occurred while ATR was unavailable from being carried forward as an immediately satisfied entry signal.

## Position scope for new BUYs

IBKR's account position combines shares acquired by this app with shares acquired manually or by another application. v3.0.8 therefore removes the account-wide broker-position check from BUY preflight.

A new app BUY is blocked only when the SQLite fill ledger records unsold app-owned shares for the same ticker. Remaining app quantity is computed from app-recorded BUY fills less the app-recorded final/protective SELL fill for each cycle. Completed fully sold cycles do not block. A cycle explicitly marked **manually handled** is excluded because the operator has confirmed that its unresolved local state was handled outside the app.

The change does not permit duplicate active app cycles: the existing active-cycle and app-order controls remain in place. It only prevents unrelated external holdings from being treated as app exposure.
