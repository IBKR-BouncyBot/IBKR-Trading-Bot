"""Stage-boundary policy for risk edits; never rewrites a working broker order."""
from __future__ import annotations

from copy import copy
from math import isfinite

from .models import CycleState, Stage, StrategySettings

# These checks also participate in arming a NEW final SELL. A Stage-2 BUY keeps
# its original cancellation/safety policy until its terminal status is known.
NEXT_ORDER_QUOTE_GUARD_FIELDS = (
    "max_spread_pct",
    "block_delayed_data_in_live",
    "stale_data_guard_enabled",
    "max_selected_price_age_seconds",
    "max_bid_ask_age_seconds",
    "max_rth_status_age_seconds",
)
NEXT_BUY_RISK_FIELDS = (
    "atr_block_new_buy_until_ready",
    "hard_risk_limits_enabled",
    "max_daily_loss_ticker",
    "max_daily_loss_total",
    "max_cycles_per_ticker_day",
    "max_consecutive_losses",
    "min_trade_price",
    "max_gap_from_prev_close_pct",
    "what_if_check_enabled",
    "volatility_filter_enabled",
    "volatility_window_seconds",
    "max_recent_price_move_pct",
    "session_timing_guard_enabled",
    "no_new_buy_first_minutes",
    "no_new_buy_last_minutes",
    "cancel_buy_before_close_minutes",
)
NEXT_ORDER_RISK_FIELDS = NEXT_ORDER_QUOTE_GUARD_FIELDS + NEXT_BUY_RISK_FIELDS


def settings_match_cycle(cycle: CycleState, settings: StrategySettings) -> bool:
    """A draft for another contract must not alter this cycle or auto-repeat."""
    return bool(
        settings.normalized_ticker() == cycle.ticker
        and settings.currency == cycle.currency
        and settings.exchange == cycle.exchange
        and (not settings.contract_con_id or settings.contract_con_id == cycle.con_id)
        and (not settings.primary_exchange or settings.primary_exchange == cycle.primary_exchange)
    )


def apply_waiting_order_guards(cycle: CycleState, settings: StrategySettings) -> tuple[CycleState, list[str]]:
    """Apply only before a new order, including entry into Stage 3 after recovery.

    Stages 2/4 keep their original fields for ongoing broker supervision. The
    draft is persisted separately by the existing settings workflow.
    """
    if not settings_match_cycle(cycle, settings):
        return cycle, []
    fields = (
        NEXT_ORDER_RISK_FIELDS if cycle.stage == Stage.WAIT_INITIAL_DROP
        else NEXT_ORDER_QUOTE_GUARD_FIELDS if cycle.stage == Stage.WAIT_RISE_TRIGGER
        else ()
    )
    changed = [field for field in fields if getattr(cycle, field) != getattr(settings, field)]
    if not changed:
        return cycle, []
    updated = copy(cycle)
    for field in changed:
        setattr(updated, field, getattr(settings, field))
    updated.touch()
    return updated, changed


def apply_repeat_order_guards(settings: StrategySettings, cycle: CycleState, draft: StrategySettings) -> StrategySettings:
    """Carry reviewed edits to the next BUY without adopting unrelated drafts."""
    if not settings_match_cycle(cycle, draft):
        return settings
    updated = copy(settings)
    for field in NEXT_ORDER_RISK_FIELDS:
        setattr(updated, field, getattr(draft, field))
    return updated


def risk_edit_request(cycle: CycleState, settings: StrategySettings) -> dict:
    """Persist edit intent, not an unscoped copy of a potentially stale draft."""
    return {
        "version": 1,
        "cycle_id": cycle.id,
        "account": cycle.account,
        "identity": {
            "ticker": cycle.ticker, "contract_con_id": cycle.con_id,
            "currency": cycle.currency, "exchange": cycle.exchange,
            "primary_exchange": cycle.primary_exchange,
        },
        "values": {name: getattr(settings, name) for name in NEXT_ORDER_RISK_FIELDS},
    }


def pending_risk_settings(record: object, cycle: CycleState) -> StrategySettings | None:
    """Only an explicit saved edit for this exact cycle is an automatic override."""
    try:
        if not isinstance(record, dict) or record.get("version") != 1 or record.get("cycle_id") != cycle.id or record.get("account") != cycle.account:
            return None
        values = record["values"]
        if set(values) != set(NEXT_ORDER_RISK_FIELDS):
            return None
        candidate = StrategySettings(**record["identity"], **values)
        if not settings_match_cycle(cycle, candidate) or candidate.validate():
            return None
        defaults = StrategySettings()
        for name, value in values.items():
            default = getattr(defaults, name)
            if isinstance(default, bool) and not isinstance(value, bool):
                return None
            if isinstance(default, (float, int)) and not isinstance(default, bool) and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or value < 0
                or (isinstance(default, int) and int(value) != value)
            ):
                return None
            if name in {"max_selected_price_age_seconds", "max_bid_ask_age_seconds", "max_rth_status_age_seconds", "volatility_window_seconds"} and value <= 0:
                return None
        return candidate
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None
