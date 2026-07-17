"""Deterministic PySide6 test doubles used by headless GUI contract tests.

The doubles implement only Qt behavior that the tests assert. Unknown rendering
and layout calls are benign no-ops, which lets the suite exercise GUI decision
logic without requiring a window server or changing production code.
"""

from __future__ import annotations

import importlib
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class SignalStub:
    """Small Qt-like signal with observable connections and emissions."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.callbacks: list[Any] = []
        self.emissions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def connect(self, callback: Any) -> None:
        self.callbacks.append(callback)

    def emit(self, *args: Any, **kwargs: Any) -> None:
        self.emissions.append((args, kwargs))
        for callback in list(self.callbacks):
            callback(*args, **kwargs)

    def __iadd__(self, callback: Any) -> SignalStub:
        self.connect(callback)
        return self

    def __isub__(self, callback: Any) -> SignalStub:
        if callback in self.callbacks:
            self.callbacks.remove(callback)
        return self


class QtFlag:
    """Value-like and callable stand-in for Qt enum members and class statics."""

    def __init__(self, value: int = 0) -> None:
        self.value = int(value)

    def __call__(self, *args: Any, **kwargs: Any) -> Dummy:
        return Dummy(*args, **kwargs)

    def __or__(self, other: Any) -> QtFlag:
        return QtFlag(self.value | int(other or 0))

    def __and__(self, other: Any) -> QtFlag:
        return QtFlag(self.value & int(other or 0))

    def __invert__(self) -> QtFlag:
        return QtFlag(~self.value)

    def __bool__(self) -> bool:
        return bool(self.value)

    def __int__(self) -> int:
        return self.value

    def __index__(self) -> int:
        return self.value

    def __eq__(self, other: object) -> bool:
        try:
            return self.value == int(other)  # type: ignore[arg-type]
        except Exception:
            return False

    def __hash__(self) -> int:
        return hash(self.value)


class QtNamespace:
    """Lazy namespace for Qt constants used by the production GUI."""

    class AlignmentFlag:
        pass

    class PenStyle:
        pass

    class BrushStyle:
        pass

    def __init__(self) -> None:
        self._flags: dict[str, QtFlag] = {}

    def __getattr__(self, name: str) -> QtFlag:
        flag = self._flags.setdefault(name, QtFlag())
        return flag


class DummyMeta(type):
    def __getattr__(cls, name: str) -> Any:
        if name == "singleShot":
            return lambda delay, callback: None
        if name in {"getSaveFileName", "getOpenFileName"}:
            return lambda *args, **kwargs: ("", "")
        if name in {"information", "warning", "critical"}:
            return lambda *args, **kwargs: QtFlag()
        if name == "question":
            return lambda *args, **kwargs: getattr(cls, "Yes")
        if name == "instance":
            return lambda: Dummy()
        return QtFlag()


_SIGNAL_NAMES = {
    "accepted",
    "cellClicked",
    "cellDoubleClicked",
    "clicked",
    "currentChanged",
    "currentIndexChanged",
    "currentTextChanged",
    "customContextMenuRequested",
    "history_updated",
    "itemSelectionChanged",
    "rejected",
    "returnPressed",
    "snapshot_updated",
    "stateChanged",
    "textChanged",
    "textEdited",
    "ticker_search_updated",
    "timeout",
    "toggled",
    "triggered",
    "valueChanged",
}

_NUMERIC_METHODS = {
    "bottom",
    "columnCount",
    "count",
    "currentIndex",
    "height",
    "left",
    "maximum",
    "minimum",
    "pointSize",
    "right",
    "rowCount",
    "top",
    "width",
    "x",
    "y",
}

_TEXT_METHODS = {"currentText", "objectName", "statusTip", "text", "toolTip"}
_BOOL_METHODS = {"hasFocus", "isActiveWindow", "isChecked", "isEnabled", "isVisible"}
_NONE_METHODS = {"currentData", "data", "itemData", "property"}
_LIST_METHODS = {"findChildren", "selectedIndexes", "selectedItems"}


class Dummy(metaclass=DummyMeta):
    """Stateful generic widget/layout/painter stand-in."""

    _missing_hasattr_names: set[str] = set()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del kwargs
        self._text = str(args[0]) if args and isinstance(args[0], str) else ""
        self._value = 0.0
        self._checked = False
        self._enabled = True
        self._visible = True
        self._current_index = 0
        self._items: list[tuple[str, Any]] = []
        self._row_count = 0
        self._column_count = 0
        self._table_items: dict[tuple[int, int], Any] = {}
        self._properties: dict[str, Any] = {}
        self._data: dict[Any, Any] = {}
        self._children: list[Any] = []
        self._point_size = 10
        self._width = 800
        self._height = 400
        self._result = 0

    def __getattr__(self, name: str) -> Any:
        if name in self._missing_hasattr_names:
            raise AttributeError(name)
        if name in _SIGNAL_NAMES:
            signal = SignalStub()
            setattr(self, name, signal)
            return signal
        if name in _NUMERIC_METHODS:
            return lambda *args, **kwargs: 0
        if name in _TEXT_METHODS:
            return lambda *args, **kwargs: ""
        if name == "value":
            return lambda *args, **kwargs: self._value
        if name in _NONE_METHODS:
            return lambda *args, **kwargs: None
        if name in _BOOL_METHODS:
            return lambda *args, **kwargs: False
        if name in _LIST_METHODS:
            return lambda *args, **kwargs: []

        def method(*args: Any, **kwargs: Any) -> Dummy:
            del args, kwargs
            return Dummy()

        setattr(self, name, method)
        return method

    def __call__(self, *args: Any, **kwargs: Any) -> Dummy:
        return Dummy(*args, **kwargs)

    def __iter__(self) -> Iterator[Any]:
        return iter(())

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int) and 0 <= key < len(self._items):
            return self._items[key]
        return Dummy()

    def __bool__(self) -> bool:
        return False

    def __or__(self, other: Any) -> Dummy:
        del other
        return self

    def __and__(self, other: Any) -> Dummy:
        del other
        return self

    def __add__(self, other: Any) -> float:
        return float(self) + float(other)

    def __radd__(self, other: Any) -> float:
        return float(other) + float(self)

    def __sub__(self, other: Any) -> float:
        return float(self) - float(other)

    def __rsub__(self, other: Any) -> float:
        return float(other) - float(self)

    def __mul__(self, other: Any) -> float:
        return float(self) * float(other)

    def __rmul__(self, other: Any) -> float:
        return float(other) * float(self)

    def __truediv__(self, other: Any) -> float:
        return float(self) / max(float(other), 1e-12)

    def __rtruediv__(self, other: Any) -> float:
        return float(other) / max(float(self), 1e-12)

    def __lt__(self, other: Any) -> bool:
        return float(self) < float(other)

    def __le__(self, other: Any) -> bool:
        return float(self) <= float(other)

    def __gt__(self, other: Any) -> bool:
        return float(self) > float(other)

    def __ge__(self, other: Any) -> bool:
        return float(self) >= float(other)

    def __float__(self) -> float:
        return float(self._value)

    def __int__(self) -> int:
        return int(self._value)

    def eventFilter(self, watched: Any, event: Any) -> bool:
        del watched, event
        return False

    def resizeEvent(self, event: Any) -> None:
        del event

    def paintEvent(self, event: Any) -> None:
        del event

    def mousePressEvent(self, event: Any) -> None:
        del event

    def mouseMoveEvent(self, event: Any) -> None:
        del event

    def mouseReleaseEvent(self, event: Any) -> None:
        del event

    def leaveEvent(self, event: Any) -> None:
        del event

    def wheelEvent(self, event: Any) -> None:
        del event

    def setText(self, value: Any) -> None:
        self._text = str(value)

    def text(self) -> str:
        return self._text

    def setValue(self, value: Any) -> None:
        self._value = float(value)

    def value(self) -> float:
        return self._value

    def setChecked(self, checked: Any) -> None:
        self._checked = bool(checked)

    def isChecked(self) -> bool:
        return self._checked

    def setEnabled(self, enabled: Any) -> None:
        self._enabled = bool(enabled)

    def isEnabled(self) -> bool:
        return self._enabled

    def setVisible(self, visible: Any) -> None:
        self._visible = bool(visible)

    def isVisible(self) -> bool:
        return self._visible

    def setCurrentIndex(self, index: Any) -> None:
        self._current_index = int(index)

    def currentIndex(self) -> int:
        return self._current_index

    def addItem(self, text: Any, data: Any = None) -> None:
        self._items.append((str(text), data))

    def addItems(self, texts: Any) -> None:
        for text in texts:
            self.addItem(text, text)

    def clear(self) -> None:
        self._text = ""
        self._items.clear()
        self._table_items.clear()
        self._row_count = 0

    def count(self) -> int:
        return len(self._items)

    def currentText(self) -> str:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index][0]
        return self._text

    def currentData(self) -> Any:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index][1]
        return None

    def itemData(self, index: int) -> Any:
        if 0 <= index < len(self._items):
            return self._items[index][1]
        return None

    def itemText(self, index: int) -> str:
        if 0 <= index < len(self._items):
            return self._items[index][0]
        return ""

    def findText(self, text: str) -> int:
        for index, (label, _) in enumerate(self._items):
            if label == text:
                return index
        return -1

    def setCurrentText(self, text: str) -> None:
        index = self.findText(text)
        if index >= 0:
            self._current_index = index
        else:
            self._text = str(text)

    def setRowCount(self, count: Any) -> None:
        self._row_count = int(count)

    def rowCount(self) -> int:
        return self._row_count

    def setColumnCount(self, count: Any) -> None:
        self._column_count = int(count)

    def columnCount(self) -> int:
        return self._column_count

    def setItem(self, row: int, column: int, item: Any) -> None:
        self._table_items[(int(row), int(column))] = item

    def item(self, row: int, column: int) -> Any:
        return self._table_items.get((int(row), int(column)))

    def setData(self, role: Any, value: Any) -> None:
        self._data[role] = value

    def data(self, role: Any) -> Any:
        return self._data.get(role)

    def setProperty(self, name: str, value: Any) -> None:
        self._properties[str(name)] = value

    def property(self, name: str) -> Any:
        return self._properties.get(str(name))

    def setPointSize(self, value: Any) -> None:
        self._point_size = int(value)

    def pointSize(self) -> int:
        return self._point_size

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height

    def resize(self, width: Any, height: Any) -> None:
        self._width = int(width)
        self._height = int(height)

    def setResult(self, value: Any) -> None:
        self._result = int(value)

    def exec(self) -> int:
        return self._result


class PointStub(Dummy):
    def __init__(self, x: Any = 0.0, y: Any = 0.0) -> None:
        super().__init__()
        self._x = float(x)
        self._y = float(y)

    def x(self) -> float:
        return self._x

    def y(self) -> float:
        return self._y


class SizeStub(Dummy):
    def __init__(self, width: Any = 0, height: Any = 0) -> None:
        super().__init__()
        self._width = int(width)
        self._height = int(height)


class RectStub(Dummy):
    def __init__(self, *args: Any) -> None:
        super().__init__()
        if len(args) == 1 and hasattr(args[0], "left"):
            source = args[0]
            self._left = float(source.left())
            self._top = float(source.top())
            self._width = float(source.width())
            self._height = float(source.height())
        elif len(args) >= 4:
            self._left = float(args[0])
            self._top = float(args[1])
            self._width = float(args[2])
            self._height = float(args[3])
        else:
            self._left = 0.0
            self._top = 0.0
            self._width = 800.0
            self._height = 400.0

    def left(self) -> float:
        return self._left

    def right(self) -> float:
        return self._left + self._width

    def top(self) -> float:
        return self._top

    def bottom(self) -> float:
        return self._top + self._height

    def width(self) -> float:
        return self._width

    def height(self) -> float:
        return self._height

    def center(self) -> PointStub:
        return PointStub(self._left + self._width / 2.0, self._top + self._height / 2.0)

    def adjusted(self, left: Any, top: Any, right: Any, bottom: Any) -> RectStub:
        return RectStub(
            self._left + float(left),
            self._top + float(top),
            self._width + float(right) - float(left),
            self._height + float(bottom) - float(top),
        )

    def contains(self, point: Any) -> bool:
        try:
            return self.left() <= float(point.x()) <= self.right() and self.top() <= float(point.y()) <= self.bottom()
        except Exception:
            return False

    def moveCenter(self, point: Any) -> None:
        self._left = float(point.x()) - self._width / 2.0
        self._top = float(point.y()) - self._height / 2.0

    def translate(self, dx: Any, dy: Any) -> None:
        self._left += float(dx)
        self._top += float(dy)

    def setLeft(self, value: Any) -> None:
        right = self.right()
        self._left = float(value)
        self._width = right - self._left

    def setRight(self, value: Any) -> None:
        self._width = float(value) - self._left

    def setTop(self, value: Any) -> None:
        bottom = self.bottom()
        self._top = float(value)
        self._height = bottom - self._top

    def setBottom(self, value: Any) -> None:
        self._height = float(value) - self._top


class TableItemStub(Dummy):
    def __init__(self, text: Any = "") -> None:
        super().__init__(str(text))


class ApplicationStub(Dummy):
    _instance: ApplicationStub | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        ApplicationStub._instance = self
        self._clipboard = Dummy()

    @classmethod
    def instance(cls) -> ApplicationStub:
        return cls._instance or cls([])

    def clipboard(self) -> Dummy:
        return self._clipboard


class PaletteStub(Dummy):
    """Minimal QPalette replacement with stable role constants."""

    Window = QtFlag(1)
    WindowText = QtFlag(2)
    Base = QtFlag(3)
    AlternateBase = QtFlag(4)
    ToolTipBase = QtFlag(5)
    ToolTipText = QtFlag(6)
    Text = QtFlag(7)
    Button = QtFlag(8)
    ButtonText = QtFlag(9)
    BrightText = QtFlag(10)
    Highlight = QtFlag(11)
    HighlightedText = QtFlag(12)
    PlaceholderText = QtFlag(13)


class StyleFactoryStub:
    """Minimal QStyleFactory replacement used by the process entry point."""

    @staticmethod
    def create(name: str) -> str:
        return name


class TimerStub(Dummy):
    @staticmethod
    def singleShot(delay: Any, callback: Any) -> None:
        del delay, callback


class EventStub(Dummy):
    Wheel = QtFlag(1)
    FocusIn = QtFlag(2)

    def __init__(self, event_type: Any = None) -> None:
        super().__init__()
        self._event_type = event_type

    def type(self) -> Any:
        return self._event_type


class MessageBoxStub(Dummy):
    Yes = QtFlag(1)
    No = QtFlag(2)
    Cancel = QtFlag(4)
    Ok = QtFlag(8)

    @classmethod
    def question(cls, *args: Any, **kwargs: Any) -> QtFlag:
        del args, kwargs
        return cls.Yes


class FileDialogStub(Dummy):
    @staticmethod
    def getSaveFileName(*args: Any, **kwargs: Any) -> tuple[str, str]:
        del args, kwargs
        return "", ""

    @staticmethod
    def getOpenFileName(*args: Any, **kwargs: Any) -> tuple[str, str]:
        del args, kwargs
        return "", ""


def _hasattr_names_from_gui_source(root: Path) -> set[str]:
    """Return literal attributes queried through hasattr in app.gui."""
    import ast

    tree = ast.parse((root / "app" / "gui.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "hasattr":
            continue
        if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant) or not isinstance(node.args[1].value, str):
            continue
        names.add(node.args[1].value)
    return names


def install_qt_stubs(root: Path) -> dict[str, types.ModuleType]:
    """Install deterministic PySide6 modules in ``sys.modules``."""
    Dummy._missing_hasattr_names = _hasattr_names_from_gui_source(root)

    pyside = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")
    qtgui = types.ModuleType("PySide6.QtGui")
    qtwidgets = types.ModuleType("PySide6.QtWidgets")

    qtcore.QByteArray = bytes
    qtcore.QEvent = EventStub
    qtcore.QObject = Dummy
    qtcore.QPointF = PointStub
    qtcore.QRectF = RectStub
    qtcore.QSize = SizeStub
    qtcore.QTimer = TimerStub
    qtcore.Qt = QtNamespace()
    qtcore.Signal = lambda *args, **kwargs: SignalStub(*args, **kwargs)

    for name in ("QAction", "QBrush", "QColor", "QFont", "QPainter", "QPen"):
        setattr(qtgui, name, Dummy)
    qtgui.QPalette = PaletteStub

    widget_types: dict[str, Any] = {
        "QApplication": ApplicationStub,
        "QFileDialog": FileDialogStub,
        "QMessageBox": MessageBoxStub,
        "QStyleFactory": StyleFactoryStub,
        "QTableWidgetItem": TableItemStub,
    }
    for name in (
        "QAbstractSpinBox",
        "QCheckBox",
        "QComboBox",
        "QDialog",
        "QDialogButtonBox",
        "QDoubleSpinBox",
        "QFormLayout",
        "QFrame",
        "QGridLayout",
        "QGroupBox",
        "QHeaderView",
        "QHBoxLayout",
        "QLabel",
        "QLineEdit",
        "QMainWindow",
        "QProgressBar",
        "QPushButton",
        "QScrollArea",
        "QSizePolicy",
        "QSpinBox",
        "QTabWidget",
        "QTableWidget",
        "QTextEdit",
        "QToolTip",
        "QVBoxLayout",
        "QWidget",
    ):
        widget_types.setdefault(name, Dummy)
    for name, value in widget_types.items():
        setattr(qtwidgets, name, value)

    pyside.QtCore = qtcore
    pyside.QtGui = qtgui
    pyside.QtWidgets = qtwidgets
    modules = {
        "PySide6": pyside,
        "PySide6.QtCore": qtcore,
        "PySide6.QtGui": qtgui,
        "PySide6.QtWidgets": qtwidgets,
    }
    sys.modules.update(modules)
    return modules


@contextmanager
def imported_gui_with_stubs(root: Path | None = None) -> Iterator[Any]:
    """Import ``app.gui`` under stubs and restore prior modules afterward."""
    project_root = Path(root or Path.cwd()).resolve()
    module_names = ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "app.gui")
    saved = {name: sys.modules.get(name) for name in module_names}
    app_package = sys.modules.get("app")
    saved_gui_attribute = getattr(app_package, "gui", None) if app_package is not None else None
    had_gui_attribute = bool(app_package is not None and hasattr(app_package, "gui"))
    try:
        sys.modules.pop("app.gui", None)
        if app_package is not None and hasattr(app_package, "gui"):
            delattr(app_package, "gui")
        install_qt_stubs(project_root)
        yield importlib.import_module("app.gui")
    finally:
        sys.modules.pop("app.gui", None)
        if app_package is not None and hasattr(app_package, "gui"):
            delattr(app_package, "gui")
        for name in module_names:
            previous = saved[name]
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        if app_package is not None and had_gui_attribute:
            setattr(app_package, "gui", saved_gui_attribute)
