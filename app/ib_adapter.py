"""IBKR TWS/Gateway socket API boundary.

The controller uses the abstract ``BrokerAdapter`` interface so strategy and
most recovery logic can be tested without a broker session. ``IbAsyncTwsAdapter``
implements contract search/qualification, data-mode and price selection, RTH
checks, optional account routing, what-if requests, native market/trailing order
construction, app-owned order filtering, status/fill polling, and recovery facts.

Broker position lookup remains available as a diagnostic/recovery fact; new-BUY
ownership is determined by the application fill ledger. Snapshot timestamps are
UTC so broker diagnostics align with SQLite and market captures.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time
from math import isfinite
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .models import APP_ORDER_PREFIX, utc_now_iso


class BrokerAdapterError(RuntimeError):
    """Raised for broker/API failures that should pause trading and reconnect."""

    pass


@dataclass(slots=True)
class OrderHandle:
    """Minimal order identity returned after IBKR accepts an order."""
    order_ref: str
    order_id: Optional[int]
    perm_id: Optional[int]
    status: str
    raw: dict[str, Any]


@dataclass(slots=True)
class PolledOrderState:
    """Normalized order state used by the controller across live and recovery paths."""
    order_ref: str
    order_id: Optional[int]
    perm_id: Optional[int]
    status: str
    filled: int
    remaining: int
    avg_fill_price: float
    commission: float
    executions: list[dict[str, Any]]
    raw: dict[str, Any]


@dataclass(slots=True)
class QualifiedContract:
    ticker: str
    con_id: Optional[int]
    raw: Any
    primary_exchange: str = ""
    local_symbol: str = ""
    trading_class: str = ""
    min_tick: float = 0.01


@dataclass(slots=True)
class ContractSearchResult:
    symbol: str
    sec_type: str = ""
    currency: str = ""
    exchange: str = ""
    primary_exchange: str = ""
    con_id: Optional[int] = None
    local_symbol: str = ""
    trading_class: str = ""
    description: str = ""
    derivative_sec_types: list[str] | None = None

    @property
    def supported(self) -> bool:
        return self.sec_type.upper() == "STK" and self.currency.upper() in {"", "USD"}

    def label(self) -> str:
        bits = [self.symbol or "-"]
        details: list[str] = []
        if self.sec_type:
            details.append(self.sec_type)
        if self.exchange:
            details.append(self.exchange)
        if self.primary_exchange and self.primary_exchange != self.exchange:
            details.append(f"primary {self.primary_exchange}")
        if self.currency:
            details.append(self.currency)
        if self.con_id is not None:
            details.append(f"conId {self.con_id}")
        if details:
            bits.append(" / ".join(details))
        name = self.description or self.local_symbol or self.trading_class
        if name and name != self.symbol:
            bits.append(name)
        if not self.supported:
            bits.append("not supported by v1 settings")
        return " | ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["label"] = self.label()
        data["supported"] = self.supported
        return data


@dataclass(slots=True)
class MarketPriceSnapshot:
    price: Optional[float]
    source: str
    requested_market_data_type: int
    subscription_market_data_type: Optional[int]
    fields: dict[str, Optional[float]]
    timestamp: str
    age_seconds: float = 0.0
    status: str = "No usable price"
    error: str = ""
    generic_ticks: str = ""
    request_exchange: str = ""
    request_primary_exchange: str = ""
    selected_market_data_type: Optional[int] = None
    auto_market_data: bool = False
    auto_attempts: list[dict[str, Any]] | None = None
    api_data_received: bool = False
    api_data_field_count: int = 0
    ticker_update_time: str = ""
    # ``None`` means the adapter cannot expose event identity (mainly test
    # doubles).  The live adapter assigns one monotonically increasing sequence
    # number per actual ib_async pendingTickersEvent update.  Re-reading the same
    # cached Ticker therefore returns the same value instead of pretending that
    # another quote arrived.
    market_data_update_sequence: Optional[int] = None
    market_data_subscription_id: str = ""
    market_data_update_received_at: str = ""
    market_data_update_age_seconds: Optional[float] = None
    market_data_event_tracking: bool = False
    market_data_event_tracking_available: bool = False
    upstream_connected: Optional[bool] = None
    upstream_state: str = ""
    upstream_message: str = ""
    upstream_error_code: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BrokerConnectivityStatus:
    """Local API and Gateway-to-IBKR connectivity are separate facts."""

    local_connected: bool
    upstream_connected: Optional[bool]
    state: str
    message: str
    error_code: Optional[int] = None
    changed_at: str = ""
    market_data_resubscribe_required: bool = False
    awaiting_fresh_market_data: bool = False
    market_data_event_tracking: bool = False

    @property
    def trading_ready(self) -> bool:
        return bool(self.local_connected and self.upstream_connected is True)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["trading_ready"] = self.trading_ready
        return data


@dataclass(slots=True)
class RthStatus:
    is_open: bool
    source: str
    message: str
    checked_at: str
    liquid_hours: str = ""
    time_zone: str = ""
    session_open: str = ""
    session_close: str = ""
    session_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BrokerAdapter:
    """Interface used by TradingController.

    Tests can provide a fake implementation of this interface without importing
    ib_async or talking to a real TWS/Gateway session.
    """

    def connect(self, host: str, port: int, client_id: int, market_data_type: int = 1) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def connectivity_status(self) -> BrokerConnectivityStatus:
        """Return local and upstream connectivity as independent facts.

        Test adapters written before v3.0.11 generally expose only
        ``is_connected``.  The interface default treats a live local connection
        as upstream-ready so those deterministic fakes remain compatible.  The
        production adapter overrides this method with IBKR error-code state.
        """
        local_connected = bool(self.is_connected())
        return BrokerConnectivityStatus(
            local_connected=local_connected,
            upstream_connected=local_connected,
            state="connected" if local_connected else "local_disconnected",
            message="Broker API connection is ready." if local_connected else "Broker API connection is disconnected.",
            changed_at=utc_now_iso(),
        )

    def process_events(self, timeout: float = 0.0) -> None:
        """Allow a synchronous adapter to dispatch queued broker callbacks."""
        return None

    def set_market_data_type(self, market_data_type: int) -> None:
        raise NotImplementedError

    def managed_accounts(self) -> list[str]:
        """Return IBKR account identifiers visible to this API session.

        This is display-only metadata for the GUI status bar. It must not be
        used to change order routing/account behavior unless the operator
        explicitly entered an account in the connection settings.
        """
        return []

    def search_stock_contracts(self, query: str, max_results: int = 16) -> list[ContractSearchResult]:
        raise NotImplementedError

    def qualify_stock(
        self,
        ticker: str,
        exchange: str,
        currency: str,
        primary_exchange: str = "",
        con_id: Optional[int] = None,
    ) -> QualifiedContract:
        raise NotImplementedError

    def price_snapshot(self, contract: QualifiedContract, timeout: float = 1.0) -> MarketPriceSnapshot:
        raise NotImplementedError

    def last_price(self, contract: QualifiedContract, timeout: float = 1.0) -> Optional[float]:
        return self.price_snapshot(contract, timeout=timeout).price

    def what_if_trailing_stop(
        self,
        *,
        contract: QualifiedContract,
        action: str,
        quantity: int,
        trailing_percent: float,
        initial_stop_price: float,
        order_ref: str,
        tif: str = "GTC",
        account: str = "",
        outside_rth: bool = False,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def what_if_market_order(
        self,
        *,
        contract: QualifiedContract,
        action: str,
        quantity: int,
        order_ref: str,
        tif: str = "GTC",
        account: str = "",
        outside_rth: bool = False,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def place_trailing_stop(
        self,
        *,
        contract: QualifiedContract,
        action: str,
        quantity: int,
        trailing_percent: float,
        initial_stop_price: float,
        order_ref: str,
        tif: str = "GTC",
        account: str = "",
        outside_rth: bool = False,
    ) -> OrderHandle:
        raise NotImplementedError

    def place_market_order(
        self,
        *,
        contract: QualifiedContract,
        action: str,
        quantity: int,
        order_ref: str,
        tif: str = "GTC",
        account: str = "",
        outside_rth: bool = False,
    ) -> OrderHandle:
        raise NotImplementedError

    def cancel_order(self, order_ref: str, order_id: Optional[int] = None) -> None:
        raise NotImplementedError

    def poll_order(self, order_ref: str) -> Optional[PolledOrderState]:
        raise NotImplementedError

    def open_app_orders(self) -> list[PolledOrderState]:
        raise NotImplementedError

    def recent_executions(self) -> list[dict[str, Any]]:
        return []

    def drain_broker_events(self) -> list[dict[str, Any]]:
        return []

    def position_size(self, contract: QualifiedContract, account: str = "") -> Optional[float]:
        return None

    def regular_trading_hours_status(self, contract: QualifiedContract) -> RthStatus:
        return RthStatus(True, "not_implemented", "RTH status not implemented by this adapter.", utc_now_iso())

    def recover_order_fill(
        self,
        *,
        order_ref: str,
        order_id: Optional[int] = None,
        perm_id: Optional[int] = None,
        ticker: str = "",
        account: str = "",
        action: str = "",
    ) -> Optional[PolledOrderState]:
        raise NotImplementedError


class IbAsyncTwsAdapter(BrokerAdapter):
    """Concrete BrokerAdapter backed by ib_async and the TWS socket API.

    The adapter caches live market-data subscriptions and open trades. It does
    not create, modify, or cancel manual TWS orders because every recovery and
    cancel path filters on the IBKRBOT| OrderRef prefix.
    """

    _GENERIC_TICK_LIST = "232"
    _AUTO_MODE_SEQUENCE = (1, 3, 2, 4)  # live, delayed, frozen, delayed-frozen.
    _AUTO_RESCAN_SECONDS = 60.0

    def __init__(self) -> None:
        self.ib: Any = None
        self._contracts: dict[str, Any] = {}
        self._contracts_by_con_id: dict[int, Any] = {}
        self._tickers: dict[tuple[int, str, str, str], Any] = {}
        self._trades_by_ref: dict[str, Any] = {}
        self._market_data_type = 0  # logical request: 0 means auto best available.
        self._active_market_data_type: Optional[int] = None  # actual TWS mode currently applied.
        self._auto_selected_market_data_type: Optional[int] = None
        self._last_auto_rescan_monotonic = 0.0
        self._search_cache: dict[tuple[str, int], list[ContractSearchResult]] = {}
        self._variant_cache: dict[tuple[str, str, str, str], QualifiedContract] = {}
        self._rth_cache: dict[int, tuple[float, RthStatus]] = {}
        self._broker_events: deque[dict[str, Any]] = deque(maxlen=1000)
        self._event_handlers_registered_for: Optional[int] = None
        self._last_open_trades_refresh_monotonic = 0.0
        self._open_trades_refresh_min_seconds = 5.0

        # A local socket to IB Gateway/TWS can stay connected while Gateway has
        # lost its upstream link to IBKR.  IBKR error codes 1100/1101/1102 drive
        # this state independently from ``IB.isConnected()``.
        self._upstream_connected: Optional[bool] = None
        self._upstream_state = "local_disconnected"
        self._upstream_message = "Broker API connection is disconnected."
        self._upstream_error_code: Optional[int] = None
        self._upstream_changed_at = utc_now_iso()
        self._market_data_resubscribe_required = False
        self._awaiting_fresh_market_data = False

        # Streaming Ticker objects retain their last values.  Event metadata lets
        # the controller distinguish a new pendingTickersEvent from another read
        # of those cached fields.
        self._market_data_event_tracking_available = False
        self._market_data_update_sequence = 0
        self._market_data_subscription_generation = 0
        self._ticker_keys_by_id: dict[int, tuple[int, str, str, str]] = {}
        self._ticker_update_meta: dict[int, dict[str, Any]] = {}

    def _register_broker_event_handlers(self) -> None:
        if self.ib is None:
            return
        ib_id = id(self.ib)
        if self._event_handlers_registered_for == ib_id:
            return

        def attach(event_name: str, callback: Any) -> bool:
            event = getattr(self.ib, event_name, None)
            if event is None:
                return False
            if hasattr(event, "connect"):
                try:
                    event.connect(callback)
                    return True
                except Exception:
                    pass
            try:
                event += callback
                return True
            except Exception:
                return False

        attach("openOrderEvent", lambda *args, **kwargs: self._record_broker_event("OPEN_ORDER", *args, **kwargs))
        attach("orderStatusEvent", lambda *args, **kwargs: self._record_broker_event("ORDER_STATUS", *args, **kwargs))
        attach("execDetailsEvent", lambda *args, **kwargs: self._record_broker_event("EXEC_DETAILS", *args, **kwargs))
        attach("commissionReportEvent", lambda *args, **kwargs: self._record_broker_event("COMMISSION_REPORT", *args, **kwargs))
        attach("errorEvent", self._on_ib_error)
        attach("disconnectedEvent", self._on_api_disconnected)
        self._market_data_event_tracking_available = attach("pendingTickersEvent", self._on_pending_tickers)
        self._event_handlers_registered_for = ib_id

    def _append_connectivity_event(
        self,
        event_type: str,
        *,
        error_code: Optional[int] = None,
        message: str = "",
        request_id: Optional[int] = None,
    ) -> None:
        self._broker_events.append(
            {
                "event_type": event_type,
                "created_at": utc_now_iso(),
                "order_ref": "",
                "error_code": error_code,
                "request_id": request_id,
                "message": message,
                "local_connected": self.is_connected(),
                "upstream_connected": self._upstream_connected,
                "upstream_state": self._upstream_state,
                "market_data_resubscribe_required": self._market_data_resubscribe_required,
                "awaiting_fresh_market_data": self._awaiting_fresh_market_data,
            }
        )

    def _set_upstream_state(
        self,
        *,
        connected: Optional[bool],
        state: str,
        message: str,
        error_code: Optional[int] = None,
        resubscribe_required: Optional[bool] = None,
        awaiting_fresh_market_data: Optional[bool] = None,
    ) -> None:
        self._upstream_connected = connected
        self._upstream_state = state
        self._upstream_message = message
        self._upstream_error_code = error_code
        self._upstream_changed_at = utc_now_iso()
        if resubscribe_required is not None:
            self._market_data_resubscribe_required = bool(resubscribe_required)
        if awaiting_fresh_market_data is not None:
            self._awaiting_fresh_market_data = bool(awaiting_fresh_market_data)

    def _invalidate_market_data_event_state(self) -> None:
        """Forget update timestamps while retaining active subscription identity."""
        for ticker_id, meta in list(self._ticker_update_meta.items()):
            self._ticker_update_meta[ticker_id] = {
                "key": meta.get("key") or self._ticker_keys_by_id.get(ticker_id),
                "subscription_id": str(meta.get("subscription_id") or ""),
                "sequence": 0,
                "received_at": "",
                "received_monotonic": 0.0,
                "ticker_update_time": "",
            }
        self._awaiting_fresh_market_data = True

    def _forget_market_data_subscriptions(self) -> None:
        """Drop cached handles without sending cancellation requests.

        IBKR code 1101 states that market-data subscriptions were lost.  The old
        handles can still contain cached values, so the next read must issue new
        reqMktData requests rather than reuse those objects.
        """
        self._tickers.clear()
        self._ticker_keys_by_id.clear()
        self._ticker_update_meta.clear()
        self._awaiting_fresh_market_data = True

    @staticmethod
    def _event_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except Exception:
            return None

    def _on_ib_error(self, *args: Any, **kwargs: Any) -> None:
        """Track IBKR server-connectivity system messages.

        The callback signature is ``(reqId, errorCode, errorString, contract)``.
        Flexible argument parsing keeps the handler compatible with test doubles
        and minor ib_async callback-shape changes.
        """
        request_id = self._event_int(args[0] if len(args) > 0 else kwargs.get("reqId"))
        error_code = self._event_int(args[1] if len(args) > 1 else kwargs.get("errorCode"))
        message = str(args[2] if len(args) > 2 else kwargs.get("errorString") or "")
        if error_code not in {1100, 1101, 1102, 1300, 2103, 2104, 2110, 10197}:
            return

        if error_code in {1100, 2110}:
            self._invalidate_market_data_event_state()
            self._set_upstream_state(
                connected=False,
                state="upstream_disconnected",
                message=message or "IB Gateway/TWS lost connectivity to IBKR servers.",
                error_code=error_code,
                resubscribe_required=False,
                awaiting_fresh_market_data=True,
            )
            event_type = "IBKR_UPSTREAM_DISCONNECTED"
        elif error_code == 1101:
            self._forget_market_data_subscriptions()
            self._set_upstream_state(
                connected=True,
                state="restored_data_lost",
                message=message or "IBKR server connectivity restored; market-data subscriptions were lost.",
                error_code=error_code,
                resubscribe_required=True,
                awaiting_fresh_market_data=True,
            )
            event_type = "IBKR_UPSTREAM_RESTORED_DATA_LOST"
        elif error_code == 1102:
            self._invalidate_market_data_event_state()
            self._set_upstream_state(
                connected=True,
                state="restored_data_maintained",
                message=message or "IBKR server connectivity restored; market-data subscriptions were maintained.",
                error_code=error_code,
                resubscribe_required=False,
                awaiting_fresh_market_data=True,
            )
            event_type = "IBKR_UPSTREAM_RESTORED_DATA_MAINTAINED"
        elif error_code == 10197:
            self._invalidate_market_data_event_state()
            self._set_upstream_state(
                # A quote-delivery message must never upgrade a previously
                # reported full IBKR server outage. 1100/2110 remains the
                # stronger state until an explicit 1101/1102 restoration.
                connected=self._upstream_connected is not False,
                state="market_data_competing_session",
                message=message or "No market data is available because another IBKR session has priority.",
                error_code=error_code,
                resubscribe_required=False,
                awaiting_fresh_market_data=True,
            )
            event_type = "IBKR_MARKET_DATA_COMPETING_SESSION"
        elif error_code == 2103:
            self._invalidate_market_data_event_state()
            self._set_upstream_state(
                connected=self._upstream_connected is not False,
                state="market_data_farm_disconnected",
                message=message or "The IBKR market-data farm connection is broken.",
                error_code=error_code,
                resubscribe_required=False,
                awaiting_fresh_market_data=True,
            )
            event_type = "IBKR_MARKET_DATA_FARM_DISCONNECTED"
        elif error_code == 2104:
            self._invalidate_market_data_event_state()
            self._set_upstream_state(
                connected=self._upstream_connected is not False,
                state="market_data_farm_restored",
                message=message or "The IBKR market-data farm connection is OK; waiting for a fresh update.",
                error_code=error_code,
                resubscribe_required=False,
                awaiting_fresh_market_data=True,
            )
            event_type = "IBKR_MARKET_DATA_FARM_RESTORED"
        else:
            self._forget_market_data_subscriptions()
            self._set_upstream_state(
                connected=False,
                state="api_port_reset",
                message=message or "The IBKR API socket port was reset.",
                error_code=error_code,
                resubscribe_required=True,
                awaiting_fresh_market_data=True,
            )
            event_type = "IBKR_API_PORT_RESET"
        self._append_connectivity_event(
            event_type,
            error_code=error_code,
            message=self._upstream_message,
            request_id=request_id,
        )

    def _on_api_disconnected(self, *args: Any, **kwargs: Any) -> None:
        self._forget_market_data_subscriptions()
        self._set_upstream_state(
            connected=False,
            state="local_disconnected",
            message="The local API connection to IB Gateway/TWS was disconnected.",
            resubscribe_required=True,
            awaiting_fresh_market_data=True,
        )
        self._append_connectivity_event("IBKR_API_DISCONNECTED", message=self._upstream_message)

    def _on_pending_tickers(self, tickers: Any = None, *args: Any, **kwargs: Any) -> None:
        """Stamp actual streaming updates before cached Ticker fields are read."""
        if tickers is None and args:
            tickers = args[0]
        if tickers is None:
            return
        if isinstance(tickers, (set, list, tuple)):
            values = list(tickers)
        else:
            values = [tickers]
        received_at = utc_now_iso()
        received_monotonic = time.monotonic()
        recorded = False
        for ticker_obj in values:
            ticker_id = id(ticker_obj)
            key = self._ticker_keys_by_id.get(ticker_id)
            if key is None:
                continue
            self._market_data_update_sequence += 1
            previous = self._ticker_update_meta.get(ticker_id) or {}
            self._ticker_update_meta[ticker_id] = {
                "key": key,
                "subscription_id": previous.get("subscription_id") or self._subscription_id(key),
                "sequence": self._market_data_update_sequence,
                "received_at": received_at,
                "received_monotonic": received_monotonic,
                "ticker_update_time": self._ticker_time_text(ticker_obj),
            }
            recorded = True
        if recorded and self._upstream_connected is not False:
            was_waiting = self._awaiting_fresh_market_data
            self._market_data_resubscribe_required = False
            self._awaiting_fresh_market_data = False
            if self._upstream_state != "connected" or was_waiting:
                self._set_upstream_state(
                    connected=True,
                    state="connected",
                    message="IB Gateway/TWS is connected to IBKR servers and fresh market data is arriving.",
                    error_code=None,
                    resubscribe_required=False,
                    awaiting_fresh_market_data=False,
                )

    @staticmethod
    def _first_with_attr(values: tuple[Any, ...], attr: str) -> Any:
        for value in values:
            if hasattr(value, attr):
                return value
        return None

    def _record_broker_event(self, event_type: str, *args: Any, **kwargs: Any) -> None:
        trade = self._first_with_attr(args, "order")
        fill = self._first_with_attr(args, "execution")
        execution = getattr(fill, "execution", None) if fill is not None else self._first_with_attr(args, "execId")
        commission_report = self._first_with_attr(args, "commission")
        order = getattr(trade, "order", None) if trade is not None else self._first_with_attr(args, "orderRef")
        order_status = getattr(trade, "orderStatus", None) if trade is not None else self._first_with_attr(args, "status")
        contract = getattr(trade, "contract", None) if trade is not None else getattr(fill, "contract", None) if fill is not None else self._first_with_attr(args, "symbol")
        order_ref = (
            getattr(order, "orderRef", "")
            or getattr(execution, "orderRef", "")
            or getattr(fill, "orderRef", "")
            or ""
        )
        if order_ref and not str(order_ref).startswith(APP_ORDER_PREFIX + "|"):
            return
        if trade is not None and order_ref:
            self._trades_by_ref[str(order_ref)] = trade
        item = {
            "event_type": event_type,
            "created_at": utc_now_iso(),
            "order_ref": str(order_ref or ""),
            "order_id": getattr(order, "orderId", None) or getattr(execution, "orderId", None),
            "perm_id": getattr(order, "permId", None) or getattr(order_status, "permId", None) or getattr(execution, "permId", None),
            "status": getattr(order_status, "status", None),
            "filled": getattr(order_status, "filled", None),
            "remaining": getattr(order_status, "remaining", None),
            "avg_fill_price": getattr(order_status, "avgFillPrice", None),
            "execution_id": getattr(execution, "execId", None),
            "side": getattr(execution, "side", None) or getattr(order, "action", None),
            "shares": getattr(execution, "shares", None),
            "price": getattr(execution, "price", None),
            "commission": getattr(commission_report, "commission", None),
            "currency": getattr(commission_report, "currency", None) or getattr(contract, "currency", None),
            "ticker": str(getattr(contract, "symbol", "") or "").upper(),
            "raw_args": [repr(arg) for arg in args],
            "raw_kwargs": {str(k): repr(v) for k, v in kwargs.items()},
        }
        self._broker_events.append(item)

    def drain_broker_events(self) -> list[dict[str, Any]]:
        events = list(self._broker_events)
        self._broker_events.clear()
        return events

    def _require_ib_async(self) -> tuple[Any, Any, Any]:
        try:
            from ib_async import IB, Order, Stock  # type: ignore
        except Exception as exc:  # pragma: no cover - depends on optional package
            raise BrokerAdapterError(
                "ib_async is not installed. Run: python -m pip install -r requirements.txt"
            ) from exc
        return IB, Order, Stock

    def connect(self, host: str, port: int, client_id: int, market_data_type: int = 1) -> None:
        IB, _, _ = self._require_ib_async()
        if self.ib is None:
            self.ib = IB()
        self._register_broker_event_handlers()
        was_connected = bool(self.ib.isConnected())
        if not was_connected:
            # A disconnected/reconnected TWS session invalidates previously cached
            # Ticker objects. They can look valid in Python while the socket feed
            # is gone, so the next price read must create fresh reqMktData handles.
            self._reset_market_data_session_state(cancel_existing=False)
            self._set_upstream_state(
                connected=None,
                state="connecting",
                message="Connecting the local API session to IB Gateway/TWS.",
                resubscribe_required=False,
                awaiting_fresh_market_data=True,
            )
            self.ib.connect(host, int(port), clientId=int(client_id), timeout=12)
            # An 1100/2110 callback can be delivered during the synchronous
            # connect handshake.  Do not overwrite that explicit upstream-down
            # state merely because the local socket itself connected.
            if self._upstream_connected is not False:
                self._set_upstream_state(
                    connected=True,
                    state="connected_waiting_for_market_data",
                    message="Local API connection established; waiting for the first fresh market-data update.",
                    resubscribe_required=False,
                    awaiting_fresh_market_data=True,
                )
        self.set_market_data_type(market_data_type)
        if self._upstream_connected is False:
            # A synchronous connect can finish its local socket handshake while
            # IBKR code 1100/2110 has already declared the server link down. Do
            # not issue the initial open-order refresh until upstream recovery.
            return
        try:
            self.refresh_open_trades_cache(force=True)
        except TypeError:
            self.refresh_open_trades_cache()

    def disconnect(self) -> None:
        connected = bool(self.ib is not None and self.ib.isConnected())
        if connected:
            self._reset_market_data_session_state(cancel_existing=True)
            self.ib.disconnect()
        else:
            self._reset_market_data_session_state(cancel_existing=False)
        self._set_upstream_state(
            connected=False,
            state="local_disconnected",
            message="Broker API connection is disconnected.",
            resubscribe_required=False,
            awaiting_fresh_market_data=False,
        )

    def is_connected(self) -> bool:
        return bool(self.ib is not None and self.ib.isConnected())

    def process_events(self, timeout: float = 0.0) -> None:
        if self.is_connected():
            self.ib.sleep(max(0.0, float(timeout)))

    def connectivity_status(self) -> BrokerConnectivityStatus:
        local_connected = self.is_connected()
        if not local_connected:
            return BrokerConnectivityStatus(
                local_connected=False,
                upstream_connected=False,
                state="local_disconnected",
                message="The local API connection to IB Gateway/TWS is disconnected.",
                error_code=self._upstream_error_code,
                changed_at=self._upstream_changed_at,
                market_data_resubscribe_required=self._market_data_resubscribe_required,
                awaiting_fresh_market_data=self._awaiting_fresh_market_data,
                market_data_event_tracking=self._market_data_event_tracking_available,
            )
        return BrokerConnectivityStatus(
            local_connected=True,
            upstream_connected=self._upstream_connected,
            state=self._upstream_state,
            message=self._upstream_message,
            error_code=self._upstream_error_code,
            changed_at=self._upstream_changed_at,
            market_data_resubscribe_required=self._market_data_resubscribe_required,
            awaiting_fresh_market_data=self._awaiting_fresh_market_data,
            market_data_event_tracking=self._market_data_event_tracking_available,
        )

    def _reset_market_data_session_state(self, *, cancel_existing: bool = False) -> None:
        """Forget live market-data handles after a TWS session boundary.

        IBKR Ticker objects are bound to the API session that created them. After
        a socket reconnect the app must not reuse cached subscription handles, or
        the GUI can show Connected while no new quote fields arrive.
        """
        if cancel_existing:
            self._clear_market_data_subscriptions()
        else:
            self._forget_market_data_subscriptions()
        self._active_market_data_type = None
        self._auto_selected_market_data_type = None
        self._last_auto_rescan_monotonic = 0.0

    def _clear_market_data_subscriptions(self) -> None:
        if self.is_connected():
            for ticker_obj in list(self._tickers.values()):
                try:
                    contract = getattr(ticker_obj, "contract", None)
                    if contract is not None:
                        self.ib.cancelMktData(contract)
                except Exception:
                    pass
        self._tickers.clear()
        self._ticker_keys_by_id.clear()
        self._ticker_update_meta.clear()
        self._awaiting_fresh_market_data = True

    def _apply_market_data_type_to_tws(self, market_data_type: int) -> None:
        """Apply a concrete TWS market-data mode. Auto mode uses this internally."""
        mode = int(market_data_type)
        if mode not in {1, 2, 3, 4}:
            mode = 1
        if not self.is_connected():
            self._active_market_data_type = None
            return
        if self._active_market_data_type == mode:
            return
        self._clear_market_data_subscriptions()
        try:
            self.ib.reqMarketDataType(mode)
            self.ib.sleep(0.25)
            self._active_market_data_type = mode
        except Exception:
            # Keep the previous active mode unknown if TWS rejected the request.
            self._active_market_data_type = None

    def set_market_data_type(self, market_data_type: int) -> None:
        new_type = int(market_data_type)
        if new_type not in {0, 1, 2, 3, 4}:
            new_type = 0
        changed = new_type != self._market_data_type
        self._market_data_type = new_type
        if changed:
            self._auto_selected_market_data_type = None
            self._last_auto_rescan_monotonic = 0.0
            if new_type != 0:
                self._clear_market_data_subscriptions()
                self._active_market_data_type = None
        if new_type == 0:
            return
        self._apply_market_data_type_to_tws(new_type)

    def search_stock_contracts(self, query: str, max_results: int = 16) -> list[ContractSearchResult]:
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        pattern = query.strip()
        if not pattern:
            return []
        cache_key = (pattern.upper(), int(max_results))
        if cache_key in self._search_cache:
            return list(self._search_cache[cache_key])
        try:
            descriptions = self.ib.reqMatchingSymbols(pattern) or []
            self.ib.sleep(0.25)
        except Exception as exc:
            raise BrokerAdapterError(f"IBKR contract search failed: {exc}") from exc

        results: list[ContractSearchResult] = []
        seen: set[tuple[Optional[int], str, str, str, str]] = set()
        for desc in list(descriptions or []):
            contract = getattr(desc, "contract", desc)
            symbol = str(getattr(contract, "symbol", "") or "").upper().strip()
            if not symbol:
                continue
            sec_type = str(getattr(contract, "secType", "") or "").upper().strip()
            currency = str(getattr(contract, "currency", "") or "").upper().strip()
            exchange = str(getattr(contract, "exchange", "") or "").upper().strip()
            primary = str(
                getattr(contract, "primaryExchange", "")
                or getattr(contract, "primaryExch", "")
                or ""
            ).upper().strip()
            con_id_raw = getattr(contract, "conId", None)
            try:
                con_id = int(con_id_raw) if con_id_raw not in (None, "") and int(con_id_raw) > 0 else None
            except Exception:
                con_id = None
            key = (con_id, symbol, sec_type, currency, primary or exchange)
            if key in seen:
                continue
            seen.add(key)
            derivatives = getattr(desc, "derivativeSecTypes", None) or getattr(desc, "derivative_sec_types", None) or []
            if isinstance(derivatives, str):
                derivatives = [derivatives]
            description = str(
                getattr(desc, "description", "")
                or getattr(desc, "longName", "")
                or getattr(contract, "description", "")
                or ""
            )
            results.append(
                ContractSearchResult(
                    symbol=symbol,
                    sec_type=sec_type,
                    currency=currency,
                    exchange=exchange,
                    primary_exchange=primary,
                    con_id=con_id,
                    local_symbol=str(getattr(contract, "localSymbol", "") or ""),
                    trading_class=str(getattr(contract, "tradingClass", "") or ""),
                    description=description,
                    derivative_sec_types=[str(x) for x in derivatives],
                )
            )

        pattern_upper = pattern.upper()
        results.sort(key=lambda item: (
            item.symbol != pattern_upper,
            not item.supported,
            item.symbol,
            item.primary_exchange or item.exchange,
            item.currency,
            item.con_id or 0,
        ))
        self._search_cache[cache_key] = results[: int(max_results)]
        return list(self._search_cache[cache_key])

    def qualify_stock(
        self,
        ticker: str,
        exchange: str,
        currency: str,
        primary_exchange: str = "",
        con_id: Optional[int] = None,
    ) -> QualifiedContract:
        _, _, Stock = self._require_ib_async()
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        ticker = ticker.upper().strip()
        exchange = exchange.upper().strip()
        currency = currency.upper().strip()
        primary_exchange = primary_exchange.upper().strip()
        con_id_value = int(con_id) if con_id else 0
        key = f"{ticker}|{exchange}|{currency}|{primary_exchange}|{con_id_value}"
        if key not in self._contracts:
            kwargs: dict[str, str] = {}
            if primary_exchange:
                kwargs["primaryExchange"] = primary_exchange
            try:
                contract = Stock(ticker, exchange, currency, **kwargs)
            except TypeError:
                contract = Stock(ticker, exchange, currency)
                if primary_exchange:
                    try:
                        setattr(contract, "primaryExchange", primary_exchange)
                    except Exception:
                        pass
            if con_id_value > 0:
                try:
                    setattr(contract, "conId", con_id_value)
                except Exception:
                    pass
            qualified = self.ib.qualifyContracts(contract)
            if not qualified:
                suffix_parts = []
                if primary_exchange:
                    suffix_parts.append(f"primaryExchange={primary_exchange}")
                if con_id_value:
                    suffix_parts.append(f"conId={con_id_value}")
                suffix = " " + " ".join(suffix_parts) if suffix_parts else ""
                raise BrokerAdapterError(f"IBKR did not resolve contract for {ticker} {exchange} {currency}{suffix}.")
            self._contracts[key] = qualified[0]
        contract = self._contracts[key]
        con_id_result = getattr(contract, "conId", None)
        if con_id_result is not None:
            self._contracts_by_con_id[int(con_id_result)] = contract
        min_tick = 0.01
        # IBKR validates order prices against the instrument's minimum price
        # increment.  US stocks usually trade in cents above $1, but the API can
        # supply the contract's minimum increment through ContractDetails.minTick.
        # Stop/trailing-stop prices that do not conform can be rejected or become
        # Inactive, so the controller uses this value to normalize order prices.
        try:
            details = list(self.ib.reqContractDetails(contract) or [])
            self.ib.sleep(0.10)
            for detail in details:
                value = float(getattr(detail, "minTick", 0.0) or 0.0)
                if value > 0 and isfinite(value):
                    min_tick = value
                    break
        except Exception:
            min_tick = 0.01
        return QualifiedContract(
            ticker=ticker,
            con_id=con_id_result,
            raw=contract,
            primary_exchange=str(getattr(contract, "primaryExchange", primary_exchange) or ""),
            local_symbol=str(getattr(contract, "localSymbol", "") or ""),
            trading_class=str(getattr(contract, "tradingClass", "") or ""),
            min_tick=float(min_tick),
        )

    @staticmethod
    def _clean_price(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            fvalue = float(value)
        except Exception:
            return None
        if fvalue > 0 and isfinite(fvalue):
            return fvalue
        return None

    def _field_value(self, ticker_obj: Any, attr: str) -> Optional[float]:
        try:
            value = getattr(ticker_obj, attr, None)
            value = value() if callable(value) else value
        except Exception:
            return None
        return self._clean_price(value)

    def _fields_from_ticker(self, ticker_obj: Any) -> dict[str, Optional[float]]:
        fields: dict[str, Optional[float]] = {}
        for attr in (
            "last", "delayedLast", "bid", "ask", "delayedBid", "delayedAsk",
            "close", "delayedClose", "markPrice", "delayedMarkPrice",
        ):
            fields[attr] = self._field_value(ticker_obj, attr)
        if fields.get("bid") is not None and fields.get("ask") is not None and fields["ask"] >= fields["bid"]:
            fields["bidAskMidpoint"] = (fields["bid"] + fields["ask"]) / 2.0
        else:
            fields["bidAskMidpoint"] = None
        fields["midpoint"] = fields["bidAskMidpoint"]
        if fields.get("delayedBid") is not None and fields.get("delayedAsk") is not None and fields["delayedAsk"] >= fields["delayedBid"]:
            fields["delayedBidAskMidpoint"] = (fields["delayedBid"] + fields["delayedAsk"]) / 2.0
        else:
            fields["delayedBidAskMidpoint"] = None
        fields["delayedMidpoint"] = fields["delayedBidAskMidpoint"]
        try:
            market_price = getattr(ticker_obj, "marketPrice")
            value = market_price() if callable(market_price) else market_price
            fields["marketPrice"] = self._clean_price(value)
        except Exception:
            fields["marketPrice"] = None
        return fields

    @staticmethod
    def _choose_price(fields: dict[str, Optional[float]]) -> tuple[Optional[float], str]:
        # Prefer the most responsive top-of-book source supplied by TWS. A raw
        # last price can remain unchanged while bid/ask and TWS marketPrice move,
        # which would otherwise make Stage 1 appear stuck above its anchor. Use
        # quote-derived/current market fields first, then last/close only when
        # those are the usable fields TWS provides.
        priority = [
            "marketPrice",
            "bidAskMidpoint",
            "delayedBidAskMidpoint",
            "markPrice",
            "delayedMarkPrice",
            "last",
            "delayedLast",
            "close",
            "delayedClose",
        ]
        for key in priority:
            value = fields.get(key)
            if value is not None:
                return value, key
        return None, "none"

    def _subscription_key(self, contract: QualifiedContract, generic_tick_list: str) -> tuple[int, str, str, str]:
        raw = contract.raw
        con_id = int(contract.con_id or getattr(raw, "conId", 0) or 0)
        exchange = str(getattr(raw, "exchange", "") or "").upper()
        primary = str(getattr(raw, "primaryExchange", "") or contract.primary_exchange or "").upper()
        return con_id, generic_tick_list or "default", exchange, primary

    def _subscription_id(self, key: tuple[int, str, str, str]) -> str:
        return "|".join(str(part) for part in key)

    def _request_ticker(self, contract: QualifiedContract, generic_tick_list: str) -> Any:
        """Return a cached streaming market-data subscription.

        The request uses reqMktData(snapshot=False, regulatorySnapshot=False).
        That means the app reads the regular subscription feed; it does not make
        fee-incurring regulatory snapshot requests.  Every new handle receives a
        unique subscription id so post-reconnect updates cannot be confused with
        an update sequence from an obsolete cached Ticker.
        """
        key = self._subscription_key(contract, generic_tick_list)
        if key not in self._tickers:
            ticker_obj = self.ib.reqMktData(contract.raw, generic_tick_list, False, False)
            self._tickers[key] = ticker_obj
            self._market_data_subscription_generation += 1
            ticker_id = id(ticker_obj)
            self._ticker_keys_by_id[ticker_id] = key
            self._ticker_update_meta[ticker_id] = {
                "key": key,
                "subscription_id": f"{self._subscription_id(key)}|g{self._market_data_subscription_generation}",
                "sequence": 0,
                "received_at": "",
                "received_monotonic": 0.0,
                "ticker_update_time": "",
            }
            self._market_data_resubscribe_required = False
            self._awaiting_fresh_market_data = True
        ticker_obj = self._tickers[key]
        self._ticker_keys_by_id.setdefault(id(ticker_obj), key)
        return ticker_obj

    def _market_data_type_from_ticker(self, ticker_obj: Any) -> Optional[int]:
        for attr in ("marketDataType", "marketDataType_", "market_data_type"):
            try:
                value = getattr(ticker_obj, attr, None)
                value = value() if callable(value) else value
                if value is not None:
                    return int(value)
            except Exception:
                pass
        return None

    @staticmethod
    def _ticker_time_text(ticker_obj: Any) -> str:
        for attr in ("time", "rtTime", "updateTime", "timestamp"):
            try:
                value = getattr(ticker_obj, attr, None)
                value = value() if callable(value) else value
            except Exception:
                continue
            if value not in (None, ""):
                return str(value)
        return ""

    def _snapshot_from_ticker(
        self,
        ticker_obj: Any,
        request_contract: Optional[QualifiedContract] = None,
        note: str = "",
    ) -> MarketPriceSnapshot:
        fields = self._fields_from_ticker(ticker_obj)
        field_count = sum(1 for value in fields.values() if value is not None)
        price, source = self._choose_price(fields)
        if note and source != "none":
            source = f"{source} via {note}"

        # The production adapter always requires event identity.  If an
        # unsupported ib_async build does not expose pendingTickersEvent, it must
        # fail closed instead of falling back to repeated reads of cached fields.
        tracking = True
        tracking_available = bool(self._market_data_event_tracking_available)
        meta = self._ticker_update_meta.get(id(ticker_obj)) or {}
        sequence = int(meta.get("sequence") or 0)
        update_received_at = str(meta.get("received_at") or "")
        update_age: Optional[float] = None
        event_seen = bool(tracking_available and sequence > 0)
        received_monotonic = float(meta.get("received_monotonic") or 0.0)
        if received_monotonic > 0:
            update_age = max(0.0, time.monotonic() - received_monotonic)

        if self._upstream_connected is False:
            status = "IBKR server connectivity is unavailable; cached prices are not tradeable"
        elif not tracking_available:
            status = "Market-data update event tracking is unavailable; cached prices are not tradeable"
        elif not event_seen:
            status = "Waiting for the first fresh market-data update"
        elif price is not None:
            status = "OK"
        else:
            status = "No usable price from TWS/API"

        raw = request_contract.raw if request_contract is not None else getattr(ticker_obj, "contract", None)
        return MarketPriceSnapshot(
            price=price,
            source=source,
            requested_market_data_type=int(self._market_data_type),
            subscription_market_data_type=self._market_data_type_from_ticker(ticker_obj),
            fields=fields,
            timestamp=utc_now_iso(),
            age_seconds=0.0,
            status=status,
            request_exchange=str(getattr(raw, "exchange", "") or ""),
            request_primary_exchange=str(getattr(raw, "primaryExchange", "") or ""),
            selected_market_data_type=self._active_market_data_type,
            auto_market_data=self._market_data_type == 0,
            api_data_received=event_seen,
            api_data_field_count=field_count,
            ticker_update_time=str(meta.get("ticker_update_time") or self._ticker_time_text(ticker_obj)),
            market_data_update_sequence=sequence,
            market_data_subscription_id=str(meta.get("subscription_id") or ""),
            market_data_update_received_at=update_received_at,
            market_data_update_age_seconds=update_age,
            market_data_event_tracking=tracking,
            market_data_event_tracking_available=tracking_available,
            upstream_connected=self._upstream_connected,
            upstream_state=self._upstream_state,
            upstream_message=self._upstream_message,
            upstream_error_code=self._upstream_error_code,
        )

    @staticmethod
    def _snapshot_has_subscription_data(snapshot: MarketPriceSnapshot) -> bool:
        """Return whether a subscription produced at least one actual update."""
        if snapshot.price is None:
            return False
        if snapshot.market_data_event_tracking:
            return bool(snapshot.api_data_received and (snapshot.market_data_update_sequence or 0) > 0)
        return bool(snapshot.api_data_received)

    def _candidate_primary_exchange(self, contract: QualifiedContract) -> str:
        primary = str(contract.primary_exchange or getattr(contract.raw, "primaryExchange", "") or "").upper()
        if primary:
            return primary
        try:
            for item in self.search_stock_contracts(contract.ticker):
                if item.symbol == contract.ticker and item.supported and item.primary_exchange:
                    return item.primary_exchange
        except Exception:
            pass
        return ""

    def _qualified_market_data_variant(self, contract: QualifiedContract, exchange: str, primary_exchange: str = "") -> Optional[QualifiedContract]:
        _, _, Stock = self._require_ib_async()
        ticker = contract.ticker.upper().strip()
        currency = str(getattr(contract.raw, "currency", "") or "USD").upper()
        key = (ticker, currency, exchange.upper(), primary_exchange.upper())
        if key in self._variant_cache:
            return self._variant_cache[key]
        try:
            kwargs: dict[str, str] = {}
            if primary_exchange:
                kwargs["primaryExchange"] = primary_exchange
            try:
                variant = Stock(ticker, exchange, currency, **kwargs)
            except TypeError:
                variant = Stock(ticker, exchange, currency)
                if primary_exchange:
                    setattr(variant, "primaryExchange", primary_exchange)
            qualified = self.ib.qualifyContracts(variant)
            variant = qualified[0] if qualified else variant
            result = QualifiedContract(
                ticker=ticker,
                con_id=getattr(variant, "conId", contract.con_id),
                raw=variant,
                primary_exchange=str(getattr(variant, "primaryExchange", primary_exchange) or ""),
                local_symbol=str(getattr(variant, "localSymbol", "") or ""),
                trading_class=str(getattr(variant, "tradingClass", "") or ""),
                min_tick=float(getattr(contract, "min_tick", 0.01) or 0.01),
            )
            self._variant_cache[key] = result
            return result
        except Exception:
            return None

    def _try_price_for_contract(self, contract: QualifiedContract, timeout: float, note: str = "") -> MarketPriceSnapshot:
        ticker_obj = self._request_ticker(contract, "")
        deadline = time.monotonic() + max(0.0, float(timeout))
        snapshot = self._snapshot_from_ticker(ticker_obj, contract, note)
        while time.monotonic() < deadline:
            self.ib.sleep(0.25)
            snapshot = self._snapshot_from_ticker(ticker_obj, contract, note)
            if self._snapshot_has_subscription_data(snapshot):
                return snapshot
        return snapshot

    def _try_market_data_variants(self, contract: QualifiedContract, timeout: float) -> Optional[MarketPriceSnapshot]:
        primary = self._candidate_primary_exchange(contract)
        if not primary:
            return None
        variants: list[tuple[str, QualifiedContract]] = []
        raw_exchange = str(getattr(contract.raw, "exchange", "") or "").upper()
        if raw_exchange == "SMART":
            smart_primary = self._qualified_market_data_variant(contract, f"SMART:{primary}")
            if smart_primary is not None:
                variants.append((f"SMART:{primary}", smart_primary))
        direct = self._qualified_market_data_variant(contract, primary)
        if direct is not None:
            variants.append((primary, direct))
        per_variant_timeout = max(0.5, min(2.0, float(timeout)))
        for note, variant in variants:
            try:
                snapshot = self._try_price_for_contract(variant, per_variant_timeout, note)
                if self._snapshot_has_subscription_data(snapshot):
                    return snapshot
            except Exception:
                continue
        return None

    def _price_snapshot_for_active_mode(self, contract: QualifiedContract, timeout: float = 1.0) -> MarketPriceSnapshot:
        snapshot = self._try_price_for_contract(contract, timeout, "")
        if self._snapshot_has_subscription_data(snapshot):
            return snapshot

        # Keep order routing on SMART, but for market data also try the more
        # explicit request forms: SMART:PRIMARY and direct primary exchange.
        variant_snapshot = self._try_market_data_variants(contract, timeout=min(2.0, max(0.5, float(timeout))))
        if variant_snapshot is not None and self._snapshot_has_subscription_data(variant_snapshot):
            return variant_snapshot

        # Generic tick 232 is useful for mark price, but frozen modes do not
        # support generic ticks. In auto mode, this check uses the concrete
        # active TWS mode, not the logical request 0.
        if (self._active_market_data_type or self._market_data_type) not in {2, 4}:
            try:
                generic_ticker = self._request_ticker(contract, self._GENERIC_TICK_LIST)
                generic_deadline = time.monotonic() + min(1.5, max(0.0, float(timeout)))
                generic_snapshot = self._snapshot_from_ticker(generic_ticker, contract, "generic 232")
                while time.monotonic() < generic_deadline:
                    self.ib.sleep(0.25)
                    generic_snapshot = self._snapshot_from_ticker(generic_ticker, contract, "generic 232")
                    generic_snapshot.generic_ticks = self._GENERIC_TICK_LIST
                    if self._snapshot_has_subscription_data(generic_snapshot):
                        return generic_snapshot
                generic_snapshot.generic_ticks = self._GENERIC_TICK_LIST
                if self._snapshot_has_subscription_data(generic_snapshot):
                    return generic_snapshot
            except Exception as exc:
                snapshot.error = str(exc)
        return snapshot

    def _annotate_auto_snapshot(
        self,
        snapshot: MarketPriceSnapshot,
        *,
        selected_mode: Optional[int],
        attempts: list[dict[str, Any]],
    ) -> MarketPriceSnapshot:
        snapshot.requested_market_data_type = 0
        snapshot.selected_market_data_type = selected_mode
        snapshot.auto_market_data = True
        snapshot.auto_attempts = attempts
        if self._snapshot_has_subscription_data(snapshot) and selected_mode is not None:
            snapshot.status = f"OK - auto selected market-data mode {selected_mode}"
        elif not snapshot.error:
            snapshot.status = "No usable price in auto mode"
        return snapshot

    def _auto_price_snapshot(self, contract: QualifiedContract, timeout: float = 1.0) -> MarketPriceSnapshot:
        """Try live/delayed/frozen modes and remember the first usable one.

        Auto mode avoids repeatedly cycling every IBKR market-data mode on every
        worker tick. A periodic rescan allows the app to notice when a better
        mode becomes available after login/subscription changes.
        """
        now = time.monotonic()
        modes: list[int]
        cached = self._auto_selected_market_data_type
        if cached in self._AUTO_MODE_SEQUENCE and now - self._last_auto_rescan_monotonic < self._AUTO_RESCAN_SECONDS:
            modes = [int(cached)]
        else:
            modes = list(self._AUTO_MODE_SEQUENCE)
        # If the cached mode fails, fall through to every remaining mode.
        for mode in self._AUTO_MODE_SEQUENCE:
            if mode not in modes:
                modes.append(mode)

        attempts: list[dict[str, Any]] = []
        first_snapshot: Optional[MarketPriceSnapshot] = None
        per_mode_timeout = max(0.35, min(1.25, float(timeout)))
        for mode in modes:
            self._apply_market_data_type_to_tws(mode)
            snapshot = self._price_snapshot_for_active_mode(contract, timeout=per_mode_timeout)
            attempts.append({
                "mode": mode,
                "price": snapshot.price,
                "source": snapshot.source,
                "status": snapshot.status,
                "request_exchange": snapshot.request_exchange,
                "request_primary_exchange": snapshot.request_primary_exchange,
            })
            if first_snapshot is None:
                first_snapshot = snapshot
            if self._snapshot_has_subscription_data(snapshot):
                self._auto_selected_market_data_type = mode
                self._last_auto_rescan_monotonic = now
                return self._annotate_auto_snapshot(snapshot, selected_mode=mode, attempts=attempts)

        if first_snapshot is None:
            first_snapshot = MarketPriceSnapshot(
                price=None,
                source="none",
                requested_market_data_type=0,
                subscription_market_data_type=None,
                fields={},
                timestamp=utc_now_iso(),
                status="No usable price in auto mode",
            )
        self._auto_selected_market_data_type = None
        self._last_auto_rescan_monotonic = now
        return self._annotate_auto_snapshot(first_snapshot, selected_mode=None, attempts=attempts)

    def price_snapshot(self, contract: QualifiedContract, timeout: float = 1.0) -> MarketPriceSnapshot:
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        if self._upstream_connected is False:
            return MarketPriceSnapshot(
                price=None,
                source="none",
                requested_market_data_type=int(self._market_data_type),
                subscription_market_data_type=None,
                fields={},
                timestamp=utc_now_iso(),
                status="IBKR server connectivity is unavailable; market-data requests are paused",
                error=self._upstream_message,
                selected_market_data_type=self._active_market_data_type,
                auto_market_data=self._market_data_type == 0,
                market_data_update_sequence=0,
                market_data_event_tracking=True,
                market_data_event_tracking_available=self._market_data_event_tracking_available,
                upstream_connected=False,
                upstream_state=self._upstream_state,
                upstream_message=self._upstream_message,
                upstream_error_code=self._upstream_error_code,
            )
        if int(self._market_data_type) == 0:
            return self._auto_price_snapshot(contract, timeout=timeout)
        self._apply_market_data_type_to_tws(self._market_data_type)
        return self._price_snapshot_for_active_mode(contract, timeout=timeout)

    def last_price(self, contract: QualifiedContract, timeout: float = 1.0) -> Optional[float]:
        return self.price_snapshot(contract, timeout=timeout).price

    @staticmethod
    def _fallback_us_equity_rth(now_utc: Optional[datetime] = None) -> RthStatus:
        now_utc = now_utc or datetime.now(timezone.utc)
        try:
            eastern = ZoneInfo("America/New_York")
            local = now_utc.astimezone(eastern)
            open_time = datetime_time(9, 30)
            close_time = datetime_time(16, 0)
            is_trading_day = local.weekday() < 5
            is_open = is_trading_day and open_time <= local.time() < close_time
            session_open = local.replace(hour=9, minute=30, second=0, microsecond=0)
            session_close = local.replace(hour=16, minute=0, second=0, microsecond=0)
            detail = local.strftime("%Y-%m-%d %H:%M:%S %Z")
            return RthStatus(
                is_open=is_open,
                source="fallback_us_equity",
                message=("US equity RTH fallback open" if is_open else "US equity RTH fallback closed") + f" at {detail}",
                checked_at=now_utc.isoformat(),
                time_zone="America/New_York",
                session_open=session_open.isoformat() if is_trading_day else "",
                session_close=session_close.isoformat() if is_trading_day else "",
                session_date=local.strftime("%Y%m%d"),
            )
        except Exception:
            return RthStatus(False, "fallback_failed", "Could not determine regular trading hours; failing closed.", now_utc.isoformat())

    @staticmethod
    def _parse_liquid_hours_window(liquid_hours: str, time_zone: str, now_utc: Optional[datetime] = None) -> Optional[RthStatus]:
        if not liquid_hours:
            return None
        now_utc = now_utc or datetime.now(timezone.utc)
        try:
            tz = ZoneInfo(time_zone or "America/New_York")
        except Exception:
            tz = ZoneInfo("America/New_York")
            time_zone = "America/New_York"
        local = now_utc.astimezone(tz)
        today = local.strftime("%Y%m%d")
        matched_day = False
        for raw_segment in str(liquid_hours).split(";"):
            segment = raw_segment.strip()
            if not segment or ":" not in segment:
                continue
            day, ranges = segment.split(":", 1)
            if day != today:
                continue
            matched_day = True
            if ranges.upper() == "CLOSED":
                return RthStatus(
                    False,
                    "contract_liquid_hours",
                    f"Contract liquidHours says CLOSED for {today}.",
                    now_utc.isoformat(),
                    liquid_hours,
                    time_zone,
                    session_date=today,
                )
            windows: list[tuple[datetime, datetime, str, str]] = []
            for raw_span in ranges.split(","):
                span = raw_span.strip()
                if "-" not in span:
                    continue
                start_text, end_text = span.split("-", 1)

                def parse_endpoint(text: str) -> datetime:
                    text = text.strip()
                    if len(text) == 4:
                        date_part = today
                        time_part = text
                    elif len(text) >= 13 and ":" in text:
                        date_part, time_part = text.split(":", 1)
                    elif len(text) >= 12:
                        date_part, time_part = text[:8], text[8:12]
                    else:
                        raise ValueError(f"Unsupported liquidHours endpoint: {text}")
                    return datetime.strptime(date_part + time_part[:4], "%Y%m%d%H%M").replace(tzinfo=tz)

                try:
                    start = parse_endpoint(start_text)
                    end = parse_endpoint(end_text)
                except Exception:
                    continue
                if end <= start and len(end_text.strip()) == 4:
                    end += timedelta(days=1)
                if end <= start:
                    continue
                windows.append((start, end, start_text.strip(), end_text.strip()))
            if not windows:
                return RthStatus(
                    False,
                    "contract_liquid_hours",
                    "No valid liquidHours window matched; treating as closed.",
                    now_utc.isoformat(),
                    liquid_hours,
                    time_zone,
                    session_date=today,
                )
            session_open = min(window[0] for window in windows)
            session_close = max(window[1] for window in windows)
            active = next((window for window in windows if window[0] <= local < window[1]), None)
            if active is not None:
                return RthStatus(
                    True,
                    "contract_liquid_hours",
                    f"RTH open in contract liquidHours window {active[2]}-{active[3]} {time_zone}.",
                    now_utc.isoformat(),
                    liquid_hours,
                    time_zone,
                    session_open=session_open.isoformat(),
                    session_close=session_close.isoformat(),
                    session_date=today,
                )
            return RthStatus(
                False,
                "contract_liquid_hours",
                f"Outside contract liquidHours for {today} {time_zone}.",
                now_utc.isoformat(),
                liquid_hours,
                time_zone,
                session_open=session_open.isoformat(),
                session_close=session_close.isoformat(),
                session_date=today,
            )
        if matched_day:
            return RthStatus(
                False,
                "contract_liquid_hours",
                "No valid liquidHours window matched; treating as closed.",
                now_utc.isoformat(),
                liquid_hours,
                time_zone,
                session_date=today,
            )
        return None

    def regular_trading_hours_status(self, contract: QualifiedContract) -> RthStatus:
        if not self.is_connected():
            return RthStatus(False, "not_connected", "Not connected to TWS; trading is blocked.", datetime.now(timezone.utc).isoformat())
        con_id = int(contract.con_id or getattr(contract.raw, "conId", 0) or 0)
        cache_key = con_id or hash((contract.ticker, getattr(contract.raw, "exchange", "")))
        cached = self._rth_cache.get(cache_key)
        now_mono = time.monotonic()
        if cached and now_mono - cached[0] < 30.0:
            return cached[1]
        try:
            details = list(self.ib.reqContractDetails(contract.raw) or [])
            self.ib.sleep(0.25)
        except Exception as exc:
            status = self._fallback_us_equity_rth()
            status.source = "fallback_after_contract_details_error"
            status.message = f"Could not request contract liquidHours ({exc}); {status.message}"
            self._rth_cache[cache_key] = (now_mono, status)
            return status
        for detail in details:
            liquid = str(getattr(detail, "liquidHours", "") or "")
            tz_name = str(getattr(detail, "timeZoneId", "") or "America/New_York")
            parsed = self._parse_liquid_hours_window(liquid, tz_name)
            if parsed is not None:
                self._rth_cache[cache_key] = (now_mono, parsed)
                return parsed
        status = self._fallback_us_equity_rth()
        status.source = "fallback_no_contract_liquid_hours"
        self._rth_cache[cache_key] = (now_mono, status)
        return status

    def what_if_trailing_stop(
        self,
        *,
        contract: QualifiedContract,
        action: str,
        quantity: int,
        trailing_percent: float,
        initial_stop_price: float,
        order_ref: str,
        tif: str = "GTC",
        account: str = "",
        outside_rth: bool = False,
    ) -> dict[str, Any]:
        """Ask IBKR for margin/order impact without transmitting the BUY.

        ib_async exposes the IBKR what-if order state through placeOrder on an
        order with whatIf=True and transmit=False. The method returns a compact
        dict so the controller and tests do not depend on ib_async classes.
        """
        _, Order, _ = self._require_ib_async()
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        side = action.upper().strip()
        try:
            qty = int(quantity)
            trail = float(trailing_percent)
            stop = float(initial_stop_price)
        except Exception as exc:
            raise BrokerAdapterError("What-if order quantity/trail/stop must be numeric.") from exc
        order = Order(
            action=side,
            orderType="TRAIL",
            totalQuantity=qty,
            trailingPercent=trail,
            trailStopPrice=stop,
            tif=(tif or "GTC").upper().strip(),
            orderRef=order_ref,
            transmit=False,
            whatIf=True,
        )
        try:
            order.outsideRth = bool(outside_rth)
            order.triggerMethod = 2
        except Exception:
            pass
        if account:
            try:
                order.account = account
            except Exception:
                pass
        try:
            trade = self.ib.placeOrder(contract.raw, order)
            self.ib.sleep(1.0)
        except Exception as exc:
            raise BrokerAdapterError(f"IBKR what-if order failed: {exc}") from exc
        order_state = getattr(trade, "orderState", None)
        warning = str(getattr(order_state, "warningText", "") or "") if order_state is not None else ""
        init_margin = str(getattr(order_state, "initMarginChange", "") or "") if order_state is not None else ""
        maint_margin = str(getattr(order_state, "maintMarginChange", "") or "") if order_state is not None else ""
        equity_change = str(getattr(order_state, "equityWithLoanChange", "") or "") if order_state is not None else ""
        status = str(getattr(order_state, "status", "") or getattr(getattr(trade, "orderStatus", None), "status", "") or "")
        ok = not any(token in warning.lower() for token in ("reject", "insufficient", "invalid", "error"))
        return {
            "ok": ok,
            "message": warning or status or "What-if margin check returned no warning.",
            "initMarginChange": init_margin,
            "maintMarginChange": maint_margin,
            "equityWithLoanChange": equity_change,
            "status": status,
            "orderType": "TRAIL",
        }

    def what_if_market_order(
        self,
        *,
        contract: QualifiedContract,
        action: str,
        quantity: int,
        order_ref: str,
        tif: str = "GTC",
        account: str = "",
        outside_rth: bool = False,
    ) -> dict[str, Any]:
        """Ask IBKR for margin/order impact of a market order without transmitting it."""
        _, Order, _ = self._require_ib_async()
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        side = action.upper().strip()
        try:
            qty = int(quantity)
        except Exception as exc:
            raise BrokerAdapterError("What-if market order quantity must be numeric.") from exc
        order = Order(
            action=side,
            orderType="MKT",
            totalQuantity=qty,
            tif=(tif or "GTC").upper().strip(),
            orderRef=order_ref,
            transmit=False,
            whatIf=True,
        )
        try:
            order.outsideRth = bool(outside_rth)
        except Exception:
            pass
        if account:
            try:
                order.account = account
            except Exception:
                pass
        try:
            trade = self.ib.placeOrder(contract.raw, order)
            self.ib.sleep(1.0)
        except Exception as exc:
            raise BrokerAdapterError(f"IBKR what-if market order failed: {exc}") from exc
        order_state = getattr(trade, "orderState", None)
        warning = str(getattr(order_state, "warningText", "") or "") if order_state is not None else ""
        init_margin = str(getattr(order_state, "initMarginChange", "") or "") if order_state is not None else ""
        maint_margin = str(getattr(order_state, "maintMarginChange", "") or "") if order_state is not None else ""
        equity_change = str(getattr(order_state, "equityWithLoanChange", "") or "") if order_state is not None else ""
        status = str(getattr(order_state, "status", "") or getattr(getattr(trade, "orderStatus", None), "status", "") or "")
        ok = not any(token in warning.lower() for token in ("reject", "insufficient", "invalid", "error"))
        return {
            "ok": ok,
            "message": warning or status or "What-if margin check returned no warning.",
            "initMarginChange": init_margin,
            "maintMarginChange": maint_margin,
            "equityWithLoanChange": equity_change,
            "status": status,
            "orderType": "MKT",
        }

    def place_trailing_stop(
        self,
        *,
        contract: QualifiedContract,
        action: str,
        quantity: int,
        trailing_percent: float,
        initial_stop_price: float,
        order_ref: str,
        tif: str = "GTC",
        account: str = "",
        outside_rth: bool = False,
    ) -> OrderHandle:
        _, Order, _ = self._require_ib_async()
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        side = action.upper().strip()
        if side not in {"BUY", "SELL"}:
            raise BrokerAdapterError(f"Unsupported trailing-stop side: {action}")
        try:
            qty = int(quantity)
        except Exception as exc:
            raise BrokerAdapterError("Order quantity must be an integer.") from exc
        if qty <= 0:
            raise BrokerAdapterError("Order quantity must be greater than zero.")
        try:
            trail = float(trailing_percent)
            stop = float(initial_stop_price)
        except Exception as exc:
            raise BrokerAdapterError("Trailing percent and initial stop price must be numeric.") from exc
        if not (0.0 < trail < 100.0):
            raise BrokerAdapterError("Trailing percent must be greater than 0 and less than 100.")
        if not (stop > 0.0 and isfinite(stop)):
            raise BrokerAdapterError("Initial stop price must be a finite positive value.")
        tif_value = (tif or "GTC").upper().strip()
        if tif_value not in {"DAY", "GTC"}:
            raise BrokerAdapterError("Only DAY and GTC time-in-force values are supported by this app.")
        order = Order(
            action=side,
            orderType="TRAIL",
            totalQuantity=qty,
            trailingPercent=trail,
            trailStopPrice=stop,
            tif=tif_value,
            orderRef=order_ref,
            transmit=True,
        )
        # Use IBKR triggerMethod=2 (Last) explicitly for the native trailing
        # order so broker diagnostics do not imply that the GUI-selected price
        # source controls IBKR's trigger. IBKR may ignore this field when the
        # order is handled natively by an exchange.
        try:
            order.triggerMethod = 2
        except Exception:
            pass
        try:
            order.outsideRth = bool(outside_rth)
        except Exception:
            pass
        if account:
            try:
                order.account = account
            except Exception:
                pass
        trade = self.ib.placeOrder(contract.raw, order)
        self.ib.sleep(0.75)
        self._trades_by_ref[order_ref] = trade
        status = getattr(getattr(trade, "orderStatus", None), "status", "Submitted") or "Submitted"
        order_id = getattr(getattr(trade, "order", None), "orderId", None)
        perm_id = getattr(getattr(trade, "order", None), "permId", None) or getattr(getattr(trade, "orderStatus", None), "permId", None)
        return OrderHandle(
            order_ref=order_ref,
            order_id=int(order_id) if order_id is not None else None,
            perm_id=int(perm_id) if perm_id else None,
            status=str(status),
            raw={
                "action": side,
                "orderType": "TRAIL",
                "quantity": qty,
                "trailingPercent": trail,
                "trailStopPrice": stop,
                "triggerMethod": 2,
                "outsideRth": bool(outside_rth),
            },
        )

    def place_market_order(
        self,
        *,
        contract: QualifiedContract,
        action: str,
        quantity: int,
        order_ref: str,
        tif: str = "GTC",
        account: str = "",
        outside_rth: bool = False,
    ) -> OrderHandle:
        _, Order, _ = self._require_ib_async()
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        side = action.upper().strip()
        if side not in {"BUY", "SELL"}:
            raise BrokerAdapterError(f"Unsupported market-order side: {action}")
        try:
            qty = int(quantity)
        except Exception as exc:
            raise BrokerAdapterError("Order quantity must be an integer.") from exc
        if qty <= 0:
            raise BrokerAdapterError("Order quantity must be greater than zero.")
        tif_value = (tif or "GTC").upper().strip()
        if tif_value not in {"DAY", "GTC"}:
            raise BrokerAdapterError("Only DAY and GTC time-in-force values are supported by this app.")
        order = Order(
            action=side,
            orderType="MKT",
            totalQuantity=qty,
            tif=tif_value,
            orderRef=order_ref,
            transmit=True,
        )
        try:
            order.outsideRth = bool(outside_rth)
        except Exception:
            pass
        if account:
            try:
                order.account = account
            except Exception:
                pass
        trade = self.ib.placeOrder(contract.raw, order)
        self.ib.sleep(0.75)
        self._trades_by_ref[order_ref] = trade
        status = getattr(getattr(trade, "orderStatus", None), "status", "Submitted") or "Submitted"
        order_id = getattr(getattr(trade, "order", None), "orderId", None)
        perm_id = getattr(getattr(trade, "order", None), "permId", None) or getattr(getattr(trade, "orderStatus", None), "permId", None)
        return OrderHandle(
            order_ref=order_ref,
            order_id=int(order_id) if order_id is not None else None,
            perm_id=int(perm_id) if perm_id else None,
            status=str(status),
            raw={
                "action": side,
                "orderType": "MKT",
                "quantity": qty,
                "outsideRth": bool(outside_rth),
            },
        )

    def cancel_order(self, order_ref: str, order_id: Optional[int] = None) -> None:
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        trade = self._trades_by_ref.get(order_ref)
        if trade is None:
            self.refresh_open_trades_cache(force=True)
            trade = self._trades_by_ref.get(order_ref)
        if trade is not None:
            self.ib.cancelOrder(trade.order)
            self.ib.sleep(0.25)
            return
        if order_id is not None:
            self.refresh_open_trades_cache(force=True)
            for cached_trade in self._trades_by_ref.values():
                if getattr(cached_trade.order, "orderId", None) == order_id:
                    self.ib.cancelOrder(cached_trade.order)
                    self.ib.sleep(0.25)
                    return
        raise BrokerAdapterError(f"Could not find open order to cancel: {order_ref}")

    def refresh_open_trades_cache(self, *, force: bool = False) -> None:
        if not self.is_connected():
            return
        now = time.monotonic()
        if force or now - self._last_open_trades_refresh_monotonic >= self._open_trades_refresh_min_seconds:
            try:
                self.ib.reqOpenOrders()
                self.ib.sleep(0.5)
                self._last_open_trades_refresh_monotonic = now
            except Exception:
                pass
        try:
            open_trades = list(self.ib.openTrades() or [])
        except Exception:
            open_trades = []
        for trade in open_trades:
            ref = getattr(getattr(trade, "order", None), "orderRef", "") or ""
            if ref.startswith(APP_ORDER_PREFIX + "|"):
                self._trades_by_ref[ref] = trade

    def _to_polled_order_state(self, trade: Any) -> Optional[PolledOrderState]:
        order = getattr(trade, "order", None)
        order_status = getattr(trade, "orderStatus", None)
        if order is None or order_status is None:
            return None
        ref = getattr(order, "orderRef", "") or ""
        if not ref.startswith(APP_ORDER_PREFIX + "|"):
            return None
        filled = getattr(order_status, "filled", 0) or 0
        remaining = getattr(order_status, "remaining", 0) or 0
        avg_fill_price = getattr(order_status, "avgFillPrice", 0.0) or 0.0
        order_id = getattr(order, "orderId", None)
        perm_id = getattr(order, "permId", None) or getattr(order_status, "permId", None)
        executions: list[dict[str, Any]] = []
        total_commission = 0.0
        execution_value = 0.0
        execution_shares = 0.0
        try:
            for fill in list(getattr(trade, "fills", []) or []):
                execution = getattr(fill, "execution", None)
                commission_report = getattr(fill, "commissionReport", None)
                commission = float(getattr(commission_report, "commission", 0.0) or 0.0)
                shares = float(getattr(execution, "shares", 0.0) or 0.0)
                price = float(getattr(execution, "price", 0.0) or 0.0)
                if shares > 0 and price > 0:
                    execution_value += shares * price
                    execution_shares += shares
                total_commission += commission
                executions.append({
                    "execId": getattr(execution, "execId", None),
                    "shares": shares or getattr(execution, "shares", None),
                    "price": price or getattr(execution, "price", None),
                    "avgPrice": getattr(execution, "avgPrice", None),
                    "side": getattr(execution, "side", None),
                    "time": getattr(execution, "time", None),
                    "commission": commission,
                    "currency": getattr(commission_report, "currency", None),
                })
        except Exception:
            executions = []
            total_commission = 0.0
            execution_value = 0.0
            execution_shares = 0.0
        if (not avg_fill_price or float(avg_fill_price) <= 0) and execution_shares > 0:
            avg_fill_price = execution_value / execution_shares
        return PolledOrderState(
            order_ref=ref,
            order_id=int(order_id) if order_id is not None else None,
            perm_id=int(perm_id) if perm_id else None,
            status=str(getattr(order_status, "status", "")),
            filled=int(float(filled)),
            remaining=int(float(remaining)),
            avg_fill_price=float(avg_fill_price),
            commission=total_commission,
            executions=executions,
            raw={
                "action": getattr(order, "action", ""),
                "orderType": getattr(order, "orderType", ""),
                "totalQuantity": getattr(order, "totalQuantity", None),
                "executions": executions,
            },
        )


    @staticmethod
    def _execution_side_matches(value: Any, action: str) -> bool:
        expected = str(action or "").upper().strip()
        if not expected:
            return True
        side = str(value or "").upper().strip()
        if expected == "BUY":
            return side in {"", "BUY", "BOT"}
        if expected == "SELL":
            return side in {"", "SELL", "SLD"}
        return True

    @staticmethod
    def _as_optional_int(value: Any) -> Optional[int]:
        try:
            if value in (None, ""):
                return None
            ivalue = int(value)
            return ivalue if ivalue > 0 else None
        except Exception:
            return None

    def _fill_matches_order(
        self,
        fill: Any,
        *,
        order_ref: str,
        order_id: Optional[int],
        perm_id: Optional[int],
        ticker: str,
        action: str,
    ) -> bool:
        execution = getattr(fill, "execution", None)
        contract = getattr(fill, "contract", None)
        order = getattr(fill, "order", None)
        if execution is None:
            return False
        if ticker:
            symbol = str(getattr(contract, "symbol", "") or "").upper().strip()
            if symbol and symbol != ticker.upper().strip():
                return False
        side = getattr(execution, "side", "")
        if not self._execution_side_matches(side, action):
            return False
        ref_candidates = [
            getattr(execution, "orderRef", ""),
            getattr(fill, "orderRef", ""),
            getattr(order, "orderRef", ""),
        ]
        if order_ref and any(str(ref or "") == order_ref for ref in ref_candidates):
            return True
        exec_order_id = self._as_optional_int(getattr(execution, "orderId", None))
        exec_perm_id = self._as_optional_int(getattr(execution, "permId", None))
        if perm_id and exec_perm_id == int(perm_id):
            return True
        return bool(order_id and exec_order_id == int(order_id))

    def _polled_state_from_fills(
        self,
        fills: list[Any],
        *,
        order_ref: str,
        order_id: Optional[int],
        perm_id: Optional[int],
        action: str,
    ) -> Optional[PolledOrderState]:
        total_shares = 0.0
        total_value = 0.0
        total_commission = 0.0
        executions: list[dict[str, Any]] = []
        for fill in fills:
            execution = getattr(fill, "execution", None)
            commission_report = getattr(fill, "commissionReport", None)
            if execution is None:
                continue
            shares = float(getattr(execution, "shares", 0.0) or 0.0)
            price = float(getattr(execution, "price", 0.0) or 0.0)
            if shares <= 0 or price <= 0:
                continue
            commission = float(getattr(commission_report, "commission", 0.0) or 0.0)
            total_shares += shares
            total_value += shares * price
            total_commission += commission
            executions.append({
                "execId": getattr(execution, "execId", None),
                "shares": shares,
                "price": price,
                "avgPrice": getattr(execution, "avgPrice", None),
                "side": getattr(execution, "side", action),
                "time": getattr(execution, "time", None),
                "commission": commission,
                "currency": getattr(commission_report, "currency", None),
            })
        if total_shares <= 0:
            return None
        avg_fill_price = total_value / total_shares
        return PolledOrderState(
            order_ref=order_ref,
            order_id=order_id,
            perm_id=perm_id,
            status="Filled",
            filled=int(total_shares),
            remaining=0,
            avg_fill_price=float(avg_fill_price),
            commission=float(total_commission),
            executions=executions,
            raw={
                "recoveredFromExecutions": True,
                "action": action.upper(),
                "executions": executions,
            },
        )

    def recover_order_fill(
        self,
        *,
        order_ref: str,
        order_id: Optional[int] = None,
        perm_id: Optional[int] = None,
        ticker: str = "",
        account: str = "",
        action: str = "",
    ) -> Optional[PolledOrderState]:
        """Best-effort recovery for an app-owned order that is no longer open.

        Open-order requests cannot return filled/cancelled orders. This method
        first checks the local TWS trade cache for the current API session, then
        requests recent executions from TWS and matches by OrderRef, permId, or
        orderId.
        """
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        polled = self.poll_order(order_ref) if order_ref else None
        if polled and polled.filled > 0:
            return polled

        # reqExecutions is the useful recovery path after an app restart, because
        # filled orders are no longer open and may not exist in ib.trades().
        try:
            from ib_async import ExecutionFilter  # type: ignore
            filt = ExecutionFilter()
            if account:
                try:
                    filt.acctCode = account
                except Exception:
                    pass
            if ticker:
                try:
                    filt.symbol = ticker.upper().strip()
                    filt.secType = "STK"
                except Exception:
                    pass
            fills = list(self.ib.reqExecutions(filt) or [])
            self.ib.sleep(0.75)
        except Exception:
            try:
                fills = list(self.ib.fills() or [])
            except Exception:
                fills = []

        matched = [
            fill
            for fill in fills
            if self._fill_matches_order(
                fill,
                order_ref=order_ref,
                order_id=order_id,
                perm_id=perm_id,
                ticker=ticker,
                action=action,
            )
        ]
        return self._polled_state_from_fills(
            matched,
            order_ref=order_ref,
            order_id=order_id,
            perm_id=perm_id,
            action=action,
        )


    def poll_order(self, order_ref: str) -> Optional[PolledOrderState]:
        if not self.is_connected():
            raise BrokerAdapterError("Not connected to TWS.")
        trade = self._trades_by_ref.get(order_ref)
        if trade is not None:
            self.ib.sleep(0.1)
            return self._to_polled_order_state(trade)
        try:
            trades = list(self.ib.trades() or [])
        except Exception:
            trades = []
        for trade in trades:
            ref = getattr(getattr(trade, "order", None), "orderRef", "") or ""
            if ref == order_ref:
                self._trades_by_ref[ref] = trade
                return self._to_polled_order_state(trade)
        self.refresh_open_trades_cache(force=True)
        trade = self._trades_by_ref.get(order_ref)
        if trade is not None:
            self.ib.sleep(0.1)
            return self._to_polled_order_state(trade)
        return None

    def open_app_orders(self) -> list[PolledOrderState]:
        if not self.is_connected():
            return []
        self.refresh_open_trades_cache(force=True)
        result: list[PolledOrderState] = []
        try:
            trades = list(self.ib.openTrades() or [])
        except Exception:
            trades = []
        for trade in trades:
            state = self._to_polled_order_state(trade)
            if state is not None:
                result.append(state)
        return result

    def _execution_dict_from_fill(self, fill: Any) -> Optional[dict[str, Any]]:
        execution = getattr(fill, "execution", None)
        if execution is None:
            return None
        contract = getattr(fill, "contract", None)
        order = getattr(fill, "order", None)
        commission_report = getattr(fill, "commissionReport", None)
        order_ref = (
            getattr(execution, "orderRef", "")
            or getattr(fill, "orderRef", "")
            or getattr(order, "orderRef", "")
            or ""
        )
        try:
            shares = float(getattr(execution, "shares", 0.0) or 0.0)
            price = float(getattr(execution, "price", 0.0) or 0.0)
        except Exception:
            return None
        if shares <= 0 or price <= 0:
            return None
        return {
            "ticker": str(getattr(contract, "symbol", "") or "").upper(),
            "con_id": getattr(contract, "conId", None),
            "sec_type": str(getattr(contract, "secType", "") or ""),
            "currency": str(getattr(commission_report, "currency", None) or getattr(contract, "currency", "") or "USD"),
            "side": str(getattr(execution, "side", "") or "").upper(),
            "shares": shares,
            "price": price,
            "avg_price": getattr(execution, "avgPrice", None),
            "commission": float(getattr(commission_report, "commission", 0.0) or 0.0),
            "order_ref": str(order_ref),
            "orderRef": str(order_ref),
            "order_id": getattr(execution, "orderId", None),
            "perm_id": getattr(execution, "permId", None),
            "execution_id": getattr(execution, "execId", None),
            "time": str(getattr(execution, "time", "") or ""),
            "account": str(getattr(execution, "acctNumber", "") or ""),
            "exchange": str(getattr(execution, "exchange", "") or ""),
            "raw": {
                "execution": repr(execution),
                "commissionReport": repr(commission_report),
            },
        }

    def managed_accounts(self) -> list[str]:
        """Return accounts reported by TWS/IB Gateway for operator display.

        The controller never uses this helper to choose an order account.
        Leaving ConnectionSettings.account blank still lets IBKR route orders
        to the default account exactly as before; the returned IDs are only
        shown in the top status bar.
        """
        if not self.is_connected():
            return []
        values: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if not text:
                return
            for part in text.replace(";", ",").split(","):
                item = part.strip()
                if item:
                    values.append(item)

        try:
            method = getattr(self.ib, "managedAccounts", None)
            if callable(method):
                result = method() or []
                if isinstance(result, str):
                    add(result)
                else:
                    for item in result:
                        add(item)
        except Exception:
            pass
        if not values:
            try:
                wrapper = getattr(self.ib, "wrapper", None)
                for item in (getattr(wrapper, "accounts", []) or []):
                    add(item)
            except Exception:
                pass
        if not values:
            try:
                for pos in list(self.ib.positions() or []):
                    add(getattr(pos, "account", ""))
            except Exception:
                pass
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def recent_executions(self) -> list[dict[str, Any]]:
        if not self.is_connected():
            return []
        fills: list[Any] = []
        try:
            fills.extend(list(self.ib.fills() or []))
        except Exception:
            pass
        try:
            requested = self.ib.reqExecutions() or []
            self.ib.sleep(0.75)
            fills.extend(list(requested or []))
        except Exception:
            pass

        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for fill in fills:
            item = self._execution_dict_from_fill(fill)
            if not item:
                continue
            key = str(item.get("execution_id") or f"{item.get('perm_id')}|{item.get('order_id')}|{item.get('side')}|{item.get('shares')}|{item.get('price')}")
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def position_size(self, contract: QualifiedContract, account: str = "") -> Optional[float]:
        if not self.is_connected():
            return None
        wanted_con_id = int(contract.con_id or getattr(contract.raw, "conId", 0) or 0)
        wanted_symbol = str(contract.ticker or getattr(contract.raw, "symbol", "") or "").upper()
        account = account.strip()
        try:
            positions = list(self.ib.positions() or [])
        except Exception:
            try:
                positions = list(self.ib.reqPositions() or [])
                self.ib.sleep(0.5)
            except Exception:
                return None
        total = 0.0
        found = False
        for pos in positions:
            pos_contract = getattr(pos, "contract", None)
            pos_account = str(getattr(pos, "account", "") or "")
            if account and pos_account and pos_account != account:
                continue
            pos_con_id = int(getattr(pos_contract, "conId", 0) or 0) if pos_contract is not None else 0
            pos_symbol = str(getattr(pos_contract, "symbol", "") or "").upper() if pos_contract is not None else ""
            if (wanted_con_id and pos_con_id == wanted_con_id) or (wanted_symbol and pos_symbol == wanted_symbol):
                try:
                    total += float(getattr(pos, "position", 0.0) or 0.0)
                    found = True
                except Exception:
                    pass
        return total if found else None

