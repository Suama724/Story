"""组织方大厅的频道导航与独立辅助窗口。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QHeaderView,
)

from client.chat_history import ChatHistory
from .messages import local_lobby_history


def text_label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def make_table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.verticalHeader().hide()
    table.setAlternatingRowColors(False)
    return table


class ConnectionDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("连接状态")
        self.resize(800, 470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(16)
        row = QHBoxLayout()
        self.status = text_label("连接状态：关闭")
        row.addWidget(self.status, 1)
        self.listen_host = QLineEdit("127.0.0.1")
        self.listen_host.setAccessibleName("监听地址")
        self.listen_host.setMaximumWidth(200)
        self.listen_port = QSpinBox()
        self.listen_port.setAccessibleName("端口")
        self.listen_port.setRange(1, 65535)
        self.listen_port.setValue(8000)
        row.addWidget(text_label("监听地址"))
        row.addWidget(self.listen_host)
        row.addWidget(text_label("端口"))
        row.addWidget(self.listen_port)
        layout.addLayout(row)
        address = QHBoxLayout()
        address.addWidget(text_label("连接地址"))
        self.address = QLineEdit()
        self.address.setReadOnly(True)
        self.address.setAccessibleName("连接地址")
        address.addWidget(self.address, 1)
        self.copy_button = QPushButton("复制连接地址")
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.address.text()))
        address.addWidget(self.copy_button)
        layout.addLayout(address)
        self.members = make_table(["身份", "名称", "状态"])
        layout.addWidget(self.members, 1)

    def update_members(self, members, online):
        participants = [member for member in members if member["role"] != "kp"]
        self.members.setRowCount(len(participants))
        for row, member in enumerate(participants):
            for column, value in enumerate((member["role"].upper(), member["actor_name"],
                                             "在线" if member["member_id"] in online else "离线")):
                self.members.setItem(row, column, QTableWidgetItem(value))


class ParticipantsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("参与者管理")
        self.resize(800, 500)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.refresh = QPushButton("刷新角色状态")
        self.add = QPushButton("添加人物")
        self.add.setObjectName("primary")
        self.edit = QPushButton("编辑人物")
        for button in (self.refresh, self.add, self.edit):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.members = make_table(["身份", "名称", "密码"])
        layout.addWidget(self.members, 1)


class RuntimeLogDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("运行日志")
        self.resize(800, 480)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1000)
        layout = QVBoxLayout(self)
        layout.addWidget(self.logs)


class ChannelHistoryDialog(QDialog):
    def __init__(self, parent, directory, channel):
        super().__init__(parent)
        self.owner = parent
        self.directory = directory
        self.channel_id = channel["channel_id"]
        self.setWindowTitle(f"{channel['name']} · 完整聊天记录")
        self.resize(880, 620)
        self.history = ChatHistory()
        self.history.olderRequested.connect(lambda: self.load(older=True))
        layout = QVBoxLayout(self)
        layout.addWidget(self.history, 1)
        self.has_more = False
        self.loaded = False
        self.pending_live = []

    def load(self, *, older=False):
        if self.owner._busy:
            self.history.set_pagination(self.has_more)
            return
        before = self.history.oldest_sequence if older else None
        self.history.set_pagination(self.has_more, loading=True)

        def apply(page):
            if not older:
                self.history.clear()
            self.history.add_messages(page["messages"])
            self.history.add_messages(self.pending_live)
            self.pending_live.clear()
            self.has_more = page["has_more"]
            self.history.set_pagination(self.has_more)
            self.loaded = True

        self.owner._run(lambda: local_lobby_history(self.directory, before=before, channel_id=self.channel_id), apply)


class ChannelManagementDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.owner = parent
        self.setWindowTitle("频道管理")
        self.resize(820, 560)
        self.table = make_table(["频道组", "频道", "状态"])
        self.name = QLineEdit()
        self.name.setMaxLength(80)
        self.group = QComboBox()
        self.description = QPlainTextEdit()
        self.description.setMaximumHeight(100)
        form = QFormLayout()
        form.addRow("频道名称", self.name)
        form.addRow("所属频道组", self.group)
        form.addRow("频道简介", self.description)
        self.save = QPushButton("保存")
        self.save.setObjectName("primary")
        self.save.clicked.connect(self._save)
        self.table.itemSelectionChanged.connect(self._selected)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addLayout(form)
        layout.addWidget(self.save, alignment=Qt.AlignmentFlag.AlignRight)
        self._signature = None
        self.channels = {}
        self.revision = 0

    def populate(self, channels, groups, revision, *, force=False):
        signature = ([(key, item["name"], item.get("group_id"), item.get("description"), item["status"])
                      for key, item in channels.items()], groups)
        if not force and self._signature == signature:
            self.revision = revision
            return
        self._signature = signature
        selected = self.selected_id()
        self.channels = channels
        self.revision = revision
        self.group.clear()
        for group in groups:
            self.group.addItem(group["name"], group["group_id"])
        names = {item["group_id"]: item["name"] for item in groups}
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        chosen = 0
        for row, channel in enumerate(channels.values()):
            self.table.insertRow(row)
            for column, value in enumerate((names.get(channel.get("group_id"), ""), channel["name"],
                                             "归档" if channel["status"] == "archived" else "活动")):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, channel["channel_id"])
                self.table.setItem(row, column, item)
            if channel["channel_id"] == selected:
                chosen = row
        self.table.selectRow(chosen)
        self.table.blockSignals(False)
        self._selected()

    def selected_id(self):
        item = self.table.item(self.table.currentRow(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected(self):
        channel = self.channels.get(self.selected_id())
        self.save.setEnabled(channel is not None and not self.owner._busy and self.owner._channel_request is None)
        if channel:
            self.name.setText(channel["name"])
            self.group.setCurrentIndex(self.group.findData(channel.get("group_id")))
            self.description.setPlainText(channel.get("description", ""))

    def _save(self):
        if self.selected_id() is not None:
            self.owner._channel_operation("channel.update", channel_id=self.selected_id(), name=self.name.text(),
                                          group_id=self.group.currentData(), description=self.description.toPlainText(),
                                          expected_revision=self.revision)


class ChannelMembersDialog(QDialog):
    def __init__(self, parent, channel):
        super().__init__(parent)
        self.setWindowTitle(f"{channel['name']} · 频道成员")
        self.channel_id = channel["channel_id"]
        self.members = make_table(["身份", "名称"])
        self.resize(540, 380)
        layout = QVBoxLayout(self)
        layout.addWidget(self.members)

    def populate(self, participants):
        self.members.setRowCount(len(participants))
        for row, member in enumerate(participants):
            self.members.setItem(row, 0, QTableWidgetItem(member["role"].upper()))
            self.members.setItem(row, 1, QTableWidgetItem(member["actor_name"]))
