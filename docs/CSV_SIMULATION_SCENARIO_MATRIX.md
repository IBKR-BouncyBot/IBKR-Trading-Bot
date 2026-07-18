# Deterministic CSV simulation scenario matrix

This v3.0.18 test-only matrix contains 58 explicit scenario contracts across 54 CSV price-path files. It extends verification without changing `app/` or `main.py`.

Every scenario asserts an exact final stage and event sequence. Applicable contracts also assert quantities, trigger/fill prices, order payloads, P/L direction and arithmetic, budget exposure, minimum-profit preservation, RTH blocking/reopening, protective exits, and error text. Shared invariants reject overfills, duplicate lifecycle events, SELL-before-BUY ordering, non-app order references, non-maximal whole-share sizing, and inconsistent completed-cycle P/L.

## Coverage by category

| Category | Contracts |
|---|---:|
| `anchor-entry` | 8 |
| `buy-execution` | 6 |
| `endurance` | 3 |
| `lifecycle` | 3 |
| `numeric` | 4 |
| `protective-exit` | 7 |
| `rth` | 5 |
| `sell-execution` | 7 |
| `sizing-slippage` | 15 |

## Scenario catalog

| Category | Scenario | CSV file | Contract purpose |
|---|---|---|---|
| `anchor-entry` | `anchor_reset_multiple` | `anchor_reset_multiple.csv` | Multiple higher prices reset the Stage 1 anchor before one BUY trail is submitted. |
| `buy-execution` | `buy_trail_keeps_falling` | `buy_trail_keeps_falling.csv` | A native BUY trail follows successive lows and fills only at its final rebound stop. |
| `lifecycle` | `full_profitable_cycle` | `full_profitable_cycle.csv` | A normal trailing BUY, profit activation, trailing SELL, and profitable completion. |
| `anchor-entry` | `long_anchor_reset_then_drop` | `long_anchor_reset_then_drop.csv` | A longer rising anchor sequence eventually drops, enters, and holds the filled position. |
| `endurance` | `long_flat_runtime` | `long_flat_runtime.csv` | Hundreds of quiet ticks create no order and leave only the latest upward anchor. |
| `anchor-entry` | `no_initial_drop` | `no_initial_drop.csv` | A monotonically rising path never satisfies the initial drop condition. |
| `sell-execution` | `no_sell_trigger_holds_position` | `no_sell_trigger_holds_position.csv` | A filled position remains open while every later price stays below the safe SELL trigger. |
| `endurance` | `prolonged_no_order_anchor_reset` | `prolonged_no_order_anchor_reset.csv` | A prolonged slow rise repeatedly resets the anchor without accumulating orders or fills. |
| `protective-exit` | `protective_cancel_then_profit_sell` | `protective_cancel_then_profit_sell.csv` | The protective SELL is cancelled before a lower-profit final SELL trail is installed. |
| `protective-exit` | `protective_replaced_by_profit_sell` | `protective_replaced_by_profit_sell.csv` | A working protective trail is cancelled and replaced before the final profitable exit. |
| `protective-exit` | `protective_sell_exits_before_profit` | `protective_sell_exits_before_profit.csv` | The protective trail closes the position before the configured minimum-profit trigger. |
| `protective-exit` | `protective_sell_loss` | `protective_sell_loss.csv` | A falling market exercises the protective-loss completion and P/L path. |
| `rth` | `rth_reopens_after_drop` | `rth_reopens_after_drop.csv` | A qualifying drop is blocked while RTH is closed and submitted only after RTH reopens. |
| `sizing-slippage` | `slippage_buffer_budget` | `slippage_buffer_budget.csv` | A five-percent sizing buffer lowers quantity and raises the safe final SELL trigger. |
| `sizing-slippage` | `slippage_sizing_wide_rebound` | `slippage_sizing_wide_rebound.csv` | A wide rebound fills above the stop while buffered sizing keeps actual notional within budget. |
| `anchor-entry` | `initial_drop_exact_boundary` | `initial_drop_exact_boundary.csv` | The initial drop comparison is inclusive at the exact configured threshold. |
| `anchor-entry` | `initial_drop_just_above_boundary` | `initial_drop_just_above_boundary.csv` | A price one ten-thousandth above the drop threshold must not create a BUY order. |
| `anchor-entry` | `anchor_reset_exact_boundary` | `anchor_reset_exact_boundary.csv` | The exact drop is calculated from the latest higher anchor, not the original price. |
| `anchor-entry` | `anchor_reset_just_above_boundary` | `anchor_reset_just_above_boundary.csv` | A reset-anchor price just above the inclusive boundary remains order-free. |
| `lifecycle` | `gap_below_drop_full_cycle` | `gap_below_drop_full_cycle.csv` | A gap through the drop threshold still produces one coherent BUY-to-SELL lifecycle. |
| `anchor-entry` | `choppy_pre_entry_single_order` | `choppy_pre_entry_single_order.csv` | Choppy pre-entry prices reset the anchor yet emit only one BUY submission. |
| `buy-execution` | `buy_rebound_exact_boundary` | `buy_rebound_exact_boundary.csv` | The BUY trail fills at the exact ratcheted stop price. |
| `buy-execution` | `buy_rebound_just_below_boundary` | `buy_rebound_just_below_boundary.csv` | The BUY trail remains working one ten-thousandth below its stop. |
| `buy-execution` | `buy_trail_multiple_lows_exact_fill` | `buy_trail_multiple_lows_exact_fill.csv` | Several lower lows ratchet the BUY stop down without moving it in the adverse direction. |
| `sizing-slippage` | `buy_gap_above_stop` | `buy_gap_above_stop.csv` | A gap above a native BUY stop records the observed fill and bases profit protection on that fill. |
| `endurance` | `buy_trail_long_hold` | `buy_trail_long_hold.csv` | A long sequence that never reaches the rebound stop leaves one BUY trail active. |
| `buy-execution` | `zero_buy_trail_market_entry` | `zero_buy_trail_market_entry.csv` | A zero-percent BUY trail uses the market-order branch and completes normally. |
| `buy-execution` | `partial_buy_40_percent_cycle` | `partial_buy_40_percent_cycle.csv` | A partial BUY cancels the remainder and sizes the final SELL to the four filled shares. |
| `sizing-slippage` | `insufficient_budget_zero_quantity` | `insufficient_budget_zero_quantity.csv` | A budget below one projected share enters ERROR without emitting an order. |
| `numeric` | `one_share_high_price_cycle` | `one_share_high_price_cycle.csv` | A high-priced instrument correctly sizes and completes a one-share cycle. |
| `numeric` | `low_price_large_quantity_cycle` | `low_price_large_quantity_cycle.csv` | A low-priced instrument exercises four-decimal triggers and a four-digit whole-share quantity. |
| `numeric` | `four_decimal_rounding_cycle` | `four_decimal_rounding_cycle.csv` | Non-round prices verify four-decimal trigger and stop rounding throughout a cycle. |
| `sell-execution` | `rise_trigger_exact_boundary` | `rise_trigger_exact_boundary.csv` | The final SELL trail is submitted at the exact safe minimum-profit activation price. |
| `sell-execution` | `rise_trigger_just_below_boundary` | `rise_trigger_just_below_boundary.csv` | No final SELL order is created one ten-thousandth below the safe activation price. |
| `sell-execution` | `sell_stop_exact_boundary` | `sell_stop_exact_boundary.csv` | The native SELL trail fills at the exact ratcheted stop. |
| `sell-execution` | `sell_stop_just_above_boundary` | `sell_stop_just_above_boundary.csv` | The native SELL trail remains working one ten-thousandth above the stop. |
| `sell-execution` | `sell_trail_multiple_highs_exact_fill` | `sell_trail_multiple_highs_exact_fill.csv` | Successive highs ratchet the SELL stop upward before an exact-boundary exit. |
| `sizing-slippage` | `sell_gap_below_stop` | `sell_gap_below_stop.csv` | A gap below the SELL stop fills at the observed lower price while preserving coherent P/L. |
| `sell-execution` | `zero_sell_trail_market_exit` | `zero_sell_trail_market_exit.csv` | A zero-percent final SELL trail uses a market order at the exact profit threshold. |
| `lifecycle` | `zero_both_trails_market_cycle` | `zero_both_trails_market_cycle.csv` | Both zero-trail branches use market orders without duplicating or skipping a leg. |
| `protective-exit` | `protective_exact_stop` | `protective_exact_stop.csv` | The initial protective SELL stop is inclusive at its exact three-percent boundary. |
| `protective-exit` | `protective_ratchet_gain` | `protective_ratchet_gain.csv` | A protective trail ratchets above the BUY fill and exits with a small gain before final activation. |
| `protective-exit` | `protective_partial_fill_quantity` | `protective_partial_fill_quantity.csv` | After a forty-percent BUY fill, the protective order and exit use exactly four shares. |
| `rth` | `rth_closed_entire_path` | `rth_closed_entire_path.csv` | A qualifying path entirely outside RTH creates no app-side BUY order. |
| `rth` | `rth_guard_disabled_closed_cycle` | `rth_guard_disabled_closed_cycle.csv` | When rth_only is explicitly disabled, closed flags do not block an otherwise complete cycle. |
| `rth` | `rth_closed_sell_trigger_then_reopen` | `rth_closed_sell_trigger_then_reopen.csv` | A new SELL is blocked while closed, then submitted once RTH reopens without duplicating it. |
| `rth` | `rth_closed_existing_sell_trail` | `rth_closed_existing_sell_trail.csv` | An already-working native SELL trail continues and fills after the RTH flag closes. |
| `sizing-slippage` | `combined_slippage_positive` | `combined_slippage_positive.csv` | One-percent adverse BUY and SELL slippage still produces a correctly calculated positive result. |
| `sizing-slippage` | `severe_unbuffered_slippage_loss` | `severe_unbuffered_slippage_loss.csv` | Severe unbuffered adverse fills can turn a nominal profit path into a small realized loss. |
| `sizing-slippage` | `reinvest_profit_quantity_cycle` | `reinvest_profit_quantity_cycle.csv` | Positive realized profit is added to the budget and increases planned quantity from ten to fifteen. |
| `sizing-slippage` | `slippage_buffer_delays_sell` | `slippage_buffer_delays_sell.csv` | The buffered safe SELL trigger remains unarmed just below its computed boundary. |
| `sizing-slippage` | `slippage_buffer_protects_profit` | `slippage_buffer_protects_profit.csv` | A five-percent buffer preserves the ten-percent target despite a five-percent adverse SELL fill. |
| `sizing-slippage` | `gap_fill_budget_exposure` | `gap_fill_budget_exposure.csv` | Without a buffer, a BUY gap can make actual fill notional exceed the configured budget. |
| `numeric` | `minimum_profit_epsilon_cycle` | `minimum_profit_epsilon_cycle.csv` | The minimum allowed 0.01 percent profit target completes through the zero-SELL-trail branch. |
| `sizing-slippage` | `reinvest_disabled_ignores_profit` | `reinvest_profit_quantity_cycle.csv` | A positive realized result is ignored when reinvest_profits is disabled. |
| `sizing-slippage` | `negative_realized_profit_ignored` | `reinvest_profit_quantity_cycle.csv` | A negative realized result never reduces the next cycle budget, even with reinvestment enabled. |
| `sizing-slippage` | `gap_fill_with_15pct_sizing_buffer` | `gap_fill_budget_exposure.csv` | A fifteen-percent sizing buffer reduces gap exposure to eight shares and stays within budget. |
| `sizing-slippage` | `slippage_buffer_budget_disabled_control` | `slippage_buffer_budget.csv` | The unbuffered control path keeps ten shares and reaches its lower final SELL trigger. |

## Interpretation limits

These are deterministic strategy simulations. They do not reproduce exchange queue priority, all TWS trigger-method details, live latency, rejections, permissions, buying power, real market-data availability, server-side order handling, or actual disconnect timing. Passing the matrix detects rule and accounting regressions; it does not prove live profitability or end-to-end IBKR execution reliability.
