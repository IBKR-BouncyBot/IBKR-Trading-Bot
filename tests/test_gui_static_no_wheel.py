from pathlib import Path


def test_no_wheel_filter_is_installed_for_spin_and_combo_fields():
    text = Path("app/gui.py").read_text(encoding="utf-8")
    assert "class NoWheelEditFilter" in text
    assert "QEvent.Wheel" in text
    assert "QAbstractSpinBox" in text
    assert "QComboBox" in text
    assert "installEventFilter(self._no_wheel_edit_filter)" in text


def test_strategy_graph_has_time_price_hover_tooltip():
    text = Path("app/gui.py").read_text(encoding="utf-8")
    assert "def mouseMoveEvent" in text
    assert "QToolTip.showText" in text
    assert "Price:" in text
    assert "datetime.fromtimestamp" in text
    assert "self._hover_point" in text
    assert "Qt.DashLine" in text


def test_atr_minimum_profit_selective_controls_exist():
    text = Path("app/gui.py").read_text(encoding="utf-8")
    assert "Adapt Minimum profit % with ATR" in text
    assert "atr_adapt_minimum_profit_enabled=self.atr_min_profit_adaptive_check.isChecked()" in text
    assert "Minimum profit remains manually set" in text


def test_strategy_graph_has_hover_price_time_marker():
    text = Path("app/gui.py").read_text(encoding="utf-8")
    assert "self._hover_point" in text
    assert "QToolTip.showText" in text
    assert "drawLine(QPointF(hover_x" in text
    assert "datetime.fromtimestamp" in text
