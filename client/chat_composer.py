"""Growing channel message input shared by the player and host."""

from math import ceil

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import QPlainTextEdit

from .ui_style import BORDER, FOCUS, SELECTION, SLIM_SCROLLBAR_STYLE, SURFACE, TEXT


MAX_VISIBLE_LINES = 8
VERTICAL_PADDING = 12
STYLE = f"""
QPlainTextEdit#chatComposer {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 9px;
    padding: 0; selection-background-color: {SELECTION}; selection-color: {TEXT};
}}
QPlainTextEdit#chatComposer:focus {{ border-color: {FOCUS}; }}
"""


class ChatComposer(QPlainTextEdit):
    sendRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.send_available = False
        self._composing = False
        self.setObjectName("chatComposer")
        self.setStyleSheet(STYLE + SLIM_SCROLLBAR_STYLE)
        self.setAccessibleName("消息输入")
        self.setAccessibleDescription("Enter 发送，Shift+Enter 换行")
        self.setTabChangesFocus(True)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 第一行右侧预留给后续输入工具。
        self.setViewportMargins(14, VERTICAL_PADDING, 96, VERTICAL_PADDING)
        self.document().setDocumentMargin(0)
        self.textChanged.connect(self._fit_height)
        self._fit_height()

    def _fit_height(self) -> None:
        line_height = self.fontMetrics().lineSpacing()
        limit = MAX_VISIBLE_LINES * line_height
        height = 0.0
        block = self.document().firstBlock()
        while block.isValid() and height < limit:
            height += self.blockBoundingRect(block).height()
            block = block.next()
        visible_height = ceil(max(line_height, min(height, limit)))
        # 留出末行光标和高 DPI 像素取整所需的空间。
        self.setFixedHeight(visible_height + 2 * (VERTICAL_PADDING + self.frameWidth()) + 2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._fit_height()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_height()

    def inputMethodEvent(self, event) -> None:
        self._composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            if modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
                event.accept()
                if self._composing or self.isReadOnly():
                    return
                if modifiers == Qt.KeyboardModifier.ShiftModifier:
                    self.insertPlainText("\n")
                elif self.send_available and not event.isAutoRepeat() and self.toPlainText().strip():
                    self.sendRequested.emit()
                return
        super().keyPressEvent(event)
