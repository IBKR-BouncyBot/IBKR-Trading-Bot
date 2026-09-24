"""Recover legacy Auto accounts only from exact archived broker fill evidence."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.ib_adapter import QualifiedContract
from app.models import ConnectionSettings, CycleState, Stage, StrategySettings
from app.storage import BotStorage


class LegacyAccountEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "app" / "controller.py"
        spec = importlib.util.spec_from_file_location("app._legacy_account_test_controller", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"IBKR_BOT_HEADLESS_SIGNALS": "1"}):
            spec.loader.exec_module(module)
        cls.controller_type = module.TradingController

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = BotStorage(Path(self.folder.name) / "legacy.sqlite")
        self.controller = self.controller_type(self.storage)
        self.strategy = StrategySettings(ticker="AAA", contract_con_id=111, atr_adaptive_enabled=False)
        self.controller.strategy = self.strategy
        self.controller.connection = ConnectionSettings(account="")
        self.controller.emit_snapshot = lambda **kwargs: None
        self.cycle = CycleState.new(self.strategy, 1, "", 100, 0)
        self.cycle.stage = Stage.WAIT_RISE_TRIGGER
        self.cycle.buy_filled_qty = 42
        self.cycle.avg_buy_price = 100
        self.cycle.buy_order_ref = "IBKRBOT|AAA|CYCLE-000001|legacy|BUY_TRAIL"
        self.cycle.buy_order_id = 101
        self.cycle.buy_perm_id = 201
        self.cycle.buy_status = "Filled"
        self.storage.upsert_cycle(self.cycle)
        self.controller.active_cycle = self.cycle
        self.accounts = ["ACCOUNT_A", "ACCOUNT_B"]
        self.contract = QualifiedContract("AAA", 111, SimpleNamespace(), currency="USD")
        self.controller.adapter = SimpleNamespace(
            requires_exact_contract_selection=True,
            managed_accounts=lambda: self.accounts,
            poll_order=lambda ref: None,
            recent_executions=lambda: [],
            is_connected=lambda: True,
            qualify_stock=lambda *args: self.contract,
            open_app_orders=lambda: [],
            position_size=lambda *args, **kwargs: 42,
        )

    def tearDown(self):
        self.controller._market_capture.shutdown()
        self.folder.cleanup()

    def fill_repr(self, **changes):
        values = {
            "execId": "legacy-1", "acctNumber": "ACCOUNT_A", "orderRef": self.cycle.buy_order_ref,
            "orderId": 101, "permId": 201, "side": "BOT",
            "conId": 111, "symbol": "AAA", "currency": "USD",
        }
        values.update(changes)
        contract = ", ".join(f"{key}={values[key]!r}" for key in ("conId", "symbol", "currency"))
        execution = ", ".join(f"{key}={values[key]!r}" for key in ("execId", "acctNumber", "orderRef", "orderId", "permId", "side"))
        return (
            f"Fill(contract=Stock({contract}), execution=Execution({execution}), "
            "commissionReport=CommissionReport(), time=datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.timezone.utc))"
        )

    def store_fill(self, text=None, *, execution_id="legacy-1", raw=None):
        self.storage.add_execution(
            cycle=self.cycle, ticker="AAA", side="BUY", shares=42, price=100,
            execution_id=execution_id, order_ref=self.cycle.buy_order_ref, order_id=101, perm_id=201,
            raw=raw if raw is not None else {"event_type": "EXEC_DETAILS", "raw_args": [text or self.fill_repr()]},
        )

    def test_legacy_fill_binds_exact_account_and_preserves_execution_evidence(self):
        self.store_fill()
        before = self.storage.get_cycle_audit_bundle(self.cycle.id)["executions"]
        self.assertIsNone(self.controller._bind_cycle_account(self.cycle))
        self.assertEqual(self.cycle.account, "ACCOUNT_A")
        self.assertEqual(self.storage.get_cycle(self.cycle.id).account, "ACCOUNT_A")
        self.assertEqual(self.storage.get_cycle_audit_bundle(self.cycle.id)["executions"], before)

    def test_start_resumes_one_legacy_cycle_with_exact_fill_and_current_position(self):
        self.store_fill()
        self.controller.connected = True
        self.controller._startup_resume_required = True
        self.controller._handle_command("START_STRATEGY", {
            "connection": self.controller.connection, "strategy": self.strategy,
        })
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(self.controller.active_cycle.account, "ACCOUNT_A")
        self.assertFalse(self.controller._startup_resume_required)
        self.assertFalse(self.controller._recovery_required)

    def test_current_structured_evidence_still_binds(self):
        self.store_fill(raw={"account": "ACCOUNT_A", "con_id": 111, "order_ref": self.cycle.buy_order_ref})
        self.assertIsNone(self.controller._bind_cycle_account(self.cycle))
        self.assertEqual(self.cycle.account, "ACCOUNT_A")

    def test_explicit_historical_resolution_then_start_preserves_current_position(self):
        self.store_fill()
        historical = CycleState.new(self.strategy, 2, "ACCOUNT_A", 100, 0)
        historical.stage = Stage.CYCLE_COMPLETE
        historical.buy_order_ref = "IBKRBOT|AAA|CYCLE-000002|history|BUY_TRAIL"
        historical.buy_status = "CancelRequested"
        self.storage.upsert_cycle(historical)
        self.controller.connected = True
        self.controller._startup_resume_required = True
        payload = {"connection": self.controller.connection, "strategy": self.strategy}
        self.controller._handle_command("START_STRATEGY", payload)
        self.assertTrue(self.controller._startup_resume_required)
        self.assertIn("Multiple unresolved cycles", self.controller.status)
        self.controller._handle_command("MARK_HISTORICAL_CYCLE_MANUALLY_HANDLED", {
            "cycle_id": historical.id,
            "note": "Historical broker orders and shares independently reconciled by operator.",
            "expected_cycle": historical.to_dict(),
        })
        self.assertTrue(self.controller._startup_resume_required)
        self.controller._handle_command("START_STRATEGY", payload)
        self.assertEqual(self.controller.active_cycle.id, self.cycle.id)
        self.assertEqual(self.controller.active_cycle.buy_filled_qty, 42)
        self.assertEqual(self.controller.active_cycle.stage, Stage.WAIT_RISE_TRIGGER)
        self.assertEqual(self.controller.active_cycle.account, "ACCOUNT_A")
        self.assertFalse(self.controller._startup_resume_required)

    def test_blank_order_reference_cannot_establish_legacy_ownership(self):
        self.cycle.buy_order_ref = ""
        self.store_fill(self.fill_repr(orderRef=""))
        self.storage.add_order(
            cycle=self.cycle, action="BUY", order_type="TRAIL", order_id=101, perm_id=201,
            order_ref="", quantity=42, trailing_percent=1, initial_stop_price=100, status="Filled",
        )
        self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))
        self.assertEqual(self.cycle.account, "")

    def test_nonpositive_contract_id_cannot_establish_legacy_ownership(self):
        for con_id in (0, -111):
            with self.subTest(con_id=con_id):
                self.cycle.con_id = con_id
                self.store_fill(self.fill_repr(conId=con_id))
                self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))
                self.assertEqual(self.cycle.account, "")

    def test_conflicting_legacy_accounts_fail_closed(self):
        self.store_fill()
        self.store_fill(self.fill_repr(execId="legacy-2", acctNumber="ACCOUNT_B"), execution_id="legacy-2")
        self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))
        self.assertEqual(self.cycle.account, "")

    def test_structured_account_cannot_hide_conflicting_legacy_evidence(self):
        self.store_fill(raw={
            "account": "ACCOUNT_B", "con_id": 111, "order_ref": self.cycle.buy_order_ref,
            "raw_args": [self.fill_repr()],
        })
        self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))
        self.assertEqual(self.cycle.account, "")

    def test_legacy_identity_mismatches_cannot_authorize_account(self):
        for field, value in (
            ("execId", "other-execution"), ("orderRef", "IBKRBOT|OTHER|BUY"),
            ("conId", 222), ("symbol", "BBB"), ("currency", "EUR"),
            ("orderId", 999), ("permId", 999), ("side", "SLD"),
            ("conId", True), ("orderId", True), ("acctNumber", ""),
        ):
            with self.subTest(field=field, value=value):
                self.store_fill(self.fill_repr(**{field: value}))
                self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))
                self.assertEqual(self.cycle.account, "")

    def test_order_table_identity_conflict_cannot_authorize_account(self):
        self.store_fill()
        self.storage.add_order(
            cycle=self.cycle, action="BUY", order_type="TRAIL", order_id=999, perm_id=201,
            order_ref=self.cycle.buy_order_ref, quantity=42, trailing_percent=1,
            initial_stop_price=100, status="Filled",
        )
        self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))
        self.assertEqual(self.cycle.account, "")

    def test_only_direct_fill_is_accepted(self):
        for text in (f"Trade(fills=[{self.fill_repr()}])", f"[{self.fill_repr()}]", "malformed Fill("):
            with self.subTest(text=text[:30]):
                self.store_fill(text)
                self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))

    def test_executable_account_expression_is_never_evaluated(self):
        marker = Path(self.folder.name) / "executed.txt"
        expression = f"__import__('pathlib').Path({str(marker)!r}).write_text('unsafe')"
        text = self.fill_repr().replace("acctNumber='ACCOUNT_A'", f"acctNumber={expression}")
        self.store_fill(text)
        self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))
        self.assertFalse(marker.exists())

    def test_oversize_and_excessively_nested_payloads_fail_closed(self):
        for text in (self.fill_repr() + " " * 40000, "(" * 1000 + self.fill_repr() + ")" * 1000):
            with self.subTest(length=len(text)):
                self.store_fill(text)
                self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))

    def test_managed_and_configured_account_constraints_remain_required(self):
        self.store_fill()
        self.accounts[:] = ["ACCOUNT_B"]
        self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))
        self.accounts.append("ACCOUNT_A")
        self.controller.connection.account = "ACCOUNT_B"
        self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))
        self.assertEqual(self.cycle.account, "")

    def test_generic_stock_contract_is_accepted_but_nonstock_contract_is_not(self):
        self.store_fill(self.fill_repr().replace("Stock(", "Contract(secType='STK', "))
        self.assertIsNone(self.controller._bind_cycle_account(self.cycle))
        self.cycle.account = ""
        self.store_fill(self.fill_repr().replace("Stock(", "Contract(secType='CASH', "))
        self.assertIsNotNone(self.controller._bind_cycle_account(self.cycle))


if __name__ == "__main__":
    unittest.main()
