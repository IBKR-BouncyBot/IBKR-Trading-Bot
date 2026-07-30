"""v3.6.0 price-monitor and Cycle Audit layout regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.qt_stubs import imported_gui_with_stubs


@pytest.fixture(scope="module")
def gui_module():
    with imported_gui_with_stubs(Path.cwd()) as module:
        yield module


def test_price_monitor_formats_full_contract_identity(gui_module) -> None:
    ticker, detail = gui_module.PricePanel._instrument_identity(
        {
            "ticker": "IREN",
            "exchange": "SMART",
            "primary_exchange": "NASDAQ",
            "currency": "USD",
            "con_id": 526906130,
        },
        {
            "price": 42.51,
            "contract": {
                "ticker": "IREN",
                "description": "IREN LTD",
                "sec_type": "STK",
                "exchange": "SMART",
                "primary_exchange": "NASDAQ",
                "currency": "USD",
                "con_id": 526906130,
                "local_symbol": "IREN",
                "trading_class": "NMS",
                "industry": "Financial",
                "category": "Investment Companies",
                "subcategory": "Investment Companies",
            },
        },
    )

    assert ticker == "IREN"
    assert detail.startswith("IREN LTD")
    assert "STK / SMART / primary NASDAQ / USD / conId 526906130" in detail
    assert "Financial / Investment Companies" in detail
    assert detail.count("Investment Companies") == 1


def test_price_monitor_updates_ticker_info_and_price_labels(gui_module) -> None:
    panel = gui_module.PricePanel()
    panel.update_data(
        {"ticker": "SAP", "currency": "EUR", "primary_exchange": "IBIS"},
        {
            "price": 185.42,
            "status": "OK",
            "source": "last",
            "fields": {"bid": 185.40, "ask": 185.44, "last": 185.42},
            "contract": {
                "ticker": "SAP",
                "description": "SAP SE",
                "sec_type": "STK",
                "exchange": "SMART",
                "primary_exchange": "IBIS",
                "currency": "EUR",
                "con_id": 123456,
            },
        },
    )

    assert panel.big_ticker.text() == "SAP"
    assert panel.big_price.text().endswith("185.4200")
    assert "SAP SE" in panel.instrument_info.text()
    assert "primary IBIS" in panel.instrument_info.text()
    assert "EUR" in panel.instrument_info.text()


def test_price_monitor_has_matching_large_ticker_and_price_styles() -> None:
    source = Path("app/gui.py").read_text(encoding="utf-8")
    panel = source[source.index("class PricePanel") : source.index("class StopDialog")]

    assert 'self.big_ticker.setObjectName("BigPrice")' in panel
    assert 'self.big_price.setObjectName("BigPrice")' in panel
    assert panel.index("self.big_ticker = QLabel") < panel.index("self.big_price = QLabel")
    assert panel.index("self.instrument_info = QLabel") < panel.index("self.big_price = QLabel")
    assert 'self.instrument_info.setObjectName("PriceInstrumentInfo")' in panel
    assert "QLabel#PriceInstrumentInfo" in source


def test_timeline_tables_keep_bounded_columns_and_stretch_message_space() -> None:
    source = Path("app/gui.py").read_text(encoding="utf-8")
    timeline = source[
        source.index("def _timeline_tab(") : source.index(
            "def _scrollable_tab(", source.index("def _timeline_tab(")
        )
    ]
    records = source[
        source.index("def _records_table(") : source.index(
            "def _money(", source.index("def _records_table(")
        )
    ]

    assert "stretch_last=False" in records
    assert "column_minimums=column_minimums" in records
    assert "column_maximums=column_maximums" in records
    assert "item.setToolTip(str(record.get(\"raw_json\") or display_value))" in records
    assert "column_minimums=(90, 70, 70, 80, 70, 120)" in timeline
    assert "column_maximums=(110, 90, 90, 110, 90, 165)" in timeline
    assert "column_minimums=(90, 95, 70, 120)" in timeline
    assert "column_maximums=(110, 125, 90, 180)" in timeline
    assert "transition_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)" in timeline
    assert "risk_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)" in timeline
    assert "_fit_table_width_to_columns(transition_table" not in timeline
    assert "_fit_table_width_to_columns(risk_table" not in timeline


def test_timeline_tables_fill_the_available_horizontal_layout() -> None:
    source = Path("app/gui.py").read_text(encoding="utf-8")
    timeline = source[
        source.index("def _timeline_tab(") : source.index(
            "def _scrollable_tab(", source.index("def _timeline_tab(")
        )
    ]

    assert "split.addWidget(transition_table, 3)" in timeline
    assert "split.addWidget(risk_table, 2)" in timeline
    assert "split.addStretch(1)" not in timeline
    assert timeline.count("expanding=True,") >= 2


def test_market_capture_tab_uses_internal_scrollbars_without_outer_scroll_area() -> None:
    source = Path("app/gui.py").read_text(encoding="utf-8")
    start = source.index("def _market_capture_tab(")
    end = source.index("def _records_table(", start)
    section = source[start:end]

    assert "max_visible_rows=8" in section
    assert "expand_when_overflow=False" in section
    assert "summary_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in section
    assert "preview_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in section
    assert "file_box.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)" in section
    assert "file_box.setMaximumHeight(140)" in section
    assert "return tab" in section
    assert "return cls._scrollable_tab(tab)" not in section


def test_orders_executions_and_decisions_use_top_aligned_table_tabs() -> None:
    source = Path("app/gui.py").read_text(encoding="utf-8")
    audit = source[source.index("class CycleAuditDialog") : source.index("class MainWindow")]

    assert audit.count("return self._top_aligned_table_tab(table)") == 3
    helper = audit[
        audit.index("def _top_aligned_table_tab") : audit.index(
            "def _enriched_details", audit.index("def _top_aligned_table_tab")
        )
    ]
    assert "layout.addWidget(table, 0, Qt.AlignTop)" in helper
    assert "layout.addStretch(1)" in helper


def test_audit_record_builders_construct_top_aligned_tabs(gui_module) -> None:
    audit = object.__new__(gui_module.CycleAuditDialog)
    audit.details = {
        "orders": [
            {
                "created_at": "2026-07-25T10:00:00+00:00",
                "action": "BUY",
                "order_type": "TRAIL",
                "quantity": 10,
                "status": "Filled",
            }
        ],
        "executions": [
            {
                "executed_at": "2026-07-25T10:01:00+00:00",
                "side": "BOT",
                "shares": 10,
                "price": 100.0,
            }
        ],
        "decision_events": [
            {
                "created_at": "2026-07-25T10:00:00+00:00",
                "event_type": "ORDER_SUBMITTED",
                "message": "Submitted BUY trail.",
            }
        ],
    }

    assert audit._build_orders_tab() is not None
    assert audit._build_executions_tab() is not None
    assert audit._build_decision_events_tab() is not None
