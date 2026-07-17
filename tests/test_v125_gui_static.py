from pathlib import Path

GUI = Path("app/gui.py").read_text(encoding="utf-8")


def test_numbered_primary_action_buttons_and_ticker_enter_binding():
    assert 'QPushButton("1. Connect to IB Gateway API")' in GUI
    assert 'QPushButton("Search for ticker")' in GUI
    assert 'QPushButton("2. Use selected match")' in GUI
    assert 'QPushButton("3. Confirm ticker + get first price")' in GUI
    assert '("start", "4. Start strategy", self._start_clicked)' in GUI
    assert 'ticker_edit.returnPressed.connect(self._search_ticker_clicked)' in GUI
    assert "Search API Matches" not in GUI


def test_connection_path_placeholder_and_tab_order_contract():
    assert 'Optional path to {short_label}' in GUI
    assert 'self.tabs.addTab(self.history_tab, "Trade history")' in GUI
    assert 'self.tabs.addTab(self.recovery_tab, "Reconciliation")' in GUI
    assert 'self.recovery_tabs.addTab(self.recovery_tab, "Reconciliation")' not in GUI


def test_price_monitor_table_contract_exposes_scrollbars_when_raw_fields_overflow():
    assert 'self.fields_table = QTableWidget(0, 10)' in GUI
    assert '_polish_table_widget(self.fields_table, stretch_last=False, expanding=True)' in GUI
    assert 'self.fields_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)' in GUI
    assert 'self.fields_table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)' in GUI
    assert 'QSizePolicy.Expanding, QSizePolicy.Fixed' in GUI


def test_flowchart_history_selector_uses_enriched_history_columns():
    assert 'configured_initial_drop_pct' in GUI
    assert 'configured_buy_rebound_pct' in GUI
    assert 'configured_min_profit_pct' in GUI
    assert 'configured_sell_trail_pct' in GUI
    assert 'configured_protective_sell_trail_pct' in GUI
    assert 'configured_slippage_buffer_pct' in GUI
    assert 'historical_cycle.setdefault("stage", Stage.CYCLE_COMPLETE.value)' in GUI
