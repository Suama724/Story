"""参与者与组织者共用的人物属性表。"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from coc7_attributes import ATTRIBUTES, MAX_IMPORT_LENGTH, RESOURCES, SKILL_COLUMNS, skill_defaults


class AttributeImportDialog(QDialog):
    importRequested = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("导入属性")
        self.resize(620, 350)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("粘贴人物卡「简化卡 骰娘导入」中的 .st 属性行")
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.submit_button = QPushButton("导入")
        self.submit_button.setObjectName("primary")
        self.submit_button.clicked.connect(self._submit)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.submit_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.text, 1)
        layout.addWidget(self.feedback)
        layout.addLayout(buttons)
        self.text.setFocus()

    def _submit(self):
        text = self.text.toPlainText()
        if not text.strip() or len(text) > MAX_IMPORT_LENGTH:
            self.feedback.setText("请粘贴一条 .st 属性指令。")
            return
        self.set_pending(True)
        self.importRequested.emit(text)

    def set_pending(self, pending: bool, feedback: str = ""):
        self.submit_button.setEnabled(not pending)
        self.text.setReadOnly(pending)
        self.feedback.setText("正在导入…" if pending else feedback)


_LIGHT = "#deeaf6"
_BLUE = "#bed7ee"
_INK = "#203548"


def _table(rows: int, columns: int) -> QTableWidget:
    table = QTableWidget(rows, columns)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.verticalHeader().hide()
    table.horizontalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(26)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.setStyleSheet("QTableWidget { gridline-color: #a9bfd0; padding: 0; border-radius: 0; }")
    return table


def _cell(table: QTableWidget, row: int, column: int, value, fill: str = "#ffffff") -> None:
    item = QTableWidgetItem("—" if value is None else str(value))
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    item.setBackground(QColor(fill))
    item.setForeground(QColor(_INK))
    table.setItem(row, column, item)


class AttributeSheetDialog(QDialog):
    def __init__(self, parent, actor_name: str, values: dict[str, int], *, organizer: bool = False):
        super().__init__(parent)
        self.setWindowTitle(f"查看属性 · {actor_name}")
        self.resize(850, 660)
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("属性"))
        self.attributes_table = _table(6, 9)
        for index, (key, label) in enumerate(ATTRIBUTES):
            row = (index // 3) * 2
            column = (index % 3) * 3
            value = values.get(key)
            self.attributes_table.setSpan(row, column, 2, 1)
            self.attributes_table.setSpan(row, column + 1, 2, 1)
            _cell(self.attributes_table, row, column, f"{label}\n{key.upper()}", _LIGHT)
            _cell(self.attributes_table, row, column + 1, value, _BLUE)
            for offset, divisor, difficulty in ((0, 2, "困难"), (1, 5, "极难")):
                _cell(self.attributes_table, row + offset, column + 2,
                      None if value is None else f"{difficulty} {value // divisor}", _LIGHT)
        self.attributes_table.setFixedHeight(6 * 26 + 2)
        layout.addWidget(self.attributes_table)

        resources = _table(1, 6)
        for index, (key, label) in enumerate(RESOURCES):
            _cell(resources, 0, 2 * index, label, _LIGHT)
            _cell(resources, 0, 2 * index + 1, values[key], _BLUE)
        resources.setFixedHeight(28)
        layout.addWidget(resources)

        layout.addWidget(QLabel("技能表"))
        defaults = skill_defaults(values)
        core_keys = {key for key, _ in (*ATTRIBUTES, *RESOURCES)}
        extra = [name for name in values if name not in defaults and name not in core_keys]
        left, right = ([name for name, _ in column] for column in SKILL_COLUMNS)
        for name in extra:
            (left if len(left) <= len(right) else right).append(name)
        self.skills_table = _table(max(len(left), len(right)), 10)
        self.skills_table.setHorizontalHeaderLabels(["技能名称", "初始", "普通", "困难", "极难"] * 2)
        for column, names in ((0, left), (5, right)):
            for row, name in enumerate(names):
                value = values[name]
                for offset, item in enumerate((name, defaults.get(name), value, value // 2, value // 5)):
                    _cell(self.skills_table, row, column + offset, item, "#ffffff" if row % 2 == 0 else _LIGHT)
                    if offset == 0:
                        self.skills_table.item(row, column).setToolTip(name)
        header = self.skills_table.horizontalHeader()
        header.setStyleSheet(f"QHeaderView::section {{ background: {_BLUE}; color: {_INK}; padding: 4px; }}")
        header.show()
        for column in (0, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.skills_table.setColumnWidth(column, 128)
        layout.addWidget(self.skills_table, 1)

        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.hide()
        layout.addWidget(self.feedback)
        buttons = QHBoxLayout()
        buttons.addStretch()
        if organizer:
            self.reset_button = QPushButton("重置属性")
            self.reset_button.setStyleSheet("color: #a33737;")
            buttons.addWidget(self.reset_button)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
