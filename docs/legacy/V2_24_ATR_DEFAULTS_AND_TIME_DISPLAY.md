# v2.24 ATR defaults, warmup guard, and operator time display

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

v2.24 changes startup defaults and related GUI text for ATR-supervised operation. Strategy math, IBKR order construction, and broker adapter order behavior are not changed. SQLite cycle rows receive additive fields so the new ATR warmup/protective-ATR settings persist with each cycle.

## Default ATR behavior

`Use ATR adaptive percentages` now defaults on. ATR still uses app-observed API prices during regular trading hours only. Outside RTH, the ATR buffer does not update and the currently visible strategy percentages remain in use.

`Block new BUY until ATR has enough RTH data` is a new default-on guard. When ATR mode is enabled but ATR is not ready, Stage 1 is shown as a yellow guard pause and no fresh BUY is submitted. This prevents the first cycle from unintentionally using manual fallback percentages while ATR mode is intended to drive the cycle.

The existing ATR defaults were reviewed and retained:

- period: 14 bars
- bar duration: 60 seconds
- Initial drop: 1.50x ATR
- BUY rebound/trail: 0.75x ATR
- Minimum profit: 1.00x ATR
- final SELL trailing-stop: 1.00x ATR
- optional Protective SELL trailing-stop: 3.00x ATR
- adaptive clamp range: 0.10% to 20.00%

These values are broad enough for app-observed one-minute bars and remain bounded by the existing min/max clamps. They are intentionally not optimized for one ticker or one volatility regime.

## Optional ATR Protective SELL

`Adapt Protective SELL trailing-stop % with ATR` is available but off by default. When enabled, the normal `Protective SELL trailing-stop %` field is written from ATR percent multiplied by the Protective SELL ATR multiplier. When disabled, the Protective SELL trailing-stop remains manual.

## Session timing default

`Block new BUY near open/close` now defaults on. It remains a configurable guard: it blocks new BUY entries during the configured first/last minutes of the regular session and can cancel unfilled BUY trails before close when configured.

## Time display

The Price data monitor now shows both current UTC and system-local time. SQLite audit rows and market-capture records are UTC-normalized; the system-local line is included only to help compare the app with Windows clock and broker workstation logs.
