"""玩家与组织者共用的大厅消息显示。"""

from PySide6.QtWidgets import QPlainTextEdit


class ChatHistory(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self._message_ids: set[str] = set()
        self._lines: dict[int, str] = {}
        self._last_sequence = 0

    def clear(self) -> None:
        super().clear()
        self._message_ids.clear()
        self._lines.clear()
        self._last_sequence = 0

    def add_message(self, event: dict[str, object]) -> None:
        self.add_messages([event])

    @property
    def oldest_sequence(self) -> int | None:
        return min(self._lines, default=None)

    def add_messages(self, events: list[dict]) -> None:
        lines = []
        inserted_earlier = False
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
                self._lines[sequence] = line
                inserted_earlier |= sequence <= self._last_sequence
                self._last_sequence = max(self._last_sequence, sequence)
            lines.append(line)
        if inserted_earlier:
            self.setPlainText("\n".join(self._lines[index] for index in sorted(self._lines)))
        elif lines:
            self.appendPlainText("\n".join(lines))
