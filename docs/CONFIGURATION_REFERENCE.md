# Configuration reference

This document describes the persisted connection and strategy settings in v3.1.1. Values shown as defaults are the dataclass defaults used for a new configuration. Saved SQLite settings override them after the first run.

## Connection settings

| Setting | Default | Meaning |
|---|---:|---|
| Platform | IB Gateway | Selects the TWS or IB Gateway profile family. Both use the TWS socket API. |
| Trading mode | Live | Selected by the connection profile. Controls warnings and live-data/account checks; it is not inferred from the port. |
| Host | `127.0.0.1` | TWS/Gateway API host. |
| Port | `4001` | Socket port. The profile selector supplies the standard defaults. |
| Client ID | `11` | IBKR API client ID. It must not conflict with another connected client that uses the same session. |
| Account | blank | Optional routing override. Blank leaves `Order.account` unset and lets IBKR select the account. |
| Platform executable path | blank | Optional path used only by the Start TWS/Gateway helper. It is not a credential store. |
| Market-data type | `0` (auto) | `0` best available, `1` live, `2` frozen, `3` delayed, `4` delayed-frozen. |

Standard profiles:

| Profile | Mode | Host | Port |
|---|---|---|---:|
| IB Gateway Live | live | `127.0.0.1` | `4001` |
| IB Gateway Paper | paper | `127.0.0.1` | `4002` |
| TWS Live | live | `127.0.0.1` | `7496` |
| TWS Paper | paper | `127.0.0.1` | `7497` |

The profile selector supplies platform, mode, host, and port. Host and port remain editable for nonstandard installations.

## Instrument and order settings

| Setting | Default | Meaning |
|---|---:|---|
| Ticker | blank | Required stock symbol. The selected API match should be confirmed before starting. |
| Investment amount | `10000.00` | Base budget used for whole-share sizing. |
| Exchange | `SMART` | Required strategy routing value. |
| Primary exchange | blank | Optional native exchange used to disambiguate a stock contract. |
| Contract conId | blank | Optional exact IBKR contract ID selected by contract search. |
| Currency | `USD` | Required strategy currency. |
| Security type | `STK` | Required strategy contract type. |
| Time in force | `GTC` | Used for submitted strategy orders. |
| RTH only | on | Strategy orders use `outsideRth=False`; new submissions also require an open/known regular session. |

The application supports one confirmed long stock cycle at a time. It does not expose settings for shorting, options, non-USD contracts, or non-SMART routing.

## Manual strategy percentages

| Setting | Default | Meaning |
|---|---:|---|
| Initial drop | `2.00%` | Drop from the current Stage-1 anchor before entry logic becomes eligible. Must be positive. |
| BUY rebound/trail | `1.00%` | Native BUY trailing percentage after the drop. Zero changes the entry to a market BUY at the drop condition. |
| Minimum profit | `3.00%` | Gross minimum initial SELL-stop level above the actual average BUY fill. Internally stored as `rise_trigger_pct` for database compatibility. |
| SELL trailing stop | `1.00%` | Native final SELL trail. Zero changes the final exit to a market SELL once the minimum-profit condition is met. |

When ATR adaptation supplies a valid value, it rewrites the order-driving percentages used by the same strategy path. Manual values remain the saved fallback/configuration values and continue to apply to fields whose ATR toggle is off.

## ATR-adaptive settings

ATR is calculated from actual ticker-update events observed by this running application. Repeated reads of cached non-null fields are excluded. Usable events are collected only while the regular session is open, bucketed by the broker callback arrival time into fixed-time OHLC bars, and converted to ATR%. Observation/bar collection continues when **Use ATR adaptive percentages** is off; disabling adaptation only prevents calculated values from changing strategy percentages. The buffer is in memory, resets when the process restarts, and is not a separate broker historical-bar feed.

| Setting | Default | Meaning |
|---|---:|---|
| Use ATR adaptive percentages | on | Enables ATR-derived strategy percentages when enough data exists. Turning it off does not stop current-session RTH observation/bar collection. |
| Adapt Minimum profit with ATR | on | Applies the minimum-profit multiplier. Off retains the manual minimum-profit value. |
| Block new BUY until ATR has enough RTH data | on | Leaves Stage 1 without an armed initial-drop trigger during warmup. |
| Adapt Protective SELL with ATR | off | Applies the protective multiplier only when both protective SELL and this option are enabled. |
| ATR period | `14` | Number of true-range periods. Readiness requires at least `period + 1` observed bar buckets; the newest bucket may still be forming. |
| ATR bar duration | `60 seconds` | Width of application-observed OHLC bars. |
| Initial-drop multiplier | `1.50` | `ATR% × multiplier`, clamped to the configured range. |
| BUY-rebound multiplier | `0.75` | `ATR% × multiplier`, clamped. Zero is allowed where configured. |
| Minimum-profit multiplier | `1.00` | `ATR% × multiplier`, clamped when adaptation is enabled for this field. |
| SELL-trail multiplier | `1.00` | `ATR% × multiplier`, clamped. |
| Protective-SELL multiplier | `3.00` | Used only when protective adaptation is enabled. |
| Minimum adaptive percentage | `0.10%` | Lower clamp. |
| Maximum adaptive percentage | `20.00%` | Upper clamp. |

### ATR warmup semantics

With both ATR mode and the warmup blocker enabled:

- pre-readiness prices update only the Stage-1 reference;
- `drop_trigger_price` remains unset;
- the manual initial-drop percentage is not armed;
- the readiness update creates a fresh anchor using the ready price;
- a subsequent update is required before the ATR-derived drop can trigger entry.

If the warmup blocker is off, the strategy can use the currently configured percentages before ATR becomes ready.

## Protective SELL

| Setting | Default | Meaning |
|---|---:|---|
| Enable Protective SELL | off | Submit a native SELL trail after a positive BUY fill. |
| Protective SELL trailing stop | `3.00%` | Manual trail unless protective ATR adaptation is enabled and ready. |

The protective order is a loss-limiting mechanism, not a guaranteed stop. If the normal profit exit becomes eligible, the controller cancels the protective SELL and waits until it is no longer working before submitting the final SELL.

## Slippage planning

| Setting | Default | Meaning |
|---|---:|---|
| Enable slippage buffer | off | Applies a planning assumption to sizing and minimum-profit activation. It does not change IBKR’s execution mechanism. |
| Slippage buffer | `0.50%` | Raises the BUY sizing price and the required initial SELL stop. |

For BUY sizing:

```text
sizing price = projected BUY stop × (1 + buffer % / 100)
```

For minimum-profit planning:

```text
required initial SELL stop = unbuffered stop / (1 - buffer % / 100)
```

This cannot guarantee a fill within the assumed buffer.

## Repetition and budget

| Setting | Default | Meaning |
|---|---:|---|
| Reinvest profits | on | Adds positive completed application net P/L for the ticker to the base investment amount. Losses do not reduce the base. |
| Auto repeat | on | Starts another cycle after completion unless stop-after-current-cycle is active or the enabled maximum completed-cycle cap has been reached. |

The reinvestment calculation uses completed cycles stored by this application, not account-wide IBKR P/L.

## Data-quality and timing guards

| Setting | Default | Meaning |
|---|---:|---|
| Block delayed data in live mode | on | Blocks a live BUY when the effective data type is not live. |
| Run IBKR what-if check before BUY | on | Uses IBKR's dedicated what-if path; missing/error state or absent finite margin/equity output blocks the live BUY. |
| Enable stale-data guard | on | Requires current selected price, bid/ask, and RTH status. |
| Maximum selected-price age | `3.0 s` | Maximum accepted age for the confirmed strategy price. |
| Maximum bid/ask age | `3.0 s` | Maximum accepted age for bid/ask fields. |
| Maximum RTH-status age | `60.0 s` | Maximum accepted age for the last RTH evaluation. |
| Enable recent-volatility filter | off | Blocks a new BUY when the observed range exceeds the configured percentage. |
| Volatility window | `300 s` | Lookback over application-observed usable prices. |
| Maximum recent price move | `5.00%` | Maximum observed range allowed when the filter is enabled. |
| Enable session-timing guard | on | Applies first/last-minute entry windows and pre-close BUY-trail cancellation. |
| No new BUY first | `5 min` | Entry block after the regular-session open. Zero disables this sub-window. |
| No new BUY last | `15 min` | Entry block before the regular-session close. Zero disables this sub-window. |
| Cancel BUY before close | `5 min` | Requests cancellation of an unfilled app BUY trail before the contract's date-specific regular-session close. Zero disables this sub-window. |
| Cancel SELL trail and liquidate before close | off | Stage 4 only. Requests cancellation of the final native SELL trail at the configured pre-close cutoff, waits for a terminal broker status, then submits one RTH-only `DAY` market SELL for the remaining app-owned shares. |
| Liquidate before close | `5 min` | Cutoff before the contract-specific RTH close. Valid range `1-240 min`. The field is active only when the optional policy is enabled. |

The first/last-minute entry windows, BUY cancellation window, and optional Stage-4 liquidation cutoff use the current contract's IBKR `liquidHours` boundaries and contract timezone. This includes date-specific early closes. If IBKR does not provide usable contract hours, the adapter exposes its existing conservative US-equity fallback; if no usable boundary is available at all, a new BUY fails closed and automatic pre-close cancellation is not guessed.

The data-type, what-if, stale-data, ATR, RTH, and controller-state checks are independent of the optional hard-risk master where implemented. Turning off hard limits does not turn off the normal broker/data safety checks. Local socket state, Gateway/TWS upstream IBKR connectivity, post-reconnect reconciliation, and the requirement for an actual post-connect/post-recovery ticker event are controller invariants rather than user-disableable settings.

## Hard risk limits

The hard-risk master is off by default. When it is on, a zero value disables the corresponding individual limit.

| Setting | Default | Scope |
|---|---:|---|
| Enable hard risk limits | off | Master for the numeric loss/count/price/spread/gap checks below. |
| Maximum daily loss for ticker | `0` | Completed application net P/L for the selected ticker during the UTC trading date used by storage queries. |
| Maximum total daily loss | `0` | Completed application net P/L across stored tickers for the date. |
| Maximum completed cycles | `0` | Total completed-cycle cap for the selected ticker. The persisted field retains the historical name `max_cycles_per_ticker_day`, but runtime behavior is not per-day. |
| Maximum consecutive losses | `0` | Consecutive completed losing application cycles. |
| Maximum spread | `1.00%` | Fixed user-configured bid/ask spread limit at BUY preflight. Live bid/ask values are compared with it but never rewrite it. It changes only through explicit user edits or loading the persisted setting. Set zero to disable. |
| Minimum trade price | `0` | Selected price floor. Set zero to disable. |
| Maximum gap from previous close | `0` | Absolute percentage gap. Set zero to disable. |

Hard BUY limits do not prevent an already-open application-owned position from being sold. SELL eligibility still depends on broker connection, valid state, RTH/order constraints, and cancellation sequencing.

## Runtime-only cycle fields

The active `CycleState` stores a snapshot of settings and operational state, including:

- anchor, last price, drop trigger, and projected stop levels;
- order IDs, permanent IDs, order references, and statuses;
- filled quantities, average prices, commissions, and fill timestamps;
- protective-order cancellation state;
- gross/net P/L;
- recovery-required, requested-market-close, close-before-RTH cancellation/liquidation, stop-after-cycle, and error state.

These fields are persisted for restart/recovery. They are not all editable from the GUI.
