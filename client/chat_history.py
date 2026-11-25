"""两端共用的增量消息队列与向上滚动分页。"""

from collections import deque
from typing import NamedTuple

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextEdit, QWidget

from .ui_icons import phosphor_icon
from .ui_style import CHAT_SCROLLBAR_STYLE, DISABLED, MUTED, SELECTION, SIDEBAR, SURFACE, TEXT


AVATAR_SIZE = 34
FADE_HEIGHT = 12
CONVERSATION_STYLE = f"""
QTextEdit#conversationHistory {{
    color: {TEXT}; background: {SURFACE}; border: none; padding: 0;
    selection-background-color: {SELECTION}; selection-color: {TEXT};
}}
"""


class _Message(NamedTuple):
    nickname: str
    text: str
    dice_result: bool


class _HistoryFade(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event) -> None:
        gradient = QLinearGradient(0, 0, 0, self.height())
        color = QColor(SURFACE)
        color.setAlpha(0)
        gradient.setColorAt(0, color)
        color.setAlpha(230)
        gradient.setColorAt(1, color)
        painter = QPainter(self)
        painter.fillRect(self.rect(), gradient)


class ChatHistory(QTextEdit):
    olderRequested = Signal()

    def __init__(self, *, conversation: bool = False) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self._message_ids: set[str] = set()
        self._records: dict[int, _Message] = {}
        self._queue: deque[int] = deque()
        self._last_sequence = 0
        self.newest_public_sequence = 0
        self.has_more = False
        self.history_loading = False
        self._updating = False
        self._conversation = conversation
        self._block_style = QTextBlockFormat()
        self._name_style = QTextCharFormat()
        self._body_style = QTextCharFormat()
        if conversation:
            self._fade = _HistoryFade(self)
            self._avatar = phosphor_icon("bold/user-circle-bold.svg", DISABLED, size=AVATAR_SIZE).pixmap(
                QSize(AVATAR_SIZE, AVATAR_SIZE), 2,
            )
            self._block_style.setLeftMargin(48)
            self._block_style.setRightMargin(6)
            self._block_style.setTopMargin(12)
            self._block_style.setBottomMargin(10)
            self._name_style.setForeground(QColor(MUTED))
            self._name_style.setFontWeight(QFont.Weight.DemiBold)
            self._body_style.setForeground(QColor(TEXT))
            self.setObjectName("conversationHistory")
            self.setStyleSheet(CONVERSATION_STYLE + CHAT_SCROLLBAR_STYLE)
            self.setAccessibleName("频道消息")
            self.document().setDocumentMargin(12)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)

    def clear(self) -> None:
        self._updating = True
        self.has_more = False
        self.history_loading = False
        super().clear()
        self._message_ids.clear()
        self._records.clear()
        self._queue.clear()
        self._last_sequence = 0
        self.newest_public_sequence = 0
        self._updating = False

    def set_pagination(self, has_more: bool, loading: bool = False) -> None:
        self.has_more = has_more
        self.history_loading = loading

    def request_older(self) -> None:
        if self.has_more and not self.history_loading and not self._updating:
            self.history_loading = True
            self.olderRequested.emit()

    def _scrolled(self, value: int) -> None:
        if value <= self.verticalScrollBar().minimum() + 2:
            self.request_older()

    def wheelEvent(self, event) -> None:
        super().wheelEvent(event)
        if event.angleDelta().y() > 0:
            self._scrolled(self.verticalScrollBar().value())

    def keyPressEvent(self, event) -> None:
        super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Home):
            self._scrolled(self.verticalScrollBar().value())

    def firstVisibleBlock(self):
        block = self.cursorForPosition(QPoint(0, 0)).block()
        layout = self.document().documentLayout()
        top = self.verticalScrollBar().value()
        while block.next().isValid() and layout.blockBoundingRect(block).bottom() <= top:
            block = block.next()
        return block

    def resizeEvent(self, event) -> None:
        bar = self.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        super().resizeEvent(event)
        if self._conversation:
            viewport = self.viewport().geometry()
            self._fade.setGeometry(viewport.x(), viewport.bottom() - FADE_HEIGHT + 1, viewport.width(), FADE_HEIGHT)
            self._fade.raise_()
        if follow:
            bar.setValue(bar.maximum())

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        if self._conversation:
            self.viewport().update(0, 0, 60, self.viewport().height())

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._conversation or self.document().isEmpty():
            return
        layout = self.document().documentLayout()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(SIDEBAR))
        origin = self.verticalScrollBar().value()
        block = self.firstVisibleBlock()
        while block.isValid():
            top = round(layout.blockBoundingRect(block).top()) - origin
            if top >= self.viewport().height():
                break
            painter.drawEllipse(QRectF(18, top + 1, AVATAR_SIZE, AVATAR_SIZE))
            painter.drawPixmap(18, top + 1, self._avatar)
            block = block.next()

    def _insert_message(self, cursor: QTextCursor, message: _Message) -> None:
        if self._conversation:
            name = " ".join(message.nickname.splitlines())
            body = (message.text.replace("\r\n", "\n").replace("\r", "\n")
                    .replace("\n", "\u2028").replace("\u2029", "\u2028"))
            lines = body.split("\u2028")
            first, end = 0, len(lines)
            # 首尾空行只影响留白，保留段落之间的空行和正文原有的空格。
            while first < end and not lines[first].strip():
                first += 1
            while first < end and not lines[end - 1].strip():
                end -= 1
            cursor.insertText(name + "\u2028", self._name_style)
            cursor.insertText("\u2028".join(lines[first:end]), self._body_style)
        else:
            separator = ": " if message.dice_result else "："
            cursor.insertText(message.nickname + separator + message.text)

    def add_message(self, event: dict[str, object]) -> None:
        self.add_messages([event])

    @property
    def oldest_sequence(self) -> int | None:
        return self._queue[0] if self._queue else None

    @property
    def newest_sequence(self) -> int:
        return self._last_sequence

    def add_messages(self, events: list[dict]) -> None:
        records = {}
        legacy = []
        for event in events:
            nickname, text = event.get("nickname"), event.get("text")
            if not isinstance(nickname, str) or not isinstance(text, str):
                continue
            message = _Message(nickname, text, event.get("kind") == "dice_result")
            message_id, sequence = event.get("message_id"), event.get("sequence")
            if isinstance(message_id, str) and type(sequence) is int:
                public_sequence = event.get("public_sequence", sequence)
                if type(public_sequence) is int:
                    self.newest_public_sequence = max(self.newest_public_sequence, public_sequence)
                if message_id in self._message_ids:
                    continue
                self._message_ids.add(message_id)
                if sequence not in self._records:
                    records[sequence] = message
            else:
                legacy.append(message)
        if not records and not legacy:
            return
        bar = self.verticalScrollBar()
        previous_value, previous_max = bar.value(), bar.maximum()
        follow = previous_value >= previous_max - 2
        ordered = sorted(records)
        prepend = bool(ordered and self._queue and ordered[-1] < self._queue[0])
        append = not ordered or not self._queue or ordered[0] > self._queue[-1]
        if prepend:
            anchor = QTextCursor(self.firstVisibleBlock())
            layout = self.document().documentLayout()
            offset = min(0, layout.blockBoundingRect(anchor.block()).top() - previous_value)
        self._updating = True
        self._records.update(records)
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        if prepend:
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            for key in ordered:
                self._insert_message(cursor, records[key])
                cursor.insertBlock(self._block_style)
            self._queue.extendleft(reversed(ordered))
            cursor.movePosition(QTextCursor.MoveOperation.End)
            messages = legacy
        else:
            if append:
                self._queue.extend(ordered)
                cursor.movePosition(QTextCursor.MoveOperation.End)
            else:
                # 同步补齐乱序缺口时按服务端序号重排。
                self._queue = deque(sorted(self._records))
                ordered = self._queue
                cursor.select(QTextCursor.SelectionType.Document)
                cursor.removeSelectedText()
            messages = [self._records[key] for key in ordered] + legacy
        for message in messages:
            if self.document().isEmpty():
                cursor.setBlockFormat(self._block_style)
            else:
                cursor.insertBlock(self._block_style)
            self._insert_message(cursor, message)
        cursor.endEditBlock()
        self._last_sequence = self._queue[-1] if self._queue else 0
        if prepend:
            bar.setValue(round(layout.blockBoundingRect(anchor.block()).top() - offset))
        elif follow:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(previous_value)
        self._updating = False
