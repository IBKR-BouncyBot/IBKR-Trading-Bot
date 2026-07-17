"""Deterministic broker boundary for non-network integration tests.

The fake models the application-visible parts of IB Gateway: independent local
and upstream connectivity, event-stamped market data, app-owned orders,
executions, cancellations, and recovery snapshots.  It never opens a socket and
never depends on ``ib_async``.
"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.ib_adapter import (
    BrokerAdapter,
    BrokerAdapterError,
    BrokerConnectivityStatus,
    ContractSearchResult,
    MarketPriceSnapshot,
    OrderHandle,
    PolledOrderState,
    QualifiedContract,
    RthStatus,
)
from app.models import APP_ORDER_PREFIX, utc_now_iso

_WORKING_STATUSES = {"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}


class DeterministicBrokerAdapter(BrokerAdapter):
    """Stateful in-memory BrokerAdapter with explicit event injection."""

    def __init__(self, *, ticker: str = "AAPL", con_id: int = 123) -> None:
        self.local_connected = True
        self.upstream_connected: Optional[bool] = True
        self.upstream_state = "connected"
        self.upstream_message = "Deterministic broker is ready."
        self.upstream_error_code: Optional[int] = None
        self.market_data_resubscribe_required = False
        self.awaiting_fresh_market_data = False
        self.market_data_event_tracking = True

        self.contract = QualifiedContract(
            ticker=ticker.upper(),
            con_id=con_id,
            raw=object(),
            primary_exchange="NASDAQ",
            local_symbol=ticker.upper(),
            trading_class="NMS",
            min_tick=0.01,
        )
        self.market_data_type = 1
        self.market_data_type_requests: list[int] = []
        self.subscription_generation = 1
        self.update_sequence = 0
        self._snapshot = self._make_snapshot(price=100.0, sequence=0)

        self.orders: dict[str, PolledOrderState] = {}
        self.placed_orders: list[dict[str, Any]] = []
        self.cancelled_orders: list[str] = []
        self.executions: list[dict[str, Any]] = []
        self.events: deque[dict[str, Any]] = deque()
        self.external_position = 0.0
        self.rth_open = True
        self.process_event_calls = 0
        self.next_order_id = 1000
        self.fail_operations: set[str] = set()
        self.accounts = ["DU_TEST"]

    @property
    def subscription_id(self) -> str:
        return f"{self.contract.ticker}:{self.contract.con_id}:g{self.subscription_generation}"

    def _make_snapshot(self, *, price: float, sequence: int) -> MarketPriceSnapshot:
        return MarketPriceSnapshot(
            price=float(price),
            source="last",
            requested_market_data_type=int(self.market_data_type),
            subscription_market_data_type=int(self.market_data_type),
            fields={
                "bid": float(price) - 0.01,
                "ask": float(price) + 0.01,
                "last": float(price),
                "close": float(price) - 0.50,
            },
            timestamp=utc_now_iso(),
            status="OK",
            api_data_received=sequence > 0,
            api_data_field_count=4,
            ticker_update_time=utc_now_iso() if sequence > 0 else "",
            market_data_update_sequence=sequence,
            market_data_subscription_id=self.subscription_id,
            market_data_update_received_at=utc_now_iso() if sequence > 0 else "",
            market_data_update_age_seconds=0.0 if sequence > 0 else None,
            market_data_event_tracking=True,
            market_data_event_tracking_available=True,
            upstream_connected=self.upstream_connected,
            upstream_state=self.upstream_state,
            upstream_message=self.upstream_message,
            upstream_error_code=self.upstream_error_code,
        )

    def _require_ready(self, operation: str) -> None:
        if operation in self.fail_operations:
            raise BrokerAdapterError(f"Injected {operation} failure.")
        if not self.local_connected:
            raise BrokerAdapterError("Local API connection is unavailable.")
        if self.upstream_connected is not True:
            raise BrokerAdapterError("Gateway is not connected to IBKR servers.")

    def connect(self, host: str, port: int, client_id: int, market_data_type: int = 1) -> None:
        del host, port, client_id
        if "connect" in self.fail_operations:
            raise BrokerAdapterError("Injected connect failure.")
        self.local_connected = True
        self.upstream_connected = True
        self.upstream_state = "connected"
        self.upstream_message = "Deterministic broker is ready."
        self.upstream_error_code = None
        self.market_data_type = int(market_data_type)

    def disconnect(self) -> None:
        self.local_connected = False
        self.upstream_connected = False
        self.upstream_state = "local_disconnected"
        self.upstream_message = "Local API connection is unavailable."
        self.upstream_error_code = None

    def is_connected(self) -> bool:
        return bool(self.local_connected)

    def connectivity_status(self) -> BrokerConnectivityStatus:
        return BrokerConnectivityStatus(
            local_connected=self.local_connected,
            upstream_connected=self.upstream_connected,
            state=self.upstream_state,
            message=self.upstream_message,
            error_code=self.upstream_error_code,
            changed_at=utc_now_iso(),
            market_data_resubscribe_required=self.market_data_resubscribe_required,
            awaiting_fresh_market_data=self.awaiting_fresh_market_data,
            market_data_event_tracking=self.market_data_event_tracking,
        )

    def process_events(self, timeout: float = 0.0) -> None:
        del timeout
        self.process_event_calls += 1

    def set_market_data_type(self, market_data_type: int) -> None:
        self.market_data_type = int(market_data_type)
        self.market_data_type_requests.append(self.market_data_type)

    def managed_accounts(self) -> list[str]:
        return list(self.accounts)

    def search_stock_contracts(self, query: str, max_results: int = 16) -> list[ContractSearchResult]:
        text = str(query or "").strip().upper()
        if not text:
            return []
        return [
            ContractSearchResult(
                symbol=text,
                sec_type="STK",
                currency="USD",
                exchange="SMART",
                primary_exchange="NASDAQ",
                con_id=self.contract.con_id,
                local_symbol=text,
                trading_class="NMS",
                description=f"Deterministic {text}",
            )
        ][: max(0, int(max_results))]

    def qualify_stock(
        self,
        ticker: str,
        exchange: str,
        currency: str,
        primary_exchange: str = "",
        con_id: Optional[int] = None,
    ) -> QualifiedContract:
        del exchange, currency
        self._require_ready("qualify")
        return QualifiedContract(
            ticker=str(ticker).upper(),
            con_id=int(con_id or self.contract.con_id or 0),
            raw=object(),
            primary_exchange=primary_exchange or self.contract.primary_exchange,
            local_symbol=str(ticker).upper(),
            trading_class=self.contract.trading_class,
            min_tick=self.contract.min_tick,
        )

    def publish_price(
        self,
        price: float,
        *,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        close: Optional[float] = None,
        timestamp: Optional[str] = None,
    ) -> MarketPriceSnapshot:
        self.update_sequence += 1
        value = float(price)
        snapshot = self._make_snapshot(price=value, sequence=self.update_sequence)
        snapshot.fields = {
            "bid": float(bid if bid is not None else value - 0.01),
            "ask": float(ask if ask is not None else value + 0.01),
            "last": value,
            "close": float(close if close is not None else value - 0.50),
        }
        snapshot.timestamp = timestamp or utc_now_iso()
        snapshot.ticker_update_time = snapshot.timestamp
        snapshot.market_data_update_received_at = snapshot.timestamp
        self._snapshot = snapshot
        self.awaiting_fresh_market_data = False
        return deepcopy(snapshot)

    def cached_snapshot(self) -> MarketPriceSnapshot:
        return deepcopy(self._snapshot)

    def price_snapshot(self, contract: QualifiedContract, timeout: float = 1.0) -> MarketPriceSnapshot:
        del contract, timeout
        if "price" in self.fail_operations:
            raise BrokerAdapterError("Injected price failure.")
        snapshot = deepcopy(self._snapshot)
        snapshot.requested_market_data_type = self.market_data_type
        snapshot.subscription_market_data_type = self.market_data_type
        snapshot.upstream_connected = self.upstream_connected
        snapshot.upstream_state = self.upstream_state
        snapshot.upstream_message = self.upstream_message
        snapshot.upstream_error_code = self.upstream_error_code
        return snapshot

    def regular_trading_hours_status(self, contract: QualifiedContract) -> RthStatus:
        del contract
        if "rth" in self.fail_operations:
            raise BrokerAdapterError("Injected RTH failure.")
        checked = datetime.now(timezone.utc)
        session_open = checked - timedelta(hours=1)
        session_close = checked + timedelta(hours=1)
        return RthStatus(
            is_open=self.rth_open,
            source="deterministic",
            message="RTH open" if self.rth_open else "RTH closed",
            checked_at=checked.isoformat(),
            liquid_hours="",
            time_zone="UTC",
            session_open=session_open.isoformat(),
            session_close=session_close.isoformat(),
            session_date=checked.strftime("%Y%m%d"),
        )

    def what_if_trailing_stop(self, **kwargs: Any) -> dict[str, Any]:
        self._require_ready("what_if")
        return {"ok": True, "message": "deterministic what-if accepted", "request": dict(kwargs)}

    def what_if_market_order(self, **kwargs: Any) -> dict[str, Any]:
        self._require_ready("what_if")
        return {"ok": True, "message": "deterministic what-if accepted", "request": dict(kwargs)}

    def _place(self, *, order_type: str, **kwargs: Any) -> OrderHandle:
        self._require_ready("place")
        order_ref = str(kwargs["order_ref"])
        quantity = int(kwargs["quantity"])
        self.next_order_id += 1
        order_id = self.next_order_id
        perm_id = order_id + 100_000
        raw = {"order_type": order_type, **deepcopy(kwargs)}
        state = PolledOrderState(
            order_ref=order_ref,
            order_id=order_id,
            perm_id=perm_id,
            status="Submitted",
            filled=0,
            remaining=quantity,
            avg_fill_price=0.0,
            commission=0.0,
            executions=[],
            raw=raw,
        )
        self.orders[order_ref] = state
        self.placed_orders.append(raw)
        self.events.append(
            {
                "event_type": "OPEN_ORDER",
                "created_at": utc_now_iso(),
                "order_ref": order_ref,
                "order_id": order_id,
                "perm_id": perm_id,
                "status": "Submitted",
                "ticker": self.contract.ticker,
            }
        )
        return OrderHandle(order_ref, order_id, perm_id, "Submitted", raw)

    def place_trailing_stop(self, **kwargs: Any) -> OrderHandle:
        return self._place(order_type="TRAIL", **kwargs)

    def place_market_order(self, **kwargs: Any) -> OrderHandle:
        return self._place(order_type="MKT", **kwargs)

    def cancel_order(self, order_ref: str, order_id: Optional[int] = None) -> None:
        del order_id
        self._require_ready("cancel")
        state = self.orders.get(order_ref)
        if state is None:
            raise BrokerAdapterError(f"Unknown order reference: {order_ref}")
        self.cancelled_orders.append(order_ref)
        self.orders[order_ref] = replace(state, status="Cancelled", remaining=max(0, state.remaining))
        self.events.append(
            {
                "event_type": "ORDER_STATUS",
                "created_at": utc_now_iso(),
                "order_ref": order_ref,
                "order_id": state.order_id,
                "perm_id": state.perm_id,
                "status": "Cancelled",
                "ticker": self.contract.ticker,
            }
        )

    def fill_order(
        self,
        order_ref: str,
        *,
        shares: int,
        price: float,
        commission: float = 0.0,
        execution_id: Optional[str] = None,
        terminal: Optional[bool] = None,
    ) -> PolledOrderState:
        state = self.orders[order_ref]
        total = state.filled + state.remaining
        cumulative = min(total, state.filled + int(shares))
        remaining = max(0, total - cumulative)
        if terminal is None:
            terminal = remaining == 0
        status = "Filled" if terminal else "Submitted"
        exec_id = execution_id or f"EXEC-{order_ref}-{len(state.executions) + 1}"
        execution = {
            "ticker": self.contract.ticker,
            "side": "BOT" if "BUY" in order_ref.upper() else "SLD",
            "shares": float(shares),
            "price": float(price),
            "avgPrice": float(price),
            "commission": float(commission),
            "currency": "USD",
            "order_ref": order_ref,
            "orderRef": order_ref,
            "order_id": state.order_id,
            "perm_id": state.perm_id,
            "execution_id": exec_id,
            "execId": exec_id,
            "time": utc_now_iso(),
            "account": "",
        }
        executions = [*state.executions, execution]
        new_state = replace(
            state,
            status=status,
            filled=cumulative,
            remaining=remaining,
            avg_fill_price=float(price),
            commission=float(commission),
            executions=executions,
            raw={**state.raw, "status": status, "filled": cumulative},
        )
        self.orders[order_ref] = new_state
        if not any(str(row.get("execution_id")) == exec_id for row in self.executions):
            self.executions.append(deepcopy(execution))
        self.events.extend(
            [
                {
                    "event_type": "EXEC_DETAILS",
                    "created_at": utc_now_iso(),
                    **deepcopy(execution),
                },
                {
                    "event_type": "ORDER_STATUS",
                    "created_at": utc_now_iso(),
                    "order_ref": order_ref,
                    "order_id": state.order_id,
                    "perm_id": state.perm_id,
                    "status": status,
                    "filled": cumulative,
                    "remaining": remaining,
                    "ticker": self.contract.ticker,
                },
            ]
        )
        if commission:
            self.events.append(
                {
                    "event_type": "COMMISSION_REPORT",
                    "created_at": utc_now_iso(),
                    "order_ref": order_ref,
                    "order_id": state.order_id,
                    "perm_id": state.perm_id,
                    "execution_id": exec_id,
                    "commission": float(commission),
                    "ticker": self.contract.ticker,
                }
            )
        return deepcopy(new_state)

    def poll_order(self, order_ref: str) -> Optional[PolledOrderState]:
        if "poll" in self.fail_operations:
            raise BrokerAdapterError("Injected poll failure.")
        state = self.orders.get(order_ref)
        return deepcopy(state) if state is not None else None

    def open_app_orders(self) -> list[PolledOrderState]:
        if "open_orders" in self.fail_operations:
            raise BrokerAdapterError("Injected open-order failure.")
        return [
            deepcopy(state)
            for ref, state in self.orders.items()
            if ref.startswith(APP_ORDER_PREFIX) and state.status in _WORKING_STATUSES
        ]

    def recent_executions(self) -> list[dict[str, Any]]:
        if "executions" in self.fail_operations:
            raise BrokerAdapterError("Injected execution-query failure.")
        return deepcopy(self.executions)

    def drain_broker_events(self) -> list[dict[str, Any]]:
        result = list(self.events)
        self.events.clear()
        return deepcopy(result)

    def position_size(self, contract: QualifiedContract, account: str = "") -> Optional[float]:
        del contract, account
        return float(self.external_position)

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
        del order_id, perm_id, ticker, account, action
        if "recover" in self.fail_operations:
            raise BrokerAdapterError("Injected recovery failure.")
        return self.poll_order(order_ref)

    def upstream_lost(self, *, code: int = 1100, message: str = "Connectivity between IBKR and TWS has been lost.") -> None:
        self.upstream_connected = False
        self.upstream_error_code = int(code)
        self.upstream_state = "upstream_disconnected" if code == 1100 else "api_port_reset"
        self.upstream_message = message
        self.awaiting_fresh_market_data = True
        self.events.append(
            {
                "event_type": "IBKR_UPSTREAM_DISCONNECTED" if code == 1100 else "IBKR_API_PORT_RESET",
                "created_at": utc_now_iso(),
                "error_code": code,
                "message": message,
                "local_connected": self.local_connected,
                "upstream_connected": False,
                "upstream_state": self.upstream_state,
                "market_data_resubscribe_required": False,
                "awaiting_fresh_market_data": True,
            }
        )

    def upstream_restored(self, *, data_lost: bool) -> None:
        self.upstream_connected = True
        self.upstream_error_code = 1101 if data_lost else 1102
        self.upstream_state = "restored_data_lost" if data_lost else "restored_data_maintained"
        self.upstream_message = "Connectivity between IBKR and TWS has been restored."
        self.market_data_resubscribe_required = bool(data_lost)
        self.awaiting_fresh_market_data = True
        if data_lost:
            self.subscription_generation += 1
            self.update_sequence = 0
            self._snapshot = self._make_snapshot(price=float(self._snapshot.price or 100.0), sequence=0)
        else:
            self._snapshot.market_data_update_sequence = 0
            self._snapshot.market_data_update_received_at = ""
            self._snapshot.market_data_update_age_seconds = None
        self.events.append(
            {
                "event_type": "IBKR_UPSTREAM_RESTORED_DATA_LOST" if data_lost else "IBKR_UPSTREAM_RESTORED_DATA_MAINTAINED",
                "created_at": utc_now_iso(),
                "error_code": self.upstream_error_code,
                "message": self.upstream_message,
                "local_connected": self.local_connected,
                "upstream_connected": True,
                "upstream_state": self.upstream_state,
                "market_data_resubscribe_required": data_lost,
                "awaiting_fresh_market_data": True,
            }
        )

    def clone_broker_snapshot(self) -> dict[str, Any]:
        """Return a deep, serializable state snapshot for replay/idempotence tests."""
        return {
            "connectivity": self.connectivity_status().to_dict(),
            "orders": {
                key: deepcopy(value.raw)
                | {"status": value.status, "filled": value.filled, "remaining": value.remaining}
                for key, value in self.orders.items()
            },
            "executions": deepcopy(self.executions),
            "subscription_id": self.subscription_id,
            "update_sequence": self.update_sequence,
        }


def timestamp_at(second: int) -> str:
    """Stable UTC timestamp used by generated market-data scenarios."""
    return datetime(2026, 7, 10, 14, 30, int(second), tzinfo=timezone.utc).isoformat()
