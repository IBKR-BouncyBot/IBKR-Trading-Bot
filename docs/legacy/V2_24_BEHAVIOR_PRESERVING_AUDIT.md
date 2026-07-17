# v2.24 behavior-preserving code audit

> **Archived release note.** This file describes an earlier release and is not the current operating specification. See the [project README](../../README.md), [current documentation index](../README.md), and [changelog](../../CHANGELOG.md).

Scope: scrutinize the uploaded v2.24 app for defects and performance issues without changing trading strategy behavior, order math, GUI behavior, persistence schema, or user-facing defaults.

## Baseline before changes

- `python3 -m pytest -q`: 313 passed.
- `python3 scripts/run_all_simulations.py`: 18 CSV simulation files/scenarios passed.
- Added regression tests before applying code fixes. The malformed-input validation tests failed against the original code because validation raised `AttributeError` before returning errors.

## Changes made

### Validation robustness

Files changed:

- `app/models.py`
- `tests/test_v225_validation_robustness.py`

Details:

- Added `_validation_float()` and `_validation_int()` helper functions for validation-only coercion.
- Hardened `ConnectionSettings.validate()` so malformed `host`, `port`, `client_id`, `trading_mode`, `platform`, and `market_data_type` values return validation errors instead of raising exceptions.
- Hardened `StrategySettings.normalized_ticker()` and `StrategySettings.validate()` so malformed numeric/string fields return validation errors instead of raising exceptions.
- Preserved existing validation messages for normal invalid values such as negative percentages, out-of-range ports, and unsupported security/currency/routing values.

Reason:

- Validation should fail closed and report input errors. It should not crash the controller or GUI if a persisted JSON setting or external caller supplies malformed values.

### Strategy-state copy performance

Files changed:

- `app/strategy.py`
- `tests/test_v225_strategy_copy_safety.py`

Details:

- Replaced `deepcopy(cycle)` with `copy(cycle)` in the pure strategy state-machine functions.
- Added a regression test proving `CycleState` currently has no mutable runtime fields, which makes shallow copies behavior-equivalent for these state updates.
- Added a regression test proving price updates still return a new cycle object and do not mutate the original cycle.

Reason:

- Price updates are a high-frequency path. `CycleState` is a scalar dataclass, so deep-copying every price update was unnecessary overhead.

Validation:

- Deterministic strategy parity probe against the original uploaded code produced a zero-line diff after normalizing timestamp fields.
- Micro-benchmark for 30,000 `StrategyEngine.on_price_update()` calls:
  - Original: 2.205279 seconds.
  - Patched: 0.520066 seconds.
  - Measured speedup: 4.24x for that isolated state-copy/update path.

### Monotonic throttling

Files changed:

- `app/controller.py`
- `tests/test_v225_controller_monotonic_throttle.py`

Details:

- Replaced wall-clock `time.time()` with `time.monotonic()` for snapshot and warning throttling.
- Added a regression test proving warning throttling is controlled by monotonic time even if wall-clock time moves backwards.

Reason:

- Rate limits and throttle intervals should use a monotonic clock. Wall-clock adjustments can otherwise suppress or burst warnings/snapshots unexpectedly.

## Static checks performed

A small AST audit found no issues for:

- Duplicate top-level function/class definitions.
- Duplicate methods within classes.
- Mutable literal default arguments.
- Duplicate literal dictionary keys.

`python3 -m compileall -q app tests scripts` completed successfully.

## Final validation after changes

- `python3 -m pytest -q`: 319 passed.
- `python3 scripts/run_all_simulations.py`: 18 CSV simulation files/scenarios passed.
- Deterministic strategy parity probe versus the original uploaded code: zero diff after ignoring timestamp-only fields.

## Behavior intentionally not changed

- No trading thresholds, ATR formulas, order type decisions, stage transitions, GUI defaults, persistence schema, or IBKR adapter order behavior were changed.
- Broad exception handlers in broker/GUI recovery paths were reviewed but left unchanged where they deliberately keep the app fail-closed or keep UI/debug reporting alive during broker/API faults.
