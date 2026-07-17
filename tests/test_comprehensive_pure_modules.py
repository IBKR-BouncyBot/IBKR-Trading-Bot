"""Deterministic contract, boundary, and failure-path tests for pure modules.

The tests in this module have no live IBKR, network, GUI, or wall-clock
requirements.  They complement scenario tests by directly specifying the
public and internal helper contracts that other modules rely on.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import ib_platform, paths
from app import models as models_module
from app.ib_platform import (
    GATEWAY_PLATFORM,
    TWS_PLATFORM,
    ConnectionProfile,
    PlatformLaunchResult,
    SocketProbeResult,
)
from app.lockfile import SingleInstanceLock
from app.market_data_capture import _json_default
from app.models import (
    PROFIT_GUARD_EPSILON_PCT,
    AppSnapshot,
    ConnectionSettings,
    Stage,
    StrategySettings,
    floor_pct,
    max_sell_trailing_stop_pct_for_projected_anchor_guard,
    max_sell_trailing_stop_pct_for_rise_trigger,
    min_rise_trigger_pct_for_projected_anchor_guard,
    min_rise_trigger_pct_for_sell_trail,
    profit_trigger_price_for_sell_trail,
    projected_anchor_stop_factor,
    projected_buy_factor_from_anchor,
    protected_anchor_profit_pct,
    protected_gross_profit_pct,
)
from app.strategy import StrategyEngine
from app.timeline_scaling import (
    _median,
    _path_prices_near_markers,
    downsample_timeline_points,
    evenly_spaced_positions,
    marker_centered_price_window,
    timeline_item_position,
    timeline_path_position,
    timeline_path_time_window,
)


@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    [
        (1.239, 2, 1.23),
        (-1.231, 2, -1.24),
        (3.9, 0, 3.0),
    ],
)
def test_floor_pct_rounds_toward_negative_infinity(value: float, decimals: int, expected: float) -> None:
    assert floor_pct(value, decimals) == pytest.approx(expected)


def test_profit_projection_helpers_are_algebraically_consistent() -> None:
    buy_factor = projected_buy_factor_from_anchor(2.0, 1.0)
    stop_factor = projected_anchor_stop_factor(2.0, 1.0, 3.0, 99.0)

    # The compatibility ``sell_trailing_stop_pct`` input intentionally does not
    # change the protected initial stop; it changes only the activation price.
    assert buy_factor == pytest.approx(0.98 * 1.01)
    assert stop_factor == pytest.approx(buy_factor * 1.03)
    assert protected_anchor_profit_pct(2.0, 1.0, 3.0, 99.0) == pytest.approx((stop_factor - 1.0) * 100.0)
    assert protected_gross_profit_pct(3.0, 1.0) == pytest.approx(3.0)


def test_profit_trigger_handles_invalid_prices_and_slippage() -> None:
    assert profit_trigger_price_for_sell_trail(None, None, 3.0, 1.0) == 0.0
    assert profit_trigger_price_for_sell_trail(-1, None, 3.0, 1.0) == 0.0
    plain = profit_trigger_price_for_sell_trail(100.0, 999.0, 3.0, 1.0)
    buffered = profit_trigger_price_for_sell_trail(100.0, 999.0, 3.0, 1.0, True, 0.5)
    assert plain == pytest.approx(103.0 / 0.99)
    assert buffered > plain


def test_compatibility_profit_guard_bounds_are_stable_constants() -> None:
    assert min_rise_trigger_pct_for_sell_trail(95.0) == PROFIT_GUARD_EPSILON_PCT
    assert max_sell_trailing_stop_pct_for_rise_trigger(0.01) == 99.99
    assert min_rise_trigger_pct_for_projected_anchor_guard(99, 99, 99) == PROFIT_GUARD_EPSILON_PCT
    assert max_sell_trailing_stop_pct_for_projected_anchor_guard(99, 99, 99) == 99.99


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        (GATEWAY_PLATFORM, GATEWAY_PLATFORM),
        (TWS_PLATFORM.upper(), TWS_PLATFORM),
        ("unsupported", TWS_PLATFORM),
        ("", TWS_PLATFORM),
    ],
)
def test_connection_settings_normalized_platform(platform: str, expected: str) -> None:
    assert ConnectionSettings(platform=platform).normalized_platform() == expected


def test_app_snapshot_json_serializes_dataclasses_enums_and_custom_fallbacks() -> None:
    snapshot = AppSnapshot(
        connected=True,
        status="Ready",
        connection=ConnectionSettings(account=""),
        strategy=StrategySettings(ticker="AAPL"),
    )
    # AppSnapshot is a slotted dataclass without ``to_dict``; the current public
    # contract therefore serializes the root through the final string fallback.
    payload = json.loads(snapshot.to_json())
    assert isinstance(payload, str)
    assert "AppSnapshot" in payload and "Ready" in payload

    # Capture the local serializer callback and exercise every supported branch
    # without changing the production implementation.
    class Marker(Enum):
        VALUE = "marker"

    class WithToDict:
        def to_dict(self):
            return {"converted": True}

    class WithDict:
        def __init__(self) -> None:
            self.raw = 7

    converted: list[object] = []

    def capture_default(obj, *, default):
        converted.extend(
            [
                default(Marker.VALUE),
                default(WithToDict()),
                default(WithDict()),
                default(object()),
            ]
        )
        return "captured"

    original_dumps = models_module.json.dumps
    models_module.json.dumps = capture_default  # type: ignore[assignment]
    try:
        assert snapshot.to_json() == "captured"
    finally:
        models_module.json.dumps = original_dumps
    assert converted[0] == "marker"
    assert converted[1] == {"converted": True}
    assert converted[2] == {"raw": 7}
    assert isinstance(converted[3], str)


def test_strategy_mark_error_sets_terminal_state_and_message() -> None:
    cycle = StrategyEngine.start_cycle(StrategySettings(ticker="AAPL"), 1, "SIM", 100.0, 0.0)
    before = cycle.updated_at
    failed = StrategyEngine.mark_error(cycle, "broker rejected order")
    assert failed is not cycle
    assert cycle.stage is Stage.WAIT_INITIAL_DROP
    assert failed.stage is Stage.ERROR
    assert failed.error_message == "broker rejected order"
    assert failed.updated_at >= before


def test_path_helpers_are_root_relative_and_create_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "app_dir", lambda: tmp_path)
    assert paths.database_path() == tmp_path / "bot_state.sqlite"
    assert paths.lock_file_path() == tmp_path / "ibkr_trading_bot.lock"
    for helper, name in (
        (paths.logs_dir, "logs"),
        (paths.exports_dir, "exports"),
        (paths.backups_dir, "backups"),
        (paths.debug_captures_dir, "debug_captures"),
    ):
        result = helper()
        assert result == tmp_path / name
        assert result.is_dir()


def test_single_instance_lock_context_manager_releases_on_exception(tmp_path: Path) -> None:
    lock_path = tmp_path / "instance.lock"
    with pytest.raises(RuntimeError, match="test failure"):
        with SingleInstanceLock(path=lock_path) as lock:
            assert lock.fd is not None
            assert lock_path.read_text(encoding="ascii")
            raise RuntimeError("test failure")
    assert not lock_path.exists()
    assert lock.fd is None


def test_connection_profile_serialization_and_label_lookup() -> None:
    profile = ConnectionProfile("x", "Example", GATEWAY_PLATFORM, "paper", "localhost", 4002)
    assert profile.to_dict() == {
        "key": "x",
        "label": "Example",
        "platform": GATEWAY_PLATFORM,
        "trading_mode": "paper",
        "host": "localhost",
        "port": 4002,
    }
    assert ib_platform.profile_label_for("gateway_live").startswith("IB Gateway Live")
    assert ib_platform.profile_label_for("missing") == "Custom"


def test_existing_file_expands_and_rejects_non_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "gateway.exe"
    executable.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("TEST_IBKR_HOME", str(tmp_path))
    assert ib_platform._existing_file(r"%TEST_IBKR_HOME%/gateway.exe") in {None, str(executable)}
    # POSIX does not expand percent-delimited variables; direct and quoted paths
    # are still part of the cross-platform contract.
    assert ib_platform._existing_file(f'"{executable}"') == str(executable)
    assert ib_platform._existing_file(str(tmp_path)) is None
    assert ib_platform._existing_file("") is None


def test_find_platform_executable_prefers_configured_path(tmp_path: Path) -> None:
    executable = tmp_path / "custom.exe"
    executable.write_bytes(b"x")
    assert ib_platform.find_platform_executable(GATEWAY_PLATFORM, str(executable)) == str(executable)


def test_find_gateway_executable_selects_newest_windows_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = tmp_path / "old.exe"
    new = tmp_path / "new.exe"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    monkeypatch.setattr(ib_platform.sys, "platform", "win32")
    monkeypatch.setattr(ib_platform.glob, "glob", lambda pattern: [str(old), str(new)])
    assert ib_platform.find_platform_executable(GATEWAY_PLATFORM) == str(new)


def test_find_tws_executable_checks_common_windows_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ib_platform.sys, "platform", "win32")
    monkeypatch.setattr(ib_platform, "_existing_file", lambda path: "C:/Jts/tws.exe" if path == ib_platform._COMMON_TWS_PATHS[-1] else None)
    assert ib_platform.find_platform_executable(TWS_PLATFORM) == "C:/Jts/tws.exe"


def test_find_platform_executable_returns_none_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ib_platform.sys, "platform", "linux")
    assert ib_platform.find_platform_executable(GATEWAY_PLATFORM) is None


def test_launch_platform_reports_missing_failure_and_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ib_platform, "find_platform_executable", lambda *args: None)
    missing = ib_platform.launch_platform(GATEWAY_PLATFORM)
    assert isinstance(missing, PlatformLaunchResult)
    assert missing.started is False
    assert "Could not find" in missing.message

    executable = tmp_path / "gateway.exe"
    executable.write_bytes(b"x")
    monkeypatch.setattr(ib_platform, "find_platform_executable", lambda *args: str(executable))
    monkeypatch.setattr(ib_platform.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("denied")))
    failed = ib_platform.launch_platform(GATEWAY_PLATFORM)
    assert failed.started is False
    assert "denied" in failed.message

    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(ib_platform.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace())
    started = ib_platform.launch_platform(GATEWAY_PLATFORM)
    assert started.started is True
    assert started.executable == str(executable)
    assert calls[0][0] == [str(executable)]


def test_probe_socket_closes_successful_connection_and_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    entered: list[bool] = []

    class Connection:
        def __enter__(self):
            entered.append(True)
            return self

        def __exit__(self, exc_type, exc, tb):
            entered.append(False)

    monkeypatch.setattr(ib_platform.socket, "create_connection", lambda *args, **kwargs: Connection())
    assert ib_platform.probe_socket("127.0.0.1", 4001) == SocketProbeResult(True, "")
    assert entered == [True, False]

    monkeypatch.setattr(ib_platform.socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("refused")))
    result = ib_platform.probe_socket("127.0.0.1", 4001)
    assert result.reachable is False
    assert "refused" in result.error


def test_connection_helper_text_distinguishes_socket_and_handshake_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ib_platform, "probe_socket", lambda *args, **kwargs: SocketProbeResult(False, "refused"))
    unreachable = ib_platform.connection_helper_text(GATEWAY_PLATFORM, "host", 4001, "connect error")
    assert "not reachable" in unreachable
    assert "connect error" in unreachable

    monkeypatch.setattr(ib_platform, "probe_socket", lambda *args, **kwargs: SocketProbeResult(True, ""))
    handshake = ib_platform.connection_helper_text(TWS_PLATFORM, "host", 7497)
    assert "socket is reachable" in handshake
    assert "Client ID" in handshake


@dataclass
class JsonDataclass:
    value: int


class ValueObject:
    value = "enum-like"


class BrokenString:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


def test_market_capture_json_default_covers_supported_and_fallback_types() -> None:
    assert _json_default(JsonDataclass(4)) == {"value": 4}
    assert _json_default(ValueObject()) == "enum-like"
    assert _json_default({3, 1, 2}) == [1, 2, 3]
    assert _json_default(Path("x")) == "x"
    assert _json_default(BrokenString()) == "<unserializable>"


def test_timeline_median_and_marker_window_handle_outliers_and_degenerate_input() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _median([])
    assert _median([3.0, 1.0, 2.0]) == 2.0
    assert _median([1.0, 3.0]) == 2.0
    assert marker_centered_price_window([]) is None
    low, high = marker_centered_price_window([100.0, 100.0, 100.0]) or (0.0, 0.0)
    assert low < 100.0 < high
    low, high = marker_centered_price_window([95.0, 100.0, 105.0, 10_000.0]) or (0.0, 0.0)
    assert low < 95.0 < 105.0 < high
    assert high < 10_000.0


def test_timeline_positions_prefer_real_time_then_semantic_fallback() -> None:
    points = [{"time": 10.0}, {"time": 15.0}, {"time": 20.0}]
    assert timeline_path_time_window(points) == (10.0, 20.0)
    assert timeline_path_time_window([{"time": 10.0}]) is None
    assert timeline_path_time_window([{"time": 10.0}, {"time": 10.0}]) is None

    assert timeline_path_position(0, 3, start=0.1, end=0.9) == pytest.approx(0.1)
    assert timeline_path_position(1, 3, start=0.1, end=0.9) == pytest.approx(0.5)
    assert timeline_path_position(2, 3, start=0.9, end=0.1) == pytest.approx(0.9)
    assert timeline_path_position(0, 1, start=0.2, end=0.8) == pytest.approx(0.5)

    assert timeline_item_position({"time": 15.0}, (10.0, 20.0), start=0.1, end=0.9) == pytest.approx(0.5)
    assert timeline_item_position({"time": 99.0, "position_hint": 0.75}, (10.0, 20.0)) == pytest.approx(0.75)
    assert timeline_item_position({"position": 0.25}, None) == pytest.approx(0.25)
    assert timeline_item_position({}, None, fallback_position=1.5) == 1.0


def test_evenly_spaced_and_downsampled_timeline_points_preserve_boundaries() -> None:
    assert evenly_spaced_positions(0) == []
    assert evenly_spaced_positions(1, start=0.2, end=0.8) == [0.5]
    assert evenly_spaced_positions(3, start=0.2, end=0.8) == pytest.approx([0.2, 0.5, 0.8])
    assert evenly_spaced_positions(3, start=0.8, end=0.2) == pytest.approx([0.2, 0.5, 0.8])

    points = [{"time": float(i), "price": 100.0 + math.sin(i / 5.0)} for i in range(1_000)]
    sampled = downsample_timeline_points(points, max_points=50)
    assert len(sampled) <= 50
    assert sampled[0] == points[0]
    assert sampled[-1] == points[-1]
    assert [row["time"] for row in sampled] == sorted(row["time"] for row in sampled)
    assert downsample_timeline_points(points[:4], max_points=50) == points[:4]



def test_path_prices_near_markers_keeps_marker_corridor_and_handles_empty_inputs() -> None:
    assert _path_prices_near_markers([100.0, 101.0, 500.0], [100.0, 101.0]) == [100.0, 101.0]
    assert _path_prices_near_markers([100.0, 500.0], []) == [100.0, 500.0]
    assert _path_prices_near_markers([], [100.0]) == []
