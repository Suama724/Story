"""主机工作台，先选团，再独立管理人物与网络监听。"""

import sys
from collections.abc import Callable
from html import escape
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QThread, QTimer, Qt, Slot
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from chat_protocol import chat_text_error
from client.chat_history import ChatHistory

from .management import add_member, campaign_details, discover_campaigns, edit_member
from .channels import archive_scene, create_scene, local_channel_state, set_visibility_mode
from .messages import local_lobby_history
from .server_controller import ServerController
from .storage import initialize_campaign, member_name

STYLE = """
QMainWindow, QDialog { background: #e9eef3; color: #203548; }
QWidget { font-size: 14px; }
QFrame#masthead { background: #203548; border-radius: 6px; }
QLabel#brand { color: white; font: 600 24px 'Bahnschrift'; }
QLabel#mastCaption { color: #c2d3e0; }
QFrame#panel { background: white; border: 1px solid #ccd6df; border-radius: 6px; }
QLabel#heading { font-size: 20px; font-weight: 600; }
QLabel#muted { color: #687b8d; }
QPushButton { background: #f6f8fa; border: 1px solid #bbcbd8; border-radius: 4px; padding: 7px 12px; }
QPushButton:hover { background: #e6eff7; border-color: #326b97; }
QPushButton:focus { border: 2px solid #326b97; }
QPushButton#primary { background: #326b97; color: white; border-color: #326b97; }
QPushButton#primary:hover { background: #25577e; }
QPushButton:disabled, QPushButton#primary:disabled { background: #edf1f5; color: #95a3af; border-color: #d5dee6; }
QLineEdit, QSpinBox, QPlainTextEdit, QListWidget, QTableWidget {
    background: white; color: #203548; border: 1px solid #ccd6df;
    border-radius: 4px; padding: 5px; selection-background-color: #d5e8f8;
    selection-color: #203548;
}
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid #326b97; }
QHeaderView::section { background: #f1f5f8; color: #687b8d; padding: 7px; border: none; }
QTabWidget::pane { border: none; }
QTabBar::tab { padding: 9px 18px; background: #e9eef3; color: #687b8d; }
QTabBar::tab:selected { background: white; color: #203548; border-top: 2px solid #326b97; }
"""


def label(text: str = "", name: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    return widget


def panel() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 18, 18, 18)
    layout.setSpacing(12)
    return frame, layout


class OperationThread(QThread):
    def __init__(self, operation: Callable, parent: QWidget) -> None:
        super().__init__(parent)
        self.operation = operation
        self.result = None
        self.error = ""

    def run(self) -> None:
        try:
            self.result = self.operation()
        except Exception as error:
            self.error = str(error)


class MemberDialog(QDialog):
    def __init__(self, parent: QWidget, member: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑人物" if member else "添加角色")
        self.resize(400, 180)
        self.actor = QLineEdit(str(member["actor_name"]) if member else "")
        self.actor.setMaxLength(160)
        form = QFormLayout()
        form.addRow("角色名", self.actor)
        self.validation = label()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.validation)
        layout.addWidget(buttons)

    def accept(self) -> None:
        try:
            member_name(self.actor.text())
        except ValueError as error:
            self.validation.setText(str(error))
            return
        super().accept()


class SceneDialog(QDialog):
    def __init__(self, parent: QWidget, members: list[dict]) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建场景")
        self.resize(400, 360)
        self.name = QLineEdit()
        self.name.setMaxLength(80)
        self.name.setPlaceholderText("例如，地下室")
        self.members = QListWidget()
        for member in members:
            if member["role"] != "pl":
                continue
            item = QListWidgetItem(member["actor_name"])
            item.setData(Qt.ItemDataRole.UserRole, member["member_id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.members.addItem(item)
        layout = QVBoxLayout(self)
        layout.addWidget(label("场景名称"))
        layout.addWidget(self.name)
        layout.addWidget(label("选择进入场景的角色"))
        layout.addWidget(self.members)
        self.feedback = label()
        layout.addWidget(self.feedback)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("创建场景")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_members(self) -> list[str]:
        return [self.members.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.members.count())
                if self.members.item(index).checkState() == Qt.CheckState.Checked]

    def accept(self) -> None:
        if not self.name.text().strip():
            self.feedback.setText("请填写场景名称。")
            return
        super().accept()


class HostWindow(QMainWindow):
    def __init__(self, data_root: Path) -> None:
        super().__init__()
        self.setWindowTitle("Story · 主机工作台")
        self.resize(1120, 840)
        self.setMinimumSize(960, 720)
        self.setStyleSheet(STYLE)
        self.data_root = data_root.resolve()
        self.campaign: dict | None = None
        self._job: OperationThread | None = None
        self._busy = False
        self._on_result: Callable | None = None
        self._closing = False
        self._returning = False
        self._refresh_pending = False
        self._history_has_more = False
        self._pending_send: str | None = None
        self._retry_send: tuple[str, str] | None = None
        self._channel_id = ""
        self._channels: dict[str, dict] = {}
        self._channel_request: str | None = None
        self._next_channel_id: str | None = None
        self._channel_drafts: dict[str, tuple[str, tuple[str, str] | None]] = {}
        self._queued_channel_state: dict | None = None
        self._visibility_mode = "secret"
        self.server = ServerController(self)
        self.server.stateChanged.connect(self._network_state)
        self.server.errorOccurred.connect(self._report_error)
        self.server.logReceived.connect(self._append_log)
        self.server.sendConfirmed.connect(self._send_confirmed)
        self.server.sendFailed.connect(self._send_failed)
        self.server.passwordChanged.connect(self._reload_campaign)
        self.server.channelsReceived.connect(self._channels_received)
        self.server.sendFailed.connect(self._channel_command_failed)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(16)
        masthead = QFrame()
        masthead.setObjectName("masthead")
        bar = QHBoxLayout(masthead)
        bar.setContentsMargins(20, 13, 20, 13)
        bar.addWidget(label("STORY", "brand"))
        bar.addWidget(label("主机工作台", "mastCaption"))
        bar.addStretch()
        self.header_state = label("选择一个团开始", "mastCaption")
        bar.addWidget(self.header_state)
        layout.addWidget(masthead)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_selector())
        self.pages.addWidget(self._build_lobby())
        layout.addWidget(self.pages, 1)
        self.feedback = label()
        self.feedback.setWordWrap(True)
        self.feedback.setMinimumWidth(0)
        layout.addWidget(self.feedback)
        self.setCentralWidget(central)
        self._update_buttons()
        QTimer.singleShot(0, self.refresh_campaigns)

    def _build_selector(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        root_row = QHBoxLayout()
        root_row.addWidget(label("数据目录"))
        self.root_field = QLineEdit(str(self.data_root))
        self.root_field.setReadOnly(True)
        browse = QPushButton("选择目录")
        browse.clicked.connect(self._browse_root)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh_campaigns)
        self.browse_button = browse
        root_row.addWidget(self.root_field, 1)
        root_row.addWidget(browse)
        root_row.addWidget(self.refresh_button)
        outer.addLayout(root_row)

        columns = QHBoxLayout()
        existing, existing_layout = panel()
        existing_layout.addWidget(label("打开已有团", "heading"))
        self.campaign_list = QListWidget()
        self.campaign_list.setSpacing(6)
        self.campaign_list.itemDoubleClicked.connect(self._select_campaign)
        self.campaign_list.currentRowChanged.connect(self._update_buttons)
        existing_layout.addWidget(self.campaign_list, 1)
        self.empty_hint = label("还没有团，可以在右侧新建。", "muted")
        existing_layout.addWidget(self.empty_hint)
        self.open_button = QPushButton("进入选中的团")
        self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self._select_campaign)
        existing_layout.addWidget(self.open_button)

        new, new_layout = panel()
        new_layout.addWidget(label("新建团", "heading"))
        self.new_name = QLineEdit()
        self.new_name.setMaxLength(200)
        self.new_name.setPlaceholderText("例如，地下室的来信")
        form = QFormLayout()
        form.addRow("团名", self.new_name)
        new_layout.addLayout(form)
        new_layout.addWidget(label("简介", "muted"))
        self.new_description = QPlainTextEdit()
        self.new_description.setAccessibleName("简介")
        self.new_description.setPlaceholderText("写下团本背景或本次跑团的说明")
        new_layout.addWidget(self.new_description, 1)
        new_layout.addWidget(label("创建后可在总频道添加角色。", "muted"))
        self.create_button = QPushButton("新建并进入")
        self.create_button.setObjectName("primary")
        self.create_button.clicked.connect(self._create_campaign)
        new_layout.addWidget(self.create_button)
        self.new_panel = new
        columns.addWidget(existing, 1)
        columns.addWidget(new, 1)
        outer.addLayout(columns, 1)
        return page

    def _build_lobby(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)
        heading = QHBoxLayout()
        self.campaign_title = label("", "heading")
        heading.addWidget(self.campaign_title, 1)
        self.switch_button = QPushButton("切换团")
        self.switch_button.clicked.connect(self._switch_campaign)
        heading.addWidget(self.switch_button)
        outer.addLayout(heading)
        self.campaign_description = label("", "muted")
        self.campaign_description.setWordWrap(True)
        outer.addWidget(self.campaign_description)

        network, network_layout = panel()
        network_layout.setContentsMargins(14, 12, 14, 12)
        row = QHBoxLayout()
        self.network_status = label("监听已关闭")
        self.listen_host = QLineEdit("127.0.0.1")
        self.listen_host.setMaximumWidth(160)
        self.listen_port = QSpinBox()
        self.listen_port.setRange(1, 65535)
        self.listen_port.setValue(8000)
        self.network_button = QPushButton("启动监听")
        self.network_button.setObjectName("primary")
        self.network_button.clicked.connect(self._toggle_network)
        row.addWidget(self.network_status)
        row.addStretch()
        row.addWidget(label("监听地址"))
        row.addWidget(self.listen_host)
        row.addWidget(label("端口"))
        row.addWidget(self.listen_port)
        row.addWidget(self.network_button)
        network_layout.addLayout(row)
        address_row = QHBoxLayout()
        self.connection_address = QLineEdit()
        self.connection_address.setReadOnly(True)
        copy_address = QPushButton("复制连接地址")
        copy_address.clicked.connect(lambda: QApplication.clipboard().setText(self.connection_address.text()))
        address_row.addWidget(self.connection_address, 1)
        address_row.addWidget(copy_address)
        network_layout.addLayout(address_row)
        self.listen_host.textChanged.connect(self._update_address)
        self.listen_port.valueChanged.connect(self._update_address)
        self._update_address()
        outer.addWidget(network)

        split = QSplitter()
        people, people_layout = panel()
        people_heading = QHBoxLayout()
        people_heading.addWidget(label("人物", "heading"))
        people_heading.addStretch()
        self.add_button = QPushButton("添加角色")
        self.add_button.clicked.connect(self._add_member)
        people_heading.addWidget(self.add_button)
        people_layout.addLayout(people_heading)
        self.member_table = QTableWidget(0, 3)
        self.member_table.setHorizontalHeaderLabels(["身份", "角色名", "密码"])
        self.member_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.member_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.member_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.member_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.member_table.verticalHeader().setVisible(False)
        self.member_table.itemSelectionChanged.connect(self._update_buttons)
        people_layout.addWidget(self.member_table, 1)
        member_actions = QHBoxLayout()
        self.edit_button = QPushButton("编辑人物")
        self.edit_button.clicked.connect(self._edit_member)
        member_actions.addWidget(self.edit_button)
        people_layout.addLayout(member_actions)
        self.members_refresh = QPushButton("刷新人物状态")
        self.members_refresh.clicked.connect(self._reload_campaign)
        people_layout.addWidget(self.members_refresh)

        channel, channel_layout = panel()
        channel_layout.addWidget(label("场景与聊天", "heading"))
        channel_row = QHBoxLayout()
        self.channel_choice = QComboBox()
        self.channel_choice.currentIndexChanged.connect(self._select_host_channel)
        self.create_scene_button = QPushButton("新建场景")
        self.create_scene_button.clicked.connect(self._create_scene)
        self.archive_scene_button = QPushButton("结束场景")
        self.archive_scene_button.clicked.connect(self._archive_scene)
        channel_row.addWidget(self.channel_choice, 1)
        channel_row.addWidget(self.create_scene_button)
        channel_row.addWidget(self.archive_scene_button)
        channel_layout.addLayout(channel_row)
        mode_row = QHBoxLayout()
        mode_row.addWidget(label("场景阅读模式"))
        self.visibility_choice = QComboBox()
        self.visibility_choice.addItem("保密 · 按参与范围阅读", "secret")
        self.visibility_choice.addItem("宽泛 · 可旁观其他聊天室", "broad")
        self.visibility_choice.currentIndexChanged.connect(self._change_visibility)
        mode_row.addWidget(self.visibility_choice, 1)
        channel_layout.addLayout(mode_row)
        self.tabs = QTabWidget()
        self.messages = ChatHistory()
        self.messages.setMinimumHeight(140)
        self.server.messageReceived.connect(self._receive_message)
        self.messages.setPlaceholderText("暂无大厅消息。")
        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)
        self.older_button = QPushButton("查看更早消息")
        self.older_button.clicked.connect(self._load_older_history)
        history_layout.addWidget(self.older_button, alignment=Qt.AlignmentFlag.AlignLeft)
        history_layout.addWidget(self.messages, 1)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1000)
        self.logs.setFont(QFont("Consolas", 10))
        self.tabs.addTab(history_tab, "频道消息")
        self.tabs.addTab(self.logs, "运行日志")
        channel_layout.addWidget(self.tabs, 1)
        self.message_input = QPlainTextEdit()
        self.message_input.setPlaceholderText("以主持人身份发言")
        self.message_input.setFixedHeight(76)
        channel_layout.addWidget(self.message_input)
        self.send_button = QPushButton("发送到总频道")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self._send_message)
        channel_layout.addWidget(self.send_button)
        split.addWidget(people)
        split.addWidget(channel)
        split.setSizes([430, 610])
        outer.addWidget(split, 1)
        return page

    def _run(self, operation: Callable, on_result: Callable) -> None:
        if self._busy:
            return
        self._busy = True
        self._on_result = on_result
        self.feedback.setText("正在处理…")
        self.feedback.setStyleSheet("color: #687b8d;")
        self._update_buttons()
        worker = OperationThread(operation, self)
        self._job = worker
        worker.finished.connect(self._job_result)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @Slot()
    def _job_result(self) -> None:
        result, error = self._job.result, self._job.error
        self._job = None
        self._busy = False
        if error:
            self._next_channel_id = None
            self.visibility_choice.blockSignals(True)
            self.visibility_choice.setCurrentIndex(self.visibility_choice.findData(self._visibility_mode))
            self.visibility_choice.blockSignals(False)
            self._report_error(error)
        else:
            self.feedback.setText("")
            if not self._closing:
                self._on_result(result)
        self._update_buttons()
        if self._queued_channel_state is not None and not self._busy and not self._closing:
            state, self._queued_channel_state = self._queued_channel_state, None
            self._populate_channels(state)
        if self._closing and not self.server.active:
            QTimer.singleShot(0, self.close)
        elif self._refresh_pending and not self._closing:
            self._reload_campaign()

    def _report_error(self, message: str) -> None:
        self.feedback.setText(message)
        self.feedback.setStyleSheet("color: #a33737;")
        self._append_log(message)

    def _append_log(self, message: str) -> None:
        self.logs.appendPlainText(message)

    def refresh_campaigns(self) -> None:
        def populated(result: tuple) -> None:
            campaigns, errors = result
            self.campaign_list.clear()
            for campaign in campaigns:
                item = QListWidgetItem(
                    f"{campaign['name']}\n{campaign['kp_name']} · {len(campaign['members']) - 1} 名玩家"
                )
                item.setData(Qt.ItemDataRole.UserRole, campaign["directory"])
                item.setToolTip(f"<pre>{escape(campaign['description'] or str(campaign['directory']))}</pre>")
                self.campaign_list.addItem(item)
            self.empty_hint.setVisible(not campaigns)
            if campaigns:
                self.campaign_list.setCurrentRow(0)
            if errors:
                self._report_error("部分团读取失败，" + "；".join(errors))

        self._run(lambda: discover_campaigns(self.data_root), populated)

    def _browse_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择团数据目录", str(self.data_root))
        if selected:
            self.data_root = Path(selected)
            self.root_field.setText(str(self.data_root))
            self.refresh_campaigns()

    def _select_campaign(self, *args: object) -> None:
        item = self.campaign_list.currentItem()
        if item is not None and not self._busy:
            directory = item.data(Qt.ItemDataRole.UserRole)
            self._run(lambda: self._read_campaign(directory), self._enter_campaign)

    def _create_campaign(self) -> None:
        name = self.new_name.text()
        description = self.new_description.toPlainText()

        def create() -> tuple[dict, dict]:
            directory = initialize_campaign(self.data_root, name, description=description)
            return self._read_campaign(directory)

        self._run(create, self._enter_campaign)

    @staticmethod
    def _read_campaign(directory: Path) -> tuple[dict, dict]:
        campaign = campaign_details(directory)
        campaign["channel_state"] = local_channel_state(directory)
        return campaign, local_lobby_history(directory)

    def _enter_campaign(self, result: tuple[dict, dict]) -> None:
        campaign, history = result
        self.campaign = campaign
        self.campaign_title.setText(str(campaign["name"]))
        self.campaign_title.setToolTip(str(campaign["directory"]))
        description = str(campaign["description"])
        self.campaign_description.setText(description.split("\n", 1)[0][:200])
        self.campaign_description.setToolTip(f"<pre>{escape(description)}</pre>")
        self.campaign_description.setVisible(bool(description))
        self.header_state.setText("本地管理 · 监听已关闭")
        self.messages.clear()
        self._channel_drafts.clear()
        self._channel_id = campaign["lobby_id"]
        self._populate_channels(campaign["channel_state"])
        self._show_history(history)
        self._retry_send = None
        self.message_input.clear()
        self.pages.setCurrentIndex(1)
        self._populate_members()

    def _show_history(self, page: dict) -> None:
        self.messages.add_messages(page["messages"])
        self._history_has_more = page["has_more"]

    def _load_older_history(self) -> None:
        before = self.messages.oldest_sequence
        if self.campaign is not None and self._history_has_more and before is not None:
            directory = self.campaign["directory"]
            channel_id = self._channel_id
            self._run(lambda: local_lobby_history(directory, before=before, channel_id=channel_id), self._show_history)

    def _populate_channels(self, state: dict) -> None:
        self._channels = {item["channel_id"]: item for item in state["channels"]}
        self._visibility_mode = state["visibility_mode"]
        selected = self._next_channel_id or self._channel_id
        self._next_channel_id = None
        if selected not in self._channels:
            selected = state["current_channel_id"]
        self.channel_choice.blockSignals(True)
        self.channel_choice.clear()
        for item in state["channels"]:
            suffix = "（归档）" if item["status"] == "archived" else ""
            self.channel_choice.addItem(item["name"] + suffix, item["channel_id"])
        self.channel_choice.setCurrentIndex(self.channel_choice.findData(selected))
        self.channel_choice.blockSignals(False)
        self.visibility_choice.blockSignals(True)
        self.visibility_choice.setCurrentIndex(self.visibility_choice.findData(state["visibility_mode"]))
        self.visibility_choice.blockSignals(False)
        if selected != self._channel_id:
            self._select_host_channel(self.channel_choice.currentIndex())
        self._update_buttons()

    def _select_host_channel(self, _index: int) -> None:
        selected = self.channel_choice.currentData()
        if selected not in self._channels or selected == self._channel_id:
            return
        self._channel_drafts[self._channel_id] = (self.message_input.toPlainText(), self._retry_send)
        self._channel_id = selected
        text, self._retry_send = self._channel_drafts.get(selected, ("", None))
        self.message_input.setPlainText(text)
        self.messages.clear()
        directory = self.campaign["directory"]
        self._run(lambda: local_lobby_history(directory, channel_id=selected), self._show_history)

    def _receive_message(self, event: dict) -> None:
        if event.get("channel_id") == self._channel_id:
            self.messages.add_message(event)

    def _create_scene(self) -> None:
        dialog = SceneDialog(self, self.campaign["members"])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        channel_id = str(uuid4())
        self._next_channel_id = channel_id
        self._channel_operation("scene.create", name=dialog.name.text(), member_ids=dialog.selected_members(),
                                channel_id=channel_id)

    def _archive_scene(self) -> None:
        channel = self._channels[self._channel_id]
        if QMessageBox.question(self, "结束场景", f"结束「{channel['name']}」并让其中的角色回到大厅？") != QMessageBox.StandardButton.Yes:
            return
        self._channel_operation("scene.archive", channel_id=self._channel_id)

    def _change_visibility(self, _index: int) -> None:
        if self.campaign is not None:
            self._channel_operation("visibility.set", mode=self.visibility_choice.currentData())

    def _channel_operation(self, operation: str, **values: object) -> None:
        if self.server.state == "running":
            request_id = str(uuid4())
            if self.server.command(operation, request_id=request_id, **values):
                self._channel_request = request_id
                self.feedback.setText("正在更新场景…")
                self._update_buttons()
            return
        directory = self.campaign["directory"]

        def update() -> dict:
            if operation == "scene.create":
                create_scene(directory, values["name"], values["member_ids"], channel_id=values["channel_id"])
            elif operation == "scene.archive":
                archive_scene(directory, values["channel_id"])
            elif operation == "visibility.set":
                set_visibility_mode(directory, values["mode"])
            return local_channel_state(directory)

        self._run(update, self._populate_channels)

    def _channels_received(self, request_id: str, state: dict) -> None:
        if self.campaign is None:
            return
        if request_id == self._channel_request:
            self._channel_request = None
            self.feedback.clear()
        if self._busy:
            self._queued_channel_state = state
            return
        self._populate_channels(state)

    def _channel_command_failed(self, request_id: str) -> None:
        if request_id == self._channel_request:
            self._channel_request = None
            self._next_channel_id = None
            self.server.command("channels")
            self._update_buttons()

    def _populate_members(self) -> None:
        selected = self._selected_member()
        selected_id = selected["member_id"] if selected else None
        self.member_table.setRowCount(0)
        for member in self.campaign["members"]:
            row = self.member_table.rowCount()
            self.member_table.insertRow(row)
            password = member["password"]
            if password is None:
                password = "本地主持" if member["role"] == "kp" else "旧密码"
            values = ["KP" if member["role"] == "kp" else "PL", member["actor_name"], password]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, member)
                item.setToolTip(f"<pre>{escape(str(value))}</pre>")
                self.member_table.setItem(row, column, item)
            if member["member_id"] == selected_id:
                self.member_table.selectRow(row)
        if self.member_table.currentRow() < 0 and self.member_table.rowCount():
            self.member_table.selectRow(0)

    def _selected_member(self) -> dict | None:
        row = self.member_table.currentRow()
        item = self.member_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _reload_campaign(self) -> None:
        self._refresh_pending = False
        if self.campaign is None or self._closing or self._returning:
            return
        if self._busy:
            self._refresh_pending = True
            return
        directory = self.campaign["directory"]
        self._run(lambda: campaign_details(directory), self._members_updated)

    def _add_member(self) -> None:
        dialog = MemberDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            actor = dialog.actor.text()
            directory = self.campaign["directory"]

            def add() -> dict:
                add_member(directory, actor)
                return campaign_details(directory)

            self._run(add, self._members_updated)

    def _members_updated(self, campaign: dict) -> None:
        self.campaign = campaign
        self._populate_members()

    def _edit_member(self) -> None:
        member = self._selected_member()
        if member is None:
            return
        dialog = MemberDialog(self, member)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            actor = dialog.actor.text()
            directory = self.campaign["directory"]

            def edit() -> dict:
                edit_member(directory, member["member_id"], actor)
                return campaign_details(directory)

            self._run(edit, self._members_updated)

    def _update_address(self, *args: object) -> None:
        host = self.listen_host.text().strip()
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1" if host == "0.0.0.0" else "::1"
        if ":" in host:
            host = f"[{host}]"
        self.connection_address.setText(f"ws://{host}:{self.listen_port.value()}/ws/lobby")

    def _toggle_network(self) -> None:
        if self.server.active:
            self.server.stop()
        else:
            host = self.listen_host.text().strip()
            if not host:
                self._report_error("请填写监听地址。")
                return
            self.feedback.setText("")
            self.server.start(self.campaign["directory"], host, self.listen_port.value())

    def _network_state(self, state: str) -> None:
        captions = {"stopped": "监听已关闭", "starting": "正在启动…", "running": "正在监听", "stopping": "正在停止…"}
        self.network_status.setText(captions[state])
        self.header_state.setText(f"本地管理 · {captions[state]}")
        self.network_button.setText("启动监听" if state == "stopped" else "停止监听")
        if state == "running":
            self.server.command("channels")
        if state == "stopped":
            self._channel_request = None
        self._update_buttons()
        if state == "stopped":
            if self._pending_send is not None:
                self._pending_send = None
                self.message_input.setReadOnly(False)
                self._report_error("监听已停止，未确认的草稿已保留。")
            if self._closing and not self._busy:
                QTimer.singleShot(0, self.close)
            elif self._returning:
                self._return_to_selector()
        self._update_buttons()

    def _send_message(self) -> None:
        if self._pending_send is not None or self.server.state != "running":
            return
        text = self.message_input.toPlainText()
        if chat_text_error(text) is not None:
            self._report_error("消息须为 1–2,000 个字符的非空白文本。")
            return
        if self._retry_send is None or self._retry_send[0] != text:
            self._retry_send = (text, str(uuid4()))
        request_id = self._retry_send[1]
        if self.server.command("say", text=text, request_id=request_id, channel_id=self._channel_id):
            self._pending_send = request_id
            self.message_input.setReadOnly(True)
            self._update_buttons()

    def _send_confirmed(self, request_id: str, message: dict[str, object]) -> None:
        if request_id == self._pending_send:
            self._receive_message(message)
            self._pending_send = None
            self._retry_send = None
            self.message_input.clear()
            self.message_input.setReadOnly(False)
            self._update_buttons()

    def _send_failed(self, request_id: str) -> None:
        if request_id == self._pending_send:
            self._pending_send = None
            self.message_input.setReadOnly(False)
            self._update_buttons()

    def _switch_campaign(self) -> None:
        if self.server.active:
            answer = QMessageBox.question(self, "切换团", "停止当前监听并返回选团页面？")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._returning = True
            self.server.stop()
        else:
            self._return_to_selector()

    def _return_to_selector(self) -> None:
        self._returning = False
        self.campaign = None
        self._channels.clear()
        self._channel_id = ""
        self._channel_drafts.clear()
        self._queued_channel_state = None
        self._next_channel_id = None
        self.pages.setCurrentIndex(0)
        self.header_state.setText("选择一个团开始")
        self.refresh_campaigns()

    def _update_buttons(self, *args: object) -> None:
        available = not self._busy and not self._closing and self._channel_request is None
        self.open_button.setEnabled(available and self.campaign_list.currentItem() is not None)
        self.create_button.setEnabled(available)
        self.new_panel.setEnabled(available)
        self.browse_button.setEnabled(available)
        self.refresh_button.setEnabled(available)
        self.switch_button.setEnabled(available and not self._returning)
        self.older_button.setEnabled(available and self.campaign is not None and self._history_has_more and not self._returning)
        self.add_button.setEnabled(available)
        self.members_refresh.setEnabled(available)
        selected = self._selected_member()
        self.edit_button.setEnabled(available and selected is not None and selected["role"] == "pl")
        self.listen_host.setEnabled(not self.server.active)
        self.listen_port.setEnabled(not self.server.active)
        self.network_button.setEnabled(self.campaign is not None and self.server.state != "stopping" and not self._closing)
        channel = self._channels.get(self._channel_id, {})
        can_send = channel.get("can_send", False)
        scene_available = available and self.campaign is not None and self._pending_send is None and self.server.state in ("stopped", "running")
        self.channel_choice.setEnabled(scene_available)
        self.create_scene_button.setEnabled(scene_available)
        self.visibility_choice.setEnabled(scene_available)
        self.archive_scene_button.setEnabled(scene_available and channel.get("kind") == "scene" and channel.get("status") == "active")
        self.send_button.setText(f"发送到{channel.get('name', '频道')}")
        self.message_input.setReadOnly(self._pending_send is not None or not can_send)
        self.send_button.setEnabled(available and self.server.state == "running" and self._pending_send is None and can_send)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.server.active:
            if not self._closing:
                answer = QMessageBox.question(self, "关闭主机", "停止监听并关闭主机窗口？")
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
            self._closing = True
            self.server.stop()
            event.ignore()
        elif self._busy:
            self._closing = True
            self.feedback.setText("正在完成本地操作，完成后关闭。")
            event.ignore()
        else:
            event.accept()


def run_gui(data_root: Path) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Story Host")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    window = HostWindow(data_root)
    window.show()
    return app.exec()
