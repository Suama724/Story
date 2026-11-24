"""参与者的只读频道资料与独立历史记录窗口。"""

import sqlite3

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView

from chat_protocol import HISTORY_PAGE_SIZE
from .chat_history import ChatHistory


class PersonalInfoDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("个人信息")
        self.setMinimumWidth(420)
        self.identity = QLineEdit()
        self.name = QLineEdit()
        self.password = QLineEdit()
        form = QFormLayout()
        for caption, field in (("身份", self.identity), ("名称", self.name), ("密码", self.password)):
            field.setReadOnly(True)
            form.addRow(caption, field)
        self.change_button = QPushButton("修改密码")
        self.change_button.setObjectName("primary")
        self.attributes_button = QPushButton("查看属性")
        self.attributes_button.hide()
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addLayout(form)
        layout.addWidget(self.feedback)
        layout.addWidget(self.attributes_button, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.change_button, alignment=Qt.AlignmentFlag.AlignRight)

    def set_identity(self, role, name, password, *, can_change):
        self.identity.setText(role.upper())
        self.name.setText(name)
        self.password.setText(password)
        self.password.setPlaceholderText("暂无本地密码" if not password else "")
        self.change_button.setEnabled(can_change)

    def set_feedback(self, text):
        self.feedback.setText(text)
        self.feedback.setVisible(bool(text))


class ChannelMembersDialog(QDialog):
    def __init__(self, parent, channel_id):
        super().__init__(parent)
        self.channel_id = channel_id
        self.resize(540, 380)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["身份", "名称"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)

    def update_channel(self, channel, online):
        self.setWindowTitle(f"{channel['name']} · 频道成员" + ("" if online else " · 本地资料"))
        members = channel.get("members", [])
        self.table.setRowCount(len(members))
        for row, member in enumerate(members):
            self.table.setItem(row, 0, QTableWidgetItem(member["role"].upper()))
            self.table.setItem(row, 1, QTableWidgetItem(member["actor_name"]))


class ChannelLogDialog(QDialog):
    def __init__(self, owner, channel_id):
        super().__init__(owner)
        self.owner = owner
        self.channel_id = channel_id
        self.history = ChatHistory()
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.hide()
        self.loaded = False
        self.has_more = False
        self.loading = False
        self.pending_live = []
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.fail("读取记录超时，可再次向上滚动重试。"))
        self.resize(880, 620)
        layout = QVBoxLayout(self)
        layout.addWidget(self.history, 1)
        layout.addWidget(self.feedback)
        self.history.olderRequested.connect(lambda: self.load(older=True))

    def cache(self):
        channel = self.owner._channels.get(self.channel_id)
        if channel is None or self.owner._base_cache is None:
            return None
        return self.owner._base_cache.for_channel(self.channel_id, lobby=channel["kind"] == "lobby")

    def load(self, *, older=False):
        if self.loading or self.channel_id not in self.owner._channels:
            return
        self.loading = True
        self.history.set_pagination(self.has_more, loading=True)
        before = self.history.oldest_sequence if older else None
        if self.owner.is_online:
            self.owner._request_log_history(self, before)
            return
        cache = self.cache()
        try:
            page = cache.page(before) if cache is not None else []
            self.apply(page, len(page) == HISTORY_PAGE_SIZE, replace=not older)
        except (OSError, sqlite3.Error):
            self.fail("无法读取本地记录。")

    def apply(self, page, has_more, *, replace=False):
        self.timer.stop()
        self.owner._cancel_log_requests(self)
        self.loading = False
        self.feedback.hide()
        if replace:
            self.history.clear()
        self.history.add_messages(page)
        self.history.add_messages(self.pending_live)
        self.pending_live.clear()
        self.loaded = True
        self.has_more = has_more
        self.history.set_pagination(has_more)

    def fail(self, text):
        self.timer.stop()
        self.owner._cancel_log_requests(self)
        self.loading = False
        self.feedback.setText(text)
        self.feedback.show()
        self.history.set_pagination(True)
