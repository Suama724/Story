"""主机工作台，先选团，再独立管理人物与网络监听。"""

import sqlite3
import sys
from collections.abc import Callable
from html import escape
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QSize, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QStackedWidget, QInputDialog,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from chat_protocol import chat_text_error
from client.attribute_dialogs import AttributeSheetDialog
from client.channel_header import ChannelHeader
from client.chat_composer import ChatComposer
from client.chat_history import ChatHistory
from client.channel_tree import ChannelTree
from client.player_window import identity_list
from client.ui_style import (
    BACKGROUND, BORDER, BUTTON_STYLE, CHANNEL_NAV_STYLE, DANGER, FOCUS, MUTED, SELECTION, SIDEBAR, SURFACE, TEXT,
    apply_theme,
)
from client.ui_widgets import ElidedButton, split_channel_view
from client.window_title_bar import BAR_HEIGHT, window_layout

from .management import add_member, campaign_details, edit_member
from .attributes import local_attributes, reset_local_attributes
from .dice import local_dice_config, save_local_dice_config
from .dice_dialog import DiceManagementDialog
from .campaign_library import CampaignLibrary
from .campaign_dialogs import CampaignManagementDialog, add_campaign_card
from .channels import execute_channel_operation, local_channel_state, mark_channel_read
from .messages import local_lobby_history
from .server_controller import ServerController
from .storage import member_name
from .lobby_dialogs import (
    ConnectionDialog, ParticipantsDialog, RuntimeLogDialog,
    ChannelHistoryDialog, ChannelManagementDialog, ChannelMembersDialog,
)

STYLE = BUTTON_STYLE + CHANNEL_NAV_STYLE + f"""
QMainWindow, QDialog {{ color: {TEXT}; }}
QDialog {{ background: {BACKGROUND}; }}
QWidget {{ font-size: 14px; }}
QFrame#panel {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; }}
QLabel#muted {{ color: {MUTED}; }}
QPushButton {{ padding: 7px 12px; }}
QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit, QComboBox, QListWidget, QTableWidget {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 5px; selection-background-color: {SELECTION}; selection-color: {TEXT};
}}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{ border-color: {FOCUS}; }}
QHeaderView::section {{ background: {SIDEBAR}; color: {MUTED}; padding: 7px; border: none; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ padding: 9px 18px; background: {BACKGROUND}; color: {MUTED}; }}
QTabBar::tab:selected {{ background: {SURFACE}; color: {TEXT}; border-top: 2px solid {FOCUS}; }}
QWidget#lobbyPage {{ background: {SURFACE}; }}
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


class FeedbackLabel(QLabel):
    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))


class MemberDialog(QDialog):
    def __init__(self, parent: QWidget, member: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑人物" if member else "添加角色 / OB")
        self.resize(400, 180)
        self.actor = QLineEdit(str(member["actor_name"]) if member else "")
        self.actor.setMaxLength(160)
        form = QFormLayout()
        self.role = QComboBox()
        self.role.addItem("PL · 玩家角色", "pl")
        self.role.addItem("OB · 观众", "ob")
        self.role.setCurrentIndex(self.role.findData(member["role"]) if member else 0)
        self.role.setEnabled(member is None)
        form.addRow("身份", self.role)
        form.addRow("名称", self.actor)
        self.actor_label = form.labelForField(self.actor)
        self.password = QLineEdit("123456")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaxLength(256)
        form.addRow("初始密码", self.password)
        self.password_label = form.labelForField(self.password)
        self.role.currentIndexChanged.connect(lambda _: self._update_role(member is None))
        self._update_role(member is None)
        self.validation = label()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.validation)
        self.attributes_button = None
        if member and member["role"] == "pl":
            self.attributes_button = QPushButton("查看属性")
            self.attributes_button.setEnabled(bool(member.get("attributes_imported")))
            layout.addWidget(self.attributes_button)
        layout.addWidget(buttons)

    def _update_role(self, creating: bool) -> None:
        is_ob = self.role.currentData() == "ob"
        self.actor_label.setText("名称")
        self.password.setVisible(creating and is_ob)
        self.password_label.setVisible(creating and is_ob)

    def accept(self) -> None:
        try:
            member_name(self.actor.text())
            if self.role.currentData() == "ob" and not self.password.text().strip():
                raise ValueError("请设置观众密码。")
        except ValueError as error:
            self.validation.setText(str(error))
            return
        super().accept()


class SceneDialog(QDialog):
    def __init__(self, parent: QWidget, members: list[dict], groups: list[dict] | None = None,
                 group_id: str | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建频道")
        self.resize(440, 480)
        self.name = QLineEdit()
        self.name.setMaxLength(80)
        self.group_choice = QComboBox()
        for group in groups or []:
            self.group_choice.addItem(group["name"], group["group_id"])
        if group_id is not None:
            self.group_choice.setCurrentIndex(self.group_choice.findData(group_id))
        self.description = QPlainTextEdit()
        self.description.setMaximumHeight(90)
        self.members = QListWidget()
        for member in members:
            if member["role"] not in ("pl", "ob"):
                continue
            item = QListWidgetItem(member["actor_name"])
            item.setData(Qt.ItemDataRole.UserRole, member["member_id"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.members.addItem(item)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("名称", self.name)
        form.addRow("频道组", self.group_choice)
        form.addRow("简介", self.description)
        layout.addLayout(form)
        layout.addWidget(label("参与者"))
        layout.addWidget(self.members, 1)
        self.feedback = label()
        layout.addWidget(self.feedback)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("创建频道")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_members(self) -> list[str]:
        return [self.members.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.members.count())
                if self.members.item(index).checkState() == Qt.CheckState.Checked]

    def accept(self) -> None:
        if not self.name.text().strip():
            self.feedback.setText("请填写频道名称。")
        elif len(self.description.toPlainText()) > 4000:
            self.feedback.setText("频道简介最多 4,000 个字符。")
        else:
            super().accept()


class CurrentPageStack(QStackedWidget):
    """只由当前页面决定窗口最小尺寸，避免工作台撑大选团页。"""
    def __init__(self) -> None:
        super().__init__()
        self.currentChanged.connect(lambda _: self.updateGeometry())

    def sizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.sizeHint() if page is not None else QSize()

    def minimumSizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else QSize()


class HostWindow(QMainWindow):
    rulesetSelected = Signal(str)

    def __init__(self, data_root: Path) -> None:
        super().__init__()
        self.resize(760, 400 + BAR_HEIGHT)
        self.setMinimumSize(680, 360 + BAR_HEIGHT)
        self.setStyleSheet(STYLE)
        self.data_root = data_root.resolve()
        self.library = CampaignLibrary(self.data_root)
        self._available_campaigns: list[dict] = []
        self._selector_geometry = None
        self._selector_was_maximized = False
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
        self._channel_revision = 0
        self._member_channels: dict[str, dict] = {}
        self._groups: list[dict] = []
        self._online_members: set[str] = set()
        self._member_editor: tuple[str, MemberDialog] | None = None
        self._pending_reads: dict[str, int] = {}
        self._channel_refresh_timer = QTimer(self)
        self._channel_refresh_timer.setSingleShot(True)
        self._channel_refresh_timer.timeout.connect(self._refresh_channel_status)
        self.server = ServerController(self)
        self.server.stateChanged.connect(self._network_state)
        self.server.errorOccurred.connect(self._report_error)
        self.server.logReceived.connect(self._append_log)
        self.server.sendConfirmed.connect(self._send_confirmed)
        self.server.sendFailed.connect(self._send_failed)
        self.server.passwordChanged.connect(self._reload_campaign)
        self.server.attributesChanged.connect(self._reload_campaign)
        self.server.channelsReceived.connect(self._channels_received)
        self.server.presenceReceived.connect(self._presence_changed)
        self.server.sendFailed.connect(self._channel_command_failed)

        central = QWidget()
        self.title_bar, self._content_layout = window_layout(self, central, "组织者登录")
        layout = self._content_layout
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(16)
        self.pages = CurrentPageStack()
        self.pages.addWidget(self._build_selector())
        self.pages.addWidget(self._build_lobby())
        layout.addWidget(self.pages, 1)
        self.feedback = FeedbackLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.hide()
        self.feedback.setWordWrap(True)
        self.feedback.setMinimumWidth(0)
        layout.addWidget(self.feedback)
        self.setCentralWidget(central)
        self._update_buttons()
        QTimer.singleShot(0, self.refresh_campaigns)

    def _build_selector(self) -> QWidget:
        page = QWidget()
        columns = QHBoxLayout(page)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(20)
        self.campaign_list = identity_list()
        self.campaign_list.setAccessibleName("选择团")
        self.campaign_list.currentItemChanged.connect(self._fill_selected_campaign)
        self.campaign_list.itemClicked.connect(self._fill_selected_campaign)
        columns.addWidget(self.campaign_list, 1)
        right = QVBoxLayout()
        right.setSpacing(14)
        new, new_layout = panel()
        self.new_name = QLineEdit()
        self.new_name.setMaxLength(200)
        self.new_name.textChanged.connect(self._campaign_name_changed)
        self.new_name.returnPressed.connect(self._select_campaign)
        form = QFormLayout()
        form.setSpacing(14)
        form.addRow("团名", self.new_name)
        self.rules_choice = QComboBox()
        self.rules_choice.addItem("coc7", "coc7")
        self.rules_choice.currentIndexChanged.connect(
            lambda _: self.rulesetSelected.emit(self.rules_choice.currentData()))
        form.addRow("规则", self.rules_choice)
        self.new_description = QPlainTextEdit()
        self.new_description.setAccessibleName("简介")
        self.new_description.setMinimumHeight(110)
        form.addRow("简介", self.new_description)
        new_layout.addLayout(form)
        right.addWidget(new, 1)
        buttons = QHBoxLayout()
        self.manage_button = QPushButton("团管理")
        self.manage_button.clicked.connect(self._manage_campaigns)
        self.open_button = QPushButton("进入")
        self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self._select_campaign)
        self.new_button = QPushButton("新建")
        self.new_button.clicked.connect(self._clear_campaign_form)
        for button in (self.manage_button, self.open_button, self.new_button):
            buttons.addWidget(button, 1)
        right.addLayout(buttons)
        self.new_panel = new
        columns.addLayout(right, 2)
        return page

    def _build_lobby(self) -> QWidget:
        page = QWidget()
        page.setObjectName("lobbyPage")
        columns = QHBoxLayout(page)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("lobbySidebar")
        left = QVBoxLayout(sidebar)
        left.setContentsMargins(16, 16, 16, 16)
        left.setSpacing(12)
        self.network_status = ElidedButton("连接状态 · 关闭")
        self.network_status.clicked.connect(self._open_connection)
        left.addWidget(self.network_status)
        left.addSpacing(16)
        self.create_group_button = QPushButton("新建频道组")
        self.create_group_button.clicked.connect(self._create_group)
        left.addWidget(self.create_group_button)
        self.channel_management_button = QPushButton("频道管理")
        self.channel_management_button.clicked.connect(self._open_channel_management)
        left.addWidget(self.channel_management_button)
        self.channel_choice = ChannelTree()
        self.channel_choice.currentIndexChanged.connect(self._select_host_channel)
        self.channel_choice.createRequested.connect(self._create_scene)
        left.addWidget(self.channel_choice, 1)
        self.dice_management_button = QPushButton("掷骰管理")
        self.dice_management_button.clicked.connect(self._open_dice_management)
        left.addWidget(self.dice_management_button)
        self.participants_button = QPushButton("参与者管理")
        self.participants_button.clicked.connect(self._open_participants)
        left.addWidget(self.participants_button)
        self.logs_button = QPushButton("日志")
        self.logs_button.clicked.connect(self._open_logs)
        left.addWidget(self.logs_button)
        self.network_button = QPushButton("开启监听")
        self.network_button.setObjectName("primary")
        self.network_button.clicked.connect(self._toggle_network)
        left.addWidget(self.network_button)
        chat_panel = QWidget()
        right = QVBoxLayout(chat_panel)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.channel_header = ChannelHeader(can_archive=True)
        self.channel_title = self.channel_header.title
        self.channel_info_button = self.channel_header.description_button
        self.channel_info_button.clicked.connect(self._open_channel_info)
        self.channel_members_button = self.channel_header.members_button
        self.channel_members_button.clicked.connect(self._open_channel_members)
        self.channel_log_button = self.channel_header.history_button
        self.channel_log_button.clicked.connect(self._open_channel_history)
        self.archive_scene_button = self.channel_header.archive_button
        self.archive_scene_button.clicked.connect(self._archive_scene)
        right.addWidget(self.channel_header)
        self.messages = ChatHistory(conversation=True)
        self.messages.olderRequested.connect(self._load_older_history)
        self.messages.setPlaceholderText("暂无消息")
        self.server.messageReceived.connect(self._receive_message)
        right.addWidget(self.messages, 1)
        self.message_input = ChatComposer()
        self.message_input.setPlaceholderText("以 KP 身份发言")
        self.message_input.sendRequested.connect(self._send_message)
        composer = QVBoxLayout()
        composer.setContentsMargins(12, 2, 12, 12)
        composer.addWidget(self.message_input)
        right.addLayout(composer)
        self.channel_splitter = split_channel_view(sidebar, chat_panel)
        columns.addWidget(self.channel_splitter)

        self.connection_dialog = ConnectionDialog(self)
        self.listen_host = self.connection_dialog.listen_host
        self.listen_port = self.connection_dialog.listen_port
        self.connection_address = self.connection_dialog.address
        self.listen_host.textChanged.connect(self._update_address)
        self.listen_port.valueChanged.connect(self._update_address)
        self._update_address()
        self.participants_dialog = ParticipantsDialog(self)
        self.member_table = self.participants_dialog.members
        self.member_table.itemSelectionChanged.connect(self._update_buttons)
        self.members_refresh = self.participants_dialog.refresh
        self.members_refresh.clicked.connect(self._reload_campaign)
        self.add_button = self.participants_dialog.add
        self.add_button.clicked.connect(self._add_member)
        self.edit_button = self.participants_dialog.edit
        self.edit_button.clicked.connect(self._edit_member)
        self.runtime_log_dialog = RuntimeLogDialog(self)
        self.logs = self.runtime_log_dialog.logs
        self.channel_management_dialog = ChannelManagementDialog(self)
        self._history_dialogs = {}
        self._members_dialogs = {}
        return page

    def _run(self, operation: Callable, on_result: Callable) -> None:
        if self._busy:
            return
        self._busy = True
        self._on_result = on_result
        self.feedback.setText("正在处理…")
        self.feedback.setStyleSheet(f"color: {MUTED};")
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
        elif self._returning and not self.server.active and not self._busy:
            self._return_to_selector()
        elif self._refresh_pending and not self._closing:
            self._reload_campaign()

    def _report_error(self, message: str) -> None:
        self.feedback.setText(message)
        self.feedback.setStyleSheet(f"color: {DANGER};")
        self._append_log(message)

    def _append_log(self, message: str) -> None:
        self.logs.appendPlainText(message)

    @staticmethod
    def _show_dialog(dialog: QDialog) -> None:
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _open_connection(self) -> None:
        self._update_channel_windows()
        self._show_dialog(self.connection_dialog)

    def _open_participants(self) -> None:
        self._show_dialog(self.participants_dialog)

    def _open_dice_management(self) -> None:
        if self.campaign is None:
            return
        directory = self.campaign["directory"]
        try:
            config = local_dice_config(directory)
        except (sqlite3.Error, OSError, ValueError) as error:
            self._report_error(str(error))
            return

        def save(updated) -> None:
            try:
                save_local_dice_config(directory, updated)
            except sqlite3.Error as error:
                raise ValueError("无法保存骰娘设置，请稍后重试。") from error

        dialog = DiceManagementDialog(self, config, save)
        dialog.exec()
        dialog.deleteLater()

    def _open_logs(self) -> None:
        self._show_dialog(self.runtime_log_dialog)

    def _open_channel_management(self) -> None:
        self.channel_management_dialog.populate(self._channels, self._groups, self._channel_revision, force=True)
        self._show_dialog(self.channel_management_dialog)

    def _open_channel_info(self) -> None:
        channel = self._channels.get(self._channel_id)
        if channel is None:
            return
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(f"{channel['name']} · 频道简介")
        dialog.resize(520, 320)
        content = QPlainTextEdit(channel.get("description", ""))
        content.setReadOnly(True)
        content.setPlaceholderText("暂无简介")
        layout = QVBoxLayout(dialog)
        layout.addWidget(content)
        self._show_dialog(dialog)

    def _open_channel_history(self) -> None:
        channel = self._channels.get(self._channel_id)
        if channel is None:
            return
        dialog = self._history_dialogs.get(self._channel_id)
        if dialog is None:
            dialog = ChannelHistoryDialog(self, self.campaign["directory"], channel)
            self._history_dialogs[self._channel_id] = dialog
        self._show_dialog(dialog)
        if not dialog.loaded:
            dialog.load()

    def _open_channel_members(self) -> None:
        channel = self._channels.get(self._channel_id)
        if channel is None:
            return
        dialog = self._members_dialogs.get(self._channel_id)
        if dialog is None:
            dialog = ChannelMembersDialog(self, channel)
            self._members_dialogs[self._channel_id] = dialog
        self._update_channel_windows()
        self._show_dialog(dialog)

    def _presence_changed(self, members: list[str]) -> None:
        self._online_members = set(members)
        self._update_channel_windows()

    def _update_channel_windows(self) -> None:
        if self.campaign is None:
            return
        members = self.campaign["members"]
        self.connection_dialog.update_members(members, self._online_members)
        for key, dialog in self._members_dialogs.items():
            channel = self._channels.get(key)
            if channel is None:
                continue
            selected = set(channel.get("member_ids", []))
            participants = [member for member in members if member["role"] == "ob" or
                            (member["role"] == "pl" and (channel["kind"] == "lobby" or member["member_id"] in selected))]
            dialog.setWindowTitle(f"{channel['name']} · 频道成员")
            dialog.populate(participants)
        for key, dialog in self._history_dialogs.items():
            if key in self._channels:
                dialog.setWindowTitle(f"{self._channels[key]['name']} · 完整聊天记录")

    def refresh_campaigns(self) -> None:
        def populated(result: tuple) -> None:
            campaigns, errors = result
            self._available_campaigns = campaigns
            previous = self.campaign_list.currentItem()
            selected = previous.data(Qt.ItemDataRole.UserRole) if previous is not None else None
            self.campaign_list.blockSignals(True)
            self.campaign_list.clear()
            for campaign in campaigns:
                if not campaign["hidden"]:
                    add_campaign_card(self.campaign_list, campaign)
                    if campaign["directory"] == selected:
                        self.campaign_list.setCurrentRow(self.campaign_list.count() - 1)
            if self.campaign_list.currentRow() < 0 and self.campaign_list.count():
                self.campaign_list.setCurrentRow(0)
            self.campaign_list.blockSignals(False)
            if self.campaign_list.currentItem() is not None:
                self._fill_selected_campaign()
            else:
                self._clear_campaign_form()
            if errors:
                self._report_error("部分团读取失败，" + "；".join(errors))

        self._run(self.library.entries, populated)

    def _matching_campaign(self) -> dict | None:
        name = self.new_name.text().strip()
        return next((campaign for campaign in self._available_campaigns if campaign["name"] == name), None)

    def _fill_selected_campaign(self, *_args: object) -> None:
        item = self.campaign_list.currentItem()
        if item is None:
            return
        directory = item.data(Qt.ItemDataRole.UserRole)
        campaign = next((value for value in self._available_campaigns if value["directory"] == directory), None)
        if campaign is not None:
            self.new_name.blockSignals(True)
            self.new_name.setText(campaign["name"])
            self.new_name.blockSignals(False)
            self._fill_campaign_fields(campaign)
            self._update_buttons()

    def _fill_campaign_fields(self, campaign: dict) -> None:
        self.new_description.setPlainText(campaign["description"])
        ruleset = campaign["ruleset"]
        index = self.rules_choice.findData(ruleset)
        if index < 0:
            self.rules_choice.addItem(ruleset, ruleset)
            index = self.rules_choice.count() - 1
        self.rules_choice.setCurrentIndex(index)

    def _campaign_name_changed(self, *_args: object) -> None:
        campaign = self._matching_campaign()
        self.campaign_list.blockSignals(True)
        self.campaign_list.setCurrentRow(-1)
        self.campaign_list.clearSelection()
        if campaign is not None:
            for row in range(self.campaign_list.count()):
                if self.campaign_list.item(row).data(Qt.ItemDataRole.UserRole) == campaign["directory"]:
                    self.campaign_list.setCurrentRow(row)
                    break
            self._fill_campaign_fields(campaign)
        self.campaign_list.blockSignals(False)
        self._update_buttons()

    def _clear_campaign_form(self) -> None:
        self.campaign_list.blockSignals(True)
        self.campaign_list.setCurrentRow(-1)
        self.campaign_list.clearSelection()
        self.campaign_list.blockSignals(False)
        self.new_name.blockSignals(True)
        self.new_name.clear()
        self.new_name.blockSignals(False)
        self.new_description.clear()
        self.rules_choice.setCurrentIndex(self.rules_choice.findData("coc7"))
        self.feedback.clear()
        self.new_name.setFocus()
        self._update_buttons()

    def _manage_campaigns(self) -> None:
        def show_management(result: tuple) -> None:
            campaigns, errors = result
            dialog = CampaignManagementDialog(self, self.library, campaigns)
            if errors:
                dialog.feedback.setText("；".join(errors))
            dialog.exec()
            if dialog.changed:
                self.refresh_campaigns()
            dialog.deleteLater()

        self._run(self.library.entries, show_management)

    def _select_campaign(self, *_args: object) -> None:
        if self._busy or self._closing:
            return
        name = self.new_name.text()
        description = self.new_description.toPlainText()

        def enter() -> tuple[dict, dict]:
            directory = self.library.enter(name, description)
            return self._read_campaign(directory)

        self._run(enter, self._enter_campaign)

    @staticmethod
    def _read_campaign(directory: Path) -> tuple[dict, dict]:
        campaign = campaign_details(directory)
        campaign["channel_state"] = local_channel_state(directory)
        return campaign, local_lobby_history(directory)

    def _enter_campaign(self, result: tuple[dict, dict]) -> None:
        self._selector_geometry = self.geometry()
        self._selector_was_maximized = self.isMaximized()
        campaign, history = result
        self.campaign = campaign
        self._queued_channel_state = None
        self._channel_revision = 0
        self.messages.clear()
        self._pending_reads.clear()
        self._channel_refresh_timer.stop()
        self._channel_drafts.clear()
        self._channel_id = campaign["lobby_id"]
        self._populate_channels(campaign["channel_state"])
        self._show_history(history)
        self._retry_send = None
        self.message_input.clear()
        self.pages.setCurrentIndex(1)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self.title_bar.set_caption(str(campaign["name"]), "KP")
        self._populate_members()
        self._update_channel_windows()
        self.centralWidget().layout().activate()
        minimum = self.minimumSizeHint().expandedTo(QSize(1000, 620))
        target = self.size().expandedTo(QSize(1280, 800))
        screen = self.screen()
        if screen is not None:
            limit = screen.availableGeometry().size() - (self.frameGeometry().size() - self.size())
            minimum = minimum.boundedTo(limit)
            target = target.boundedTo(limit)
        self.setMinimumSize(minimum)
        if not self.isMaximized():
            self.resize(target.expandedTo(minimum))

    def _show_history(self, page: dict) -> None:
        self.messages.add_messages(page["messages"])
        self._history_has_more = page["has_more"]
        self._queue_current_read()

    def _load_older_history(self) -> None:
        if self._busy:
            self.messages.set_pagination(self._history_has_more)
            return
        before = self.messages.oldest_sequence
        if self.campaign is not None and self._history_has_more and before is not None:
            directory = self.campaign["directory"]
            channel_id = self._channel_id
            self._run(lambda: local_lobby_history(directory, before=before, channel_id=channel_id), self._show_history)

    def _populate_channels(self, state: dict) -> None:
        if self.campaign is None or self._returning:
            return
        if not any(item["channel_id"] == self.campaign["lobby_id"] for item in state["channels"]):
            return
        self._channels = {item["channel_id"]: item for item in state["channels"]}
        self._groups = state.get("groups", [])
        self._visibility_mode = state["visibility_mode"]
        self._channel_revision = state["revision"]
        self._member_channels = {item["member_id"]: item for item in state.get("members", [])}
        selected = self._channel_id
        if self._next_channel_id in self._channels:
            selected = self._next_channel_id
            self._next_channel_id = None
        elif self._channel_request is None:
            self._next_channel_id = None
        if selected not in self._channels:
            selected = state["current_channel_id"]
        self.channel_choice.blockSignals(True)
        self.channel_choice.set_channels(self._groups, state["channels"], selected)
        self.channel_choice.blockSignals(False)
        if selected != self._channel_id:
            self._select_host_channel(0)
        self.channel_management_dialog.populate(self._channels, self._groups, self._channel_revision)
        self._update_channel_windows()
        self._queue_current_read()
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
        dialog = self._history_dialogs.get(event.get("channel_id"))
        if dialog is not None:
            if dialog.loaded:
                dialog.history.add_message(event)
            else:
                dialog.pending_live.append(event)
        if event.get("channel_id") == self._channel_id:
            self.messages.add_message(event)
            self._queue_current_read()
        self._schedule_channel_refresh()

    def _queue_current_read(self, *_args: object) -> None:
        if self.campaign is None:
            return
        sequence = self.messages.newest_public_sequence
        known = self._channels.get(self._channel_id, {}).get("read_sequence", 0)
        if sequence > max(known, self._pending_reads.get(self._channel_id, 0)):
            self._pending_reads[self._channel_id] = sequence
            self._schedule_channel_refresh()

    def _schedule_channel_refresh(self) -> None:
        if self.campaign is not None and not self._closing and not self._channel_refresh_timer.isActive():
            self._channel_refresh_timer.start(100)

    def _refresh_channel_status(self) -> None:
        if self.campaign is None or self._closing or self._returning:
            return
        if (self._busy or self._channel_request is not None or QApplication.activeModalWidget() is not None
                or self.server.state in ("starting", "stopping")):
            self._schedule_channel_refresh()
            return
        reads, self._pending_reads = self._pending_reads, {}
        if self.server.state == "running":
            for channel_id, sequence in reads.items():
                self.server.command("channel.read", channel_id=channel_id, sequence=sequence)
            if not reads:
                self.server.command("channels")
            return
        if not reads:
            return
        directory = self.campaign["directory"]

        def refresh() -> dict:
            for channel_id, sequence in reads.items():
                mark_channel_read(directory, channel_id, sequence)
            return local_channel_state(directory)

        self._run(refresh, self._populate_channels)

    def _flush_pending_reads(self, on_result: Callable) -> bool:
        if not self._pending_reads or self.campaign is None:
            return False
        reads, self._pending_reads = self._pending_reads, {}
        directory = self.campaign["directory"]
        self._channel_refresh_timer.stop()

        def save() -> None:
            for channel_id, sequence in reads.items():
                mark_channel_read(directory, channel_id, sequence)

        self._run(save, on_result)
        return True

    def _create_scene(self, group_id: str | None = None) -> None:
        if self.campaign is None or self._busy or self._channel_request is not None:
            return
        channel = self._channels.get(self._channel_id, {})
        dialog = SceneDialog(self, self.campaign["members"], self._groups,
                             group_id or channel.get("group_id"))
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        name, members = dialog.name.text(), dialog.selected_members()
        group, description = dialog.group_choice.currentData(), dialog.description.toPlainText()
        dialog.deleteLater()
        if not accepted:
            return
        channel_id = str(uuid4())
        self._next_channel_id = channel_id
        self._channel_operation("scene.create", name=name, member_ids=members, channel_id=channel_id,
                                group_id=group, description=description)

    def _create_group(self) -> None:
        name, accepted = QInputDialog.getText(self, "新建频道组", "名称")
        if accepted:
            self._channel_operation("group.create", name=name, group_id=str(uuid4()))

    def _archive_scene(self) -> None:
        channel = self._channels.get(self._channel_id)
        if channel is None or channel["kind"] != "scene" or channel["status"] != "active":
            return
        if QMessageBox.question(self, "结束频道", f"结束「{channel['name']}」并将其归档为只读？") != QMessageBox.StandardButton.Yes:
            return
        self._channel_operation("scene.archive", channel_id=self._channel_id)

    def _channel_operation(self, operation: str, **values: object) -> None:
        request_id = str(uuid4())
        if self.server.state == "running":
            if self.server.command(operation, request_id=request_id, **values):
                self._channel_request = request_id
                self.feedback.setText("正在更新频道…")
                self._update_buttons()
            return
        directory = self.campaign["directory"]

        values["request_id"] = request_id
        self._run(lambda: execute_channel_operation(directory, operation, values), self._populate_channels)

    def _channels_received(self, request_id: str, state: dict) -> None:
        if self.campaign is None or self._returning:
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
            values = [member["role"].upper(), member["actor_name"], password]
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
        dialog = MemberDialog(self.participants_dialog)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            actor = dialog.actor.text()
            role = dialog.role.currentData()
            password = dialog.password.text() if role == "ob" else "123456"
            directory = self.campaign["directory"]

            def add() -> dict:
                add_member(directory, actor, role=role, password=password)
                return campaign_details(directory)

            self._run(add, self._members_updated)

    def _members_updated(self, campaign: dict) -> None:
        self.campaign = campaign
        if self._member_editor is not None:
            member_id, editor = self._member_editor
            member = next((item for item in campaign["members"] if item["member_id"] == member_id), {})
            editor.attributes_button.setEnabled(bool(member.get("attributes_imported")))
        self._populate_members()
        self._update_channel_windows()

    def _edit_member(self) -> None:
        member = self._selected_member()
        if member is None:
            return
        dialog = MemberDialog(self.participants_dialog, member)
        if dialog.attributes_button is not None:
            self._member_editor = (member["member_id"], dialog)
            dialog.attributes_button.clicked.connect(lambda: self._view_member_attributes(dialog, member["member_id"]))
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self._member_editor = None
        if accepted:
            actor = dialog.actor.text()
            directory = self.campaign["directory"]

            def edit() -> dict:
                edit_member(directory, member["member_id"], actor)
                return campaign_details(directory)

            self._run(edit, self._members_updated)
        dialog.deleteLater()

    def _view_member_attributes(self, editor: MemberDialog, member_id: str) -> None:
        directory = self.campaign["directory"]
        try:
            sheet = local_attributes(directory, member_id)
        except (sqlite3.Error, ValueError) as error:
            editor.validation.setText(str(error))
            return
        if sheet["attributes"] is None:
            editor.attributes_button.setEnabled(False)
            return
        dialog = AttributeSheetDialog(editor, sheet["actor_name"], sheet["attributes"], organizer=True)

        def reset() -> None:
            try:
                reset_local_attributes(directory, member_id)
            except (sqlite3.Error, ValueError) as error:
                dialog.feedback.setText(str(error))
                dialog.feedback.show()
                return
            editor.attributes_button.setEnabled(False)
            if self.server.active:
                self.server.command("attributes.refresh", member_id=member_id)
            self._reload_campaign()
            dialog.accept()

        dialog.reset_button.clicked.connect(reset)
        dialog.exec()
        dialog.deleteLater()

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
        states = {"stopped": "关闭", "starting": "开启中…", "running": "开启", "stopping": "关闭中…"}
        self.network_status.setText(f"连接状态 · {states[state]}")
        self.connection_dialog.status.setText(f"连接状态：{states[state]}")
        self.network_button.setText("开启监听" if state == "stopped" else "关闭监听")
        if state == "running":
            self.server.command("channels")
        if state == "stopped":
            self._channel_request = None
            self._online_members.clear()
            self._update_channel_windows()
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
        if not self.message_input.send_available:
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
        if self._pending_reads:
            self._returning = True
            self._flush_pending_reads(lambda _: self._return_to_selector())
            return
        self._returning = False
        self.campaign = None
        self._queued_channel_state = None
        for dialog in self._history_dialogs.values():
            dialog.close()
            dialog.deleteLater()
        for dialog in self._members_dialogs.values():
            dialog.close()
            dialog.deleteLater()
        self._history_dialogs.clear()
        self._members_dialogs.clear()
        self._online_members.clear()
        self._channels.clear()
        self._channel_id = ""
        self._channel_drafts.clear()
        self._queued_channel_state = None
        self._next_channel_id = None
        self._member_channels.clear()
        self._pending_reads.clear()
        self._channel_refresh_timer.stop()
        self.pages.setCurrentIndex(0)
        self.title_bar.set_caption("组织者登录")
        self.setMinimumSize(680, 360 + BAR_HEIGHT)
        self.centralWidget().layout().activate()
        if self._selector_geometry is not None:
            if self._selector_was_maximized:
                self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
            else:
                self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMaximized)
                self.setGeometry(self._selector_geometry)
            self._selector_geometry = None
        self._content_layout.setContentsMargins(20, 18, 20, 14)
        self.refresh_campaigns()

    def _update_buttons(self, *args: object) -> None:
        available = not self._busy and not self._closing and self._channel_request is None
        name = self.new_name.text().strip()
        self.open_button.setText("新建并进入" if name and self._matching_campaign() is None else "进入")
        self.open_button.setEnabled(available and bool(name))
        self.new_panel.setEnabled(available)
        self.campaign_list.setEnabled(available)
        self.manage_button.setEnabled(available)
        self.new_button.setEnabled(available)
        self.add_button.setEnabled(available and self.campaign is not None)
        self.members_refresh.setEnabled(available)
        selected = self._selected_member()
        self.edit_button.setEnabled(available and selected is not None and selected["role"] in ("pl", "ob"))
        self.listen_host.setEnabled(not self.server.active)
        self.listen_port.setEnabled(not self.server.active)
        self.network_button.setEnabled(self.campaign is not None and self.server.state in ("stopped", "running") and not self._closing)
        channel = self._channels.get(self._channel_id, {})
        scene_available = available and self.campaign is not None and self._pending_send is None and self.server.state in ("stopped", "running")
        self.channel_choice.setEnabled(scene_available)
        self.create_group_button.setEnabled(scene_available)
        self.channel_management_button.setEnabled(scene_available)
        self.channel_management_dialog.save.setEnabled(scene_available and self.channel_management_dialog.selected_id() is not None)
        self.channel_header.set_channel(channel.get("name", ""), channel.get("description", ""))
        for button in (self.channel_info_button, self.channel_members_button, self.channel_log_button):
            button.setEnabled(scene_available and bool(channel))
        self.archive_scene_button.setEnabled(scene_available and channel.get("kind") == "scene" and channel.get("status") == "active")
        can_send = channel.get("can_send", False)
        self.message_input.setPlaceholderText("频道已归档" if channel.get("status") == "archived" else f"以 KP 身份发送到 {channel.get('name', '')}")
        self.message_input.setReadOnly(self._pending_send is not None or not can_send)
        self.message_input.send_available = available and self.server.state == "running" and self._pending_send is None and can_send
        self.messages.set_pagination(self._history_has_more, loading=not available)
        for dialog in self._history_dialogs.values():
            dialog.history.set_pagination(dialog.has_more, loading=not available)
        self.network_status.setEnabled(self.campaign is not None and not self._closing)
        self.participants_button.setEnabled(self.campaign is not None and not self._closing)
        self.dice_management_button.setEnabled(self.campaign is not None and not self._closing)
        self.logs_button.setEnabled(self.campaign is not None and not self._closing)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.server.active:
            self._closing = True
            self.server.stop()
            event.ignore()
        elif self._busy:
            self._closing = True
            self.feedback.setText("正在完成本地操作，完成后关闭。")
            event.ignore()
        else:
            self._closing = True
            self._channel_refresh_timer.stop()
            if self._flush_pending_reads(lambda _: None):
                event.ignore()
                return
            for dialog in self.findChildren(QDialog):
                dialog.close()
            event.accept()


def run_gui(data_root: Path) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Story Host")
    apply_theme(app)
    app.setFont(QFont("Microsoft YaHei UI", 10))
    window = HostWindow(data_root)
    window.show()
    return app.exec()
