"""两端共用的增量消息队列与向上滚动分页。"""

from collections import deque

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit


class ChatHistory(QPlainTextEdit):
    olderRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self._message_ids: set[str] = set()
        self._lines: dict[int, str] = {}
        self._queue: deque[int] = deque()
        self._last_sequence = 0
        self.has_more = False
        self.history_loading = False
        self._updating = False
        self.verticalScrollBar().valueChanged.connect(self._scrolled)

    def clear(self) -> None:
        self._updating = True
        self.has_more = False
        self.history_loading = False
        super().clear()
        self._message_ids.clear()
        self._lines.clear()
        self._queue.clear()
        self._last_sequence = 0
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
            line = f"{nickname}：{text}"
            message_id, sequence = event.get("message_id"), event.get("sequence")
            if isinstance(message_id, str) and type(sequence) is int:
                if message_id in self._message_ids:
                    continue
                self._message_ids.add(message_id)
                if sequence not in self._lines:
                    records[sequence] = line
            else:
                legacy.append(line)
        if not records and not legacy:
            return
        bar = self.verticalScrollBar()
        previous_value, previous_max = bar.value(), bar.maximum()
        follow = previous_value >= previous_max - 2
        ordered = sorted(records)
        prepend = bool(ordered and self._queue and ordered[-1] < self._queue[0])
        append = not ordered or not self._queue or ordered[0] > self._queue[-1]
        self._updating = True
        self._lines.update(records)
        cursor = QTextCursor(self.document())
        if prepend:
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.insertText("\n".join(records[key] for key in ordered) + "\n")
            self._queue.extendleft(reversed(ordered))
        elif append:
            self._queue.extend(ordered)
            cursor.movePosition(QTextCursor.MoveOperation.End)
            lines = [records[key] for key in ordered] + legacy
            cursor.insertText(("" if self.document().isEmpty() else "\n") + "\n".join(lines))
        else:
            # 补同步期间的乱序缺口较少；正常上翻仅在文档开头增量插入。
            self._queue = deque(sorted(self._lines))
            self.setPlainText("\n".join(self._lines[key] for key in self._queue))
        self._last_sequence = self._queue[-1] if self._queue else 0
        if prepend:
            bar.setValue(previous_value + bar.maximum() - previous_max)
        elif follow:
            bar.setValue(bar.maximum())
        else:
            bar.setValue(previous_value)
        self._updating = False
