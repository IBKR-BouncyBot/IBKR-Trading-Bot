#!/usr/bin/env python3
"""Run a small deterministic mutation gate for safety-critical contracts.

This is intentionally a smoke suite rather than a full mutation campaign. Each
mutant changes one financial or state-machine condition in a temporary copy of
``app``. A focused independent probe must pass against the original copy and
fail against the mutant. Production sources are never edited in place.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Mutation:
    name: str
    relative_path: str
    original: str
    replacement: str
    probe: str
    occurrence: int = 1


MUTATIONS = (
    Mutation(
        name="buy slippage increases sizing price",
        relative_path="app/models.py",
        original="return 1.0 + pct / 100.0",
        replacement="return 1.0 - pct / 100.0",
        probe="""
from app.models import effective_buy_sizing_price, slippage_factor
assert slippage_factor(True, 2.0) == 1.02
assert effective_buy_sizing_price(100.0, True, 2.0) == 102.0
""",
    ),
    Mutation(
        name="BUY native trail triggers at exact stop",
        relative_path="app/simulation.py",
        original="return price >= self.stop_price",
        replacement="return price > self.stop_price",
        probe="""
from app.simulation import NativeTrailSimulator
trail = NativeTrailSimulator("BUY", 101.0, 1.0)
assert trail.update(101.0) is True
""",
    ),
    Mutation(
        name="SELL native trail triggers at exact stop",
        relative_path="app/simulation.py",
        original="return price <= self.stop_price",
        replacement="return price < self.stop_price",
        probe="""
from app.simulation import NativeTrailSimulator
trail = NativeTrailSimulator("SELL", 99.0, 1.0)
assert trail.update(99.0) is True
""",
    ),
    Mutation(
        name="initial drop triggers at configured boundary",
        relative_path="app/strategy.py",
        original="if last_price <= next_cycle.drop_trigger_price:",
        replacement="if last_price < next_cycle.drop_trigger_price:",
        probe="""
from app.models import StrategySettings
from app.strategy import StrategyEngine
settings = StrategySettings(
    ticker="AAPL",
    initial_drop_pct=2.0,
    atr_adaptive_enabled=False,
    atr_block_new_buy_until_ready=False,
)
cycle = StrategyEngine.start_cycle(settings, 1, "", 100.0, 0.0)
updated, actions = StrategyEngine.on_price_update(cycle, 98.0, is_rth=True)
assert [action.action_type for action in actions] == ["PLACE_BUY_TRAIL"]
""",
    ),
    Mutation(
        name="SELL PnL uses overlapping bought and sold quantity",
        relative_path="app/strategy.py",
        original="qty = min(next_cycle.buy_filled_qty, next_cycle.sell_filled_qty)",
        replacement="qty = max(next_cycle.buy_filled_qty, next_cycle.sell_filled_qty)",
        occurrence=2,
        probe="""
from app.models import StrategySettings
from app.strategy import StrategyEngine
cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
cycle.buy_filled_qty = 10
cycle.avg_buy_price = 100.0
completed = StrategyEngine.on_sell_fill(cycle, 12, 101.0, "Filled")
assert completed.gross_pnl == 10.0
""",
    ),
    Mutation(
        name="app-owned unsold quantity honors either completed exit leg",
        relative_path="app/storage.py",
        original="remaining = max(0, bought - max(final_sold, protective_sold))",
        replacement="remaining = max(0, bought - min(final_sold, protective_sold))",
        probe="""
import tempfile
from pathlib import Path
from app.models import Stage, StrategySettings
from app.storage import BotStorage
from app.strategy import StrategyEngine
with tempfile.TemporaryDirectory() as folder:
    storage = BotStorage(Path(folder) / "state.sqlite")
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
    cycle.stage = Stage.CYCLE_COMPLETE
    cycle.buy_filled_qty = 10
    cycle.sell_filled_qty = 10
    cycle.protective_sell_filled_qty = 0
    storage.upsert_cycle(cycle)
    assert storage.get_app_owned_unsold_position("AAPL")["quantity"] == 0
""",
    ),
    Mutation(
        name="BUY prices round upward to the broker increment",
        relative_path="app/ib_adapter.py",
        original='"up": decimal.ROUND_CEILING,',
        replacement='"up": decimal.ROUND_FLOOR,',
        probe="""
from app.ib_adapter import _round_decimal_increment
assert _round_decimal_increment(42.5996, 0.01, "up") == 42.6
""",
    ),
    Mutation(
        name="SELL prices round downward to the broker increment",
        relative_path="app/ib_adapter.py",
        original='"down": decimal.ROUND_FLOOR,',
        replacement='"down": decimal.ROUND_CEILING,',
        probe="""
from app.ib_adapter import _round_decimal_increment
assert _round_decimal_increment(186.93288, 0.02, "down") == 186.92
""",
    ),
    Mutation(
        name="market-rule boundaries include the exact low edge",
        relative_path="app/ib_adapter.py",
        original="if float(price) + 1e-12 >= row.low_edge:",
        replacement="if float(price) > row.low_edge:",
        probe="""
from app.ib_adapter import IbAsyncTwsAdapter, PriceIncrementBand
rows = (
    PriceIncrementBand(low_edge=0.0, increment=0.0001),
    PriceIncrementBand(low_edge=1.0, increment=0.01),
)
assert IbAsyncTwsAdapter._increment_for_market_price(1.0, rows) == 0.01
""",
    ),
    Mutation(
        name="what-if trailing orders keep transmit enabled",
        relative_path="app/ib_adapter.py",
        original="            transmit=True,\n            whatIf=True,",
        replacement="            transmit=False,\n            whatIf=True,",
        occurrence=1,
        probe="""
from types import SimpleNamespace
from app.ib_adapter import IbAsyncTwsAdapter, QualifiedContract
class Order:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
class IB:
    def __init__(self):
        self.order = None
    def isConnected(self):
        return True
    def whatIfOrder(self, _contract, order):
        self.order = order
        return SimpleNamespace(
            status="PreSubmitted",
            warningText="",
            initMarginChange="0",
            maintMarginChange="0",
            equityWithLoanChange="0",
        )
adapter = IbAsyncTwsAdapter()
adapter.ib = IB()
adapter._upstream_connected = True
adapter._require_ib_async = lambda: (None, Order, None)
contract = QualifiedContract("AAPL", 1, SimpleNamespace())
result = adapter.what_if_trailing_stop(
    contract=contract,
    action="BUY",
    quantity=1,
    trailing_percent=1.0,
    initial_stop_price=101.0,
    order_ref="IBKRBOT|AAPL|CYCLE-1|MUTATION|BUY_TRAIL|WHATIF",
)
assert result["ok"] is True
assert adapter.ib.order.transmit is True
assert adapter.ib.order.whatIf is True
""",
    ),
    Mutation(
        name="database currency lock rejects a different currency",
        relative_path="app/storage.py",
        original="if current and current != requested:",
        replacement="if current and current == requested:",
        occurrence=1,
        probe="""
import tempfile
from pathlib import Path
from app.storage import BotStorage, DatabaseCurrencyError
with tempfile.TemporaryDirectory() as folder:
    storage = BotStorage(Path(folder) / "state.sqlite")
    assert storage.claim_database_contract_currency("USD") == "USD"
    try:
        storage.claim_database_contract_currency("EUR")
    except DatabaseCurrencyError:
        pass
    else:
        raise AssertionError("cross-currency claim was accepted")
""",
    ),
    Mutation(
        name="automatic reconnect interval remains ten seconds",
        relative_path="app/controller.py",
        original="RECONNECT_INTERVAL_SECONDS = 10.0",
        replacement="RECONNECT_INTERVAL_SECONDS = 1.0",
        probe="""
import sys
import types
qtcore = types.ModuleType("PySide6.QtCore")
class QObject:
    pass
class Signal:
    def __init__(self, *_args, **_kwargs):
        pass
    def emit(self, *_args, **_kwargs):
        pass
qtcore.QObject = QObject
qtcore.Signal = Signal
pyside6 = types.ModuleType("PySide6")
pyside6.QtCore = qtcore
sys.modules["PySide6"] = pyside6
sys.modules["PySide6.QtCore"] = qtcore
from app.controller import TradingController
assert TradingController.RECONNECT_INTERVAL_SECONDS == 10.0
""",
    ),
    Mutation(
        name="execution identifiers remain idempotency keys",
        relative_path="app/storage.py",
        original="            if execution_key:\n                existing = con.execute(",
        replacement="            if False and execution_key:\n                existing = con.execute(",
        probe="""
import tempfile
from pathlib import Path
from app.storage import BotStorage
with tempfile.TemporaryDirectory() as folder:
    storage = BotStorage(Path(folder) / "state.sqlite")
    for _ in range(2):
        storage.upsert_execution(
            cycle=None,
            ticker="AAPL",
            side="BUY",
            shares=5,
            price=100.0,
            execution_id="EXEC-ONE",
        )
    with storage.connect() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM executions WHERE execution_id='EXEC-ONE'"
        ).fetchone()[0]
    assert count == 1
""",
    ),
    Mutation(
        name="late zero commission cannot erase a known commission",
        relative_path="app/storage.py",
        original=(
            "                    or (commission_value == 0.0 and "
            "float(current.get(\"commission\") or 0.0) != 0.0)"
        ),
        replacement="",
        probe="""
import tempfile
from pathlib import Path
from app.storage import BotStorage
with tempfile.TemporaryDirectory() as folder:
    storage = BotStorage(Path(folder) / "state.sqlite")
    storage.upsert_execution(
        cycle=None,
        ticker="AAPL",
        side="BUY",
        shares=5,
        price=100.0,
        commission=0.75,
        execution_id="EXEC-COMMISSION",
    )
    storage.upsert_execution(
        cycle=None,
        ticker="AAPL",
        side="BUY",
        shares=5,
        price=100.0,
        commission=0.0,
        execution_id="EXEC-COMMISSION",
    )
    assert storage.get_execution("EXEC-COMMISSION")["commission"] == 0.75
""",
    ),
    Mutation(
        name="LSE continuous-session close remains 16:30 London",
        relative_path="app/ib_adapter.py",
        original='"LSE": ("Europe/London", dt.time(8, 0), dt.time(16, 30)),',
        replacement='"LSE": ("Europe/London", dt.time(8, 0), dt.time(16, 50)),',
        probe="""
import datetime as dt
from app.ib_adapter import IbAsyncTwsAdapter
raw = IbAsyncTwsAdapter._parse_liquid_hours_window(
    "20260724:0900-20260724:1750",
    "MET",
    dt.datetime.fromisoformat("2026-07-24T15:45:00+00:00"),
)
assert raw is not None
status = IbAsyncTwsAdapter._apply_primary_exchange_continuous_session(
    raw,
    "LSE",
    dt.datetime.fromisoformat("2026-07-24T15:45:00+00:00"),
)
assert status.is_open is False
assert dt.datetime.fromisoformat(status.session_close).astimezone(
    dt.timezone.utc
) == dt.datetime.fromisoformat("2026-07-24T15:30:00+00:00")
""",
    ),
    Mutation(
        name="local BUY guard remains PreflightBlocked",
        relative_path="app/strategy.py",
        original='next_cycle.buy_status = "PreflightBlocked"',
        replacement='next_cycle.buy_status = "SubmitFailed"',
        probe="""
from app.models import StrategySettings
from app.strategy import StrategyEngine
cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "", 100.0, 0.0)
blocked = StrategyEngine.rollback_preflight_blocked_order(cycle, "BUY", "local guard")
assert blocked.buy_status == "PreflightBlocked"
assert blocked.buy_order_ref is None
""",
    ),
    Mutation(
        name="first throttled warning logs at monotonic time zero",
        relative_path="app/controller.py",
        original="last = self._last_price_warning_at.get(key)",
        replacement="last = self._last_price_warning_at.get(key, 0.0)",
        probe="""
import os
from types import SimpleNamespace
os.environ["IBKR_BOT_HEADLESS_SIGNALS"] = "1"
import app.controller as controller_module
controller = object.__new__(controller_module.TradingController)
controller._last_price_warning_at = {}
events = []
controller._log = lambda level, message, cycle: events.append((level, message))
controller_module.time.monotonic = lambda: 0.0
cycle = SimpleNamespace(ticker="AAPL", stage=SimpleNamespace(value="1_WAIT_INITIAL_DROP"))
controller._log_price_warning_throttled(
    cycle,
    "blocked",
    interval_seconds=60.0,
    throttle_key="stable-key",
)
assert events == [("WARN", "blocked")]
""",
    ),
)


def _replace_occurrence(text: str, original: str, replacement: str, occurrence: int) -> str:
    positions: list[int] = []
    offset = 0
    while True:
        index = text.find(original, offset)
        if index < 0:
            break
        positions.append(index)
        offset = index + len(original)
    if len(positions) < occurrence:
        raise ValueError(
            f"Expected occurrence {occurrence} of mutation target, found {len(positions)}."
        )
    index = positions[occurrence - 1]
    return text[:index] + replacement + text[index + len(original) :]


def _run_probe(root: Path, probe: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", probe],
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def run_mutation(root: Path, mutation: Mutation) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="ibkr_mutation_") as folder:
        work = Path(folder)
        shutil.copytree(root / "app", work / "app")

        baseline = _run_probe(work, mutation.probe)
        if baseline.returncode != 0:
            details = baseline.stderr.strip() or baseline.stdout.strip()
            return False, f"baseline probe failed: {details}"

        target = work / mutation.relative_path
        text = target.read_text(encoding="utf-8")
        try:
            mutated = _replace_occurrence(
                text,
                mutation.original,
                mutation.replacement,
                mutation.occurrence,
            )
        except ValueError as exc:
            return False, str(exc)
        target.write_text(mutated, encoding="utf-8")

        result = _run_probe(work, mutation.probe)
        if result.returncode == 0:
            return False, "mutant survived its contract probe"
        return True, "killed"


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for mutation in MUTATIONS:
        passed, detail = run_mutation(root, mutation)
        marker = "PASS" if passed else "FAIL"
        print(f"[{marker}] {mutation.name}: {detail}")
        if not passed:
            failures.append(mutation.name)
    if failures:
        print(f"MUTATION SMOKE FAILED: {len(failures)} mutant(s) survived or could not run.")
        return 1
    print(f"MUTATION SMOKE PASSED: {len(MUTATIONS)}/{len(MUTATIONS)} safety mutants killed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
