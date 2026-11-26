import json
import sqlite3
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, QUrl, QUrlQuery, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtNetwork import QAbstractSocket, QNetworkProxy, QNetworkRequest
from PySide6.QtWebSockets import QWebSocket, QWebSocketProtocol
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from chat_protocol import (
    CHAT_ACK,
    CHAT_ERROR,
    CHAT_HISTORY,
    CHAT_HISTORY_GET,
    CHAT_MESSAGE,
    CHAT_SEND,
    CHANNEL_CHANGED,
    CHANNEL_STATE_GET,
    HISTORY_PAGE_SIZE,
    LOBBY_PATH,
    PING_INTERVAL_SECONDS,
    PONG_TIMEOUT_SECONDS,
    SEND_ERRORS,
    chat_text_error,
    decode_event,
)

from .auth_client import AuthClient, ClientSession, server_urls
from .channel_header import ChannelHeader
from .chat_composer import ChatComposer
from .chat_history import ChatHistory
from .history_cache import HistoryCache, is_saved_message
from .channel_tree import ChannelTree
from .participant_sidebar import ParticipantSidebar
from .ui_style import CHANNEL_NAV_STYLE, STYLE
from .ui_widgets import split_channel_view
from .host_monitor import HostMonitor
from .lobby_dialogs import ChannelLogDialog, ChannelMembersDialog

SocketState = QAbstractSocket.SocketState

DEFAULT_ADDRESS = f"ws://127.0.0.1:8000{LOBBY_PATH}"
CONNECT_TIMEOUT_MS = 10_000
# 在约两分钟的短时断网后仍有机会自动恢复，单次等待最多 30 秒。
RECONNECT_DELAYS_MS = (1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000, 30_000, 30_000)
STATE_LABELS = {
    SocketState.HostLookupState: "正在解析主机地址…",
    SocketState.ConnectingState: "正在连接…",
    SocketState.ConnectedState: "已连接",
    SocketState.ClosingState: "正在断开…",
    SocketState.UnconnectedState: "未连接",
}


class FeedbackLabel(QLabel):
    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))

    def clear(self):
        super().clear()
        self.hide()


class LobbyWindow(QWidget):
    """带频道权限、历史同步和草稿隔离的聊天窗口。"""

    sessionRejected = Signal()
    loginRequested = Signal()
    personalInfoRequested = Signal()
    attributesStatusChanged = Signal()

    def __init__(self, *, session: ClientSession | None = None, address: str = DEFAULT_ADDRESS,
                 cache: HistoryCache | None = None, offline_actor: str = "") -> None:
        super().__init__()
        self.session = session
        self._managed = session is not None or bool(offline_actor)
        self._cache = cache
        self._base_cache = cache
        self._channels: dict[str, dict] = {}
        self._unread_channels: set[str] = set()
        self._channel_id = ""
        self._history_has_more = False
        self._host_open = True if session is not None else None
        self._host_checking = session is None
        self._host_monitor = None
        self._offline_login_pending = False
        self._log_dialogs = {}
        self._members_dialogs = {}
        self._info_dialogs = {}
        self._log_requests = {}
        self._assigned_channel_id = ""
        self._channel_drafts: dict[str, tuple[str, tuple[str, str] | None]] = {}
        self._channel_state_ready = False
        self.attributes_imported: bool | None = None
        self._channel_timer = QTimer(self)
        self._channel_timer.setSingleShot(True)
        self._channel_timer.timeout.connect(self._channel_state_timed_out)
        self._history_request_id = ""
        self._history_request: tuple[str, int] | None = None
        self._live_during_sync: list[dict] | None = None
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.timeout.connect(self._history_timed_out)
        self._connection_active = False
        self._opening = False
        self._connect_timer = QTimer(self)
        self._connect_timer.setSingleShot(True)
        self._connect_timer.timeout.connect(self._connection_timed_out)
        self._retry_count = 0
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._connect_socket)
        self._ping_payload: bytes | None = None
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setSingleShot(True)
        self._heartbeat_timer.timeout.connect(self._heartbeat)
        self._session_check = AuthClient(self) if self._managed else None
        if self._session_check is not None:
            self._session_check.failed.connect(self._session_check_failed)
        self.setWindowTitle("Story · 大厅")
        self.resize(640, 440)

        self.socket = QWebSocket(parent=self)
        # 直连测试主机，避免系统代理接管局域网连接。
        self.socket.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.address = QLineEdit(address)
        self.nickname = QLineEdit()
        self.nickname.setPlaceholderText("输入临时昵称")
        if not self._managed:
            self.connect_button = QPushButton("连接")
            self.disconnect_button = QPushButton("断开")
            self.connection_status = QLabel()
        self.channel_choice = ChannelTree(managed=False) if self._managed else QComboBox()
        if not self._managed:
            self.channel_choice.hide()
        self.channel_choice.currentIndexChanged.connect(self._select_channel)
        self.feedback = FeedbackLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.chat_history = ChatHistory(conversation=self._managed, fade_top=self._managed)
        self.chat_history.setPlaceholderText("暂无可查看的消息")
        self.chat_history.olderRequested.connect(self._load_older)
        self.message = ChatComposer() if self._managed else QPlainTextEdit()
        self.message.setPlaceholderText("输入消息")
        if not self._managed:
            self.message.setFixedHeight(80)
            self.send_button = QPushButton("发送")
            self.send_button.setObjectName("primary")
        self._awaiting_send = False
        self.retry_send: tuple[str, str] | None = None
        if self._managed:
            self.nickname.setText(session.actor_name if session is not None else offline_actor)
            self.nickname.setReadOnly(True)
            self.address.setReadOnly(True)


        self._build_layout()
        self._connect_signals()
        self._update_connection_state(self.socket.state())
        if self._cache is not None:
            try:
                state = self._base_cache.channel_state()
                if state is not None:
                    self._apply_channel_state(state, online=False)
                else:
                    self._show_cached_page()
            except (OSError, sqlite3.Error, ValueError):
                self.feedback.setText("无法读取本地频道信息。")
        if self._managed:
            campaign_id = session.campaign_id if session is not None else None
            if campaign_id is None and self._base_cache is not None:
                campaign_id = self._base_cache.scope[0]
            self._host_monitor = HostMonitor(address, campaign_id, self)
            self._host_monitor.changed.connect(self._host_state_changed)
            self._host_monitor.start()

    def resume(self, session: ClientSession, cache: HistoryCache) -> None:
        self.session = session
        if self._host_monitor is not None:
            self._host_monitor.campaign_id = session.campaign_id
        self.nickname.setText(session.actor_name)
        if self._cache is None:
            self._base_cache = cache
            self._cache = cache
            self._show_cached_page()
        self._open_connection()

    def _build_layout(self) -> None:
        if not self._managed:
            self.setStyleSheet(STYLE)
            connection_row = QHBoxLayout()
            for field in (self.address, self.nickname, self.connect_button, self.disconnect_button):
                connection_row.addWidget(field)
            layout = QVBoxLayout(self)
            layout.addLayout(connection_row)
            layout.addWidget(self.connection_status)
            layout.addWidget(self.feedback)
            self.history_label = QLabel("大厅消息")
            layout.addWidget(self.history_label)
            layout.addWidget(self.chat_history, 1)
            layout.addWidget(self.message)
            layout.addWidget(self.send_button)
            return
        self.setObjectName("channelWorkspace")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(STYLE + CHANNEL_NAV_STYLE)
        self.address.setParent(self)
        self.address.hide()
        self.nickname.setParent(self)
        self.nickname.hide()
        columns = QHBoxLayout(self)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(0)
        self._sidebar = ParticipantSidebar(self.channel_choice, self)
        self.connection_button = self._sidebar.connection_button
        self.connect_button = self.disconnect_button = self.connection_button
        self.personal_info_button = self._sidebar.personal_button
        self.personal_info_button.clicked.connect(lambda _checked=False: self.personalInfoRequested.emit())
        chat_panel = QFrame()
        chat_panel.setObjectName("channelChatPanel")
        right = QVBoxLayout(chat_panel)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.channel_header = ChannelHeader(divider=False)
        self.channel_info_button = self.channel_header.description_button
        self.channel_info_button.clicked.connect(self._open_channel_info)
        self.channel_members_button = self.channel_header.members_button
        self.channel_members_button.clicked.connect(self._open_channel_members)
        self.channel_log_button = self.channel_header.history_button
        self.channel_log_button.clicked.connect(self._open_channel_log)
        right.addWidget(self.channel_header)
        right.addWidget(self.chat_history, 1)
        composer = QVBoxLayout()
        composer.setContentsMargins(12, 2, 12, 12)
        composer.setSpacing(6)
        self.feedback.hide()
        composer.addWidget(self.feedback)
        composer.addWidget(self.message)
        right.addLayout(composer)
        self.channel_splitter = split_channel_view(self._sidebar, chat_panel)
        columns.addWidget(self.channel_splitter)

    def _connect_signals(self) -> None:
        if self._managed:
            self._sidebar.server_status.clicked.connect(self._refresh_server_status)
            self.connection_button.clicked.connect(self._toggle_connection)
            self.message.sendRequested.connect(self._send_message)
        else:
            self.connect_button.clicked.connect(self._open_connection)
            self.disconnect_button.clicked.connect(self._close_connection)
            self.send_button.clicked.connect(self._send_message)
        self.address.returnPressed.connect(self._open_connection)
        self.nickname.returnPressed.connect(self._open_connection)
        self.socket.stateChanged.connect(self._update_connection_state)
        self.socket.connected.connect(self._connected)
        self.socket.textMessageReceived.connect(self._show_message)
        self.socket.errorOccurred.connect(self._show_socket_error)
        self.socket.pong.connect(self._pong_received)

    @property
    def is_online(self):
        return self.socket.state() == SocketState.ConnectedState and self._channel_state_ready

    def _toggle_connection(self):
        if self._reconnect_timer.isActive() or self.socket.state() != SocketState.UnconnectedState:
            self._close_connection()
        else:
            self._open_connection()

    @Slot()
    def _refresh_server_status(self) -> None:
        self._host_checking = True
        self._update_server_status()
        self._sidebar.show_server_status()
        self._host_monitor.check()

    def _host_state_changed(self, state: bool | None) -> None:
        self._host_checking = False
        self._host_open = state
        self._update_server_status()

    def _update_server_status(self) -> None:
        opened = self._host_open
        if opened is None and self.socket.state() == SocketState.ConnectedState:
            opened = True
        self._sidebar.set_server_state(opened, checking=self._host_checking)

    @staticmethod
    def _show_dialog(dialog):
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _refresh_channel_info(self):
        if self.is_online:
            self.socket.sendTextMessage(json.dumps({"type": CHANNEL_STATE_GET}))

    def _open_channel_info(self):
        if self._channel_id not in self._channels:
            return
        dialog = self._info_dialogs.get(self._channel_id)
        if dialog is None:
            dialog = QDialog(self)
            dialog.resize(520, 320)
            dialog.content = QPlainTextEdit()
            dialog.content.setReadOnly(True)
            dialog.content.setPlaceholderText("暂无本地简介")
            layout = QVBoxLayout(dialog)
            layout.addWidget(dialog.content)
            self._info_dialogs[self._channel_id] = dialog
        self._update_auxiliary_windows()
        self._show_dialog(dialog)
        self._refresh_channel_info()

    def _open_channel_members(self):
        if self._channel_id not in self._channels:
            return
        dialog = self._members_dialogs.get(self._channel_id)
        if dialog is None:
            dialog = ChannelMembersDialog(self, self._channel_id)
            self._members_dialogs[self._channel_id] = dialog
        self._update_auxiliary_windows()
        self._show_dialog(dialog)
        self._refresh_channel_info()

    def _open_channel_log(self):
        if self._channel_id not in self._channels:
            return
        dialog = self._log_dialogs.get(self._channel_id)
        if dialog is None:
            dialog = ChannelLogDialog(self, self._channel_id)
            self._log_dialogs[self._channel_id] = dialog
        self._update_auxiliary_windows()
        self._show_dialog(dialog)
        dialog.load()

    def _update_auxiliary_windows(self):
        if not self._managed:
            return
        for mapping in (self._info_dialogs, self._members_dialogs, self._log_dialogs):
            for key, dialog in list(mapping.items()):
                channel = self._channels.get(key)
                if channel is None:
                    if mapping is self._log_dialogs:
                        self._cancel_log_requests(dialog)
                        dialog.timer.stop()
                        dialog.history.clear()
                    dialog.close()
                    dialog.deleteLater()
                    del mapping[key]
                    continue
                if mapping is self._info_dialogs:
                    dialog.setWindowTitle(channel["name"] + " · 频道简介" + ("" if self.is_online else " · 本地资料"))
                    dialog.content.setPlainText(channel.get("description", ""))
                elif mapping is self._members_dialogs:
                    dialog.update_channel(channel, self.is_online)
                else:
                    dialog.setWindowTitle(channel["name"] + (" · 聊天记录" if self.is_online else " · 本地聊天记录"))
        for button in (self.channel_info_button, self.channel_members_button, self.channel_log_button):
            button.setEnabled(self._channel_id in self._channels)

    def _cancel_log_requests(self, dialog):
        self._log_requests = {key: value for key, value in self._log_requests.items() if value[0] is not dialog}

    def _request_log_history(self, dialog, before):
        request_id = str(uuid4())
        mode, cursor = ("before", before) if before is not None else ("latest", 0)
        self._log_requests[request_id] = (dialog, mode, cursor)
        payload = {"type": CHAT_HISTORY_GET, "channel_id": dialog.channel_id,
                   "request_id": request_id, "mode": mode, "cursor": cursor}
        if self.socket.sendTextMessage(json.dumps(payload)) <= 0:
            dialog.fail("无法请求聊天记录。")
        else:
            dialog.timer.start(5000)

    def _receive_log_history(self, event):
        dialog, mode, cursor = self._log_requests.get(event["request_id"])
        if event.get("channel_id") != dialog.channel_id or (event.get("mode"), event.get("cursor")) != (mode, cursor):
            return
        page = event.get("messages")
        if (dialog.channel_id not in self._channels or not isinstance(page, list) or len(page) > HISTORY_PAGE_SIZE
                or not all(is_saved_message(item) and item.get("channel_id") == dialog.channel_id for item in page)):
            dialog.fail("无法读取该频道记录。")
            return
        sequences = [item["sequence"] for item in page]
        if sequences != sorted(set(sequences)) or (mode == "before" and sequences and sequences[-1] >= cursor):
            dialog.fail("聊天记录顺序无效。")
            return
        cache = dialog.cache()
        if cache is not None:
            try:
                cache.save(page)
            except (OSError, sqlite3.Error):
                self.feedback.setText("本地记录保存失败。")
        dialog.apply(page, event.get("has_more") is True, replace=mode == "latest")

    @Slot()
    def _open_connection(self) -> None:
        if not self.connect_button.isEnabled():
            return
        if self.session is None and self._managed:
            self.loginRequested.emit()
            return
        self._retry_count = 0
        self.feedback.clear()
        self._connect_socket()

    def _connect_socket(self) -> None:
        address = QUrl(self.address.text().strip(), QUrl.ParsingMode.StrictMode)
        if (
            not address.isValid()
            or address.scheme() not in {"ws", "wss"}
            or not address.host()
        ):
            self.feedback.setText("地址无效，请输入完整的 WebSocket 地址。")
            return

        if self.session is not None:
            request = QNetworkRequest(address)
            request.setRawHeader(b"Authorization", f"Bearer {self.session.token}".encode("ascii"))
            self._connection_active = self._opening = True
            self._connect_timer.start(CONNECT_TIMEOUT_MS)
            self.socket.open(request)
        else:
            nickname = self.nickname.text().strip()
            if not nickname or len(nickname.splitlines()) != 1:
                self.feedback.setText("请输入单行昵称。")
                return
            query = QUrlQuery(address)
            query.removeAllQueryItems("nickname")
            encoded_nickname = QUrl.toPercentEncoding(nickname).data().decode("ascii")
            query.addQueryItem("nickname", encoded_nickname)
            address.setQuery(query)
            self.socket.open(address)

    @Slot()
    def _close_connection(self) -> None:
        self._connection_active = self._opening = False
        self._retry_count = 0
        self._reconnect_timer.stop()
        self._connect_timer.stop()
        self._stop_heartbeat()
        self._channel_timer.stop()
        if self._session_check is not None:
            self._session_check.cancel()
        if self.socket.state() == SocketState.ConnectedState:
            self.socket.close()
        else:
            self.socket.abort()
        self._update_connection_state(self.socket.state())

    def _connection_timed_out(self) -> None:
        self.feedback.setText("连接主机超时。")
        self.socket.abort()

    def _connection_lost(self) -> None:
        if not self._connection_active or self.socket.state() != SocketState.UnconnectedState:
            return
        was_opening = self._opening
        self._connection_active = self._opening = False
        self._connect_timer.stop()
        self._stop_heartbeat()
        if not was_opening and self.socket.closeCode() == QWebSocketProtocol.CloseCode.CloseCodePolicyViolated:
            self.sessionRejected.emit()
            return
        if was_opening:
            # 被拒绝的 WebSocket 握手没有关闭帧，用 HTTP 区分会话失效和网络故障。
            http, _ = server_urls(self.address.text())
            self._session_check.cancel()
            self._session_check.request(http, "session", token=self.session.token)
        if self._retry_count < len(RECONNECT_DELAYS_MS):
            delay = RECONNECT_DELAYS_MS[self._retry_count]
            self._retry_count += 1
            self._reconnect_timer.start(delay)
        else:
            message = "自动重连失败，可以点击左下角的连接图标再试。"
            if self.retry_send is not None:
                message += "未确认的消息已保留。"
            self.feedback.setText(message)
        self._update_connection_state(self.socket.state())

    def _session_check_failed(self, _operation: str, code: str, _message: str) -> None:
        if code == "invalid_session":
            self.sessionRejected.emit()

    def set_offline_login_pending(self, pending: bool) -> None:
        self._offline_login_pending = pending
        self._update_connection_button(self.socket.state())

    def _update_connection_button(self, state: SocketState) -> None:
        if self._offline_login_pending:
            self._sidebar.set_connection_state(False, "正在连接", "本机未连接 · 正在等待主机响应…")
            self.connection_button.setEnabled(False)
            return
        connected = state == SocketState.ConnectedState
        idle = state == SocketState.UnconnectedState
        waiting_to_retry = self._reconnect_timer.isActive()
        action = "取消重连" if waiting_to_retry else "断开连接" if not idle else "连接"
        status = "本机已连接" if connected else "本机未连接"
        if not connected:
            if waiting_to_retry or (self._opening and self._retry_count > 0):
                status += f" · 正在重连（{self._retry_count}/{len(RECONNECT_DELAYS_MS)}）…"
            elif not idle:
                status += f" · {STATE_LABELS.get(state, '正在连接…')}"
        self._sidebar.set_connection_state(connected, action, status)
        self.connection_button.setEnabled(state != SocketState.ClosingState)

    def _update_connection_state(self, state: SocketState) -> None:
        connected = state == SocketState.ConnectedState
        idle = state == SocketState.UnconnectedState

        waiting_to_retry = self._reconnect_timer.isActive()
        if self._managed:
            if connected:
                self._host_open = True
            self._update_server_status()
            self._update_connection_button(state)
        else:
            self.connection_status.setText(STATE_LABELS.get(state, "正在连接…"))
        self.address.setEnabled(idle)
        self.nickname.setEnabled(idle)
        if not self._managed:
            self.connect_button.setEnabled(idle and not self._connection_active and not waiting_to_retry)
            self.disconnect_button.setEnabled(waiting_to_retry or (not idle and state != SocketState.ClosingState))
        if not connected:
            for dialog in self._log_dialogs.values():
                if dialog.loading:
                    dialog.fail("连接已断开，可查看本地记录。")
            self._channel_state_ready = False
            self._channel_timer.stop()
            self._finish_history_request()
            self._finish_sync()
        if not connected and self._awaiting_send:
            self._awaiting_send = False
            self.feedback.setText("连接已断开，消息未确认，草稿已保留。")
        if idle and self._connection_active:
            # 等待 Qt 更新关闭码，握手失败也可能只发出状态或错误信号。
            QTimer.singleShot(0, self, self._connection_lost)

        self._update_auxiliary_windows()
        self._update_send_controls()

    def _update_send_controls(self) -> None:
        connected = self.socket.state() == SocketState.ConnectedState
        can_send = not self._managed or (self._channel_state_ready and self._channels.get(self._channel_id, {}).get("can_send") is True)
        send_available = connected and can_send and not self._awaiting_send
        self.message.setReadOnly(self._awaiting_send or (self._managed and (not connected or not can_send)))
        if self._managed:
            self.message.send_available = send_available
            channel = self._channels.get(self._channel_id, {})
            if not connected:
                placeholder = "未连接"
            elif channel.get("attributes_required") is True:
                placeholder = "请先通过「个人」导入角色属性"
            else:
                placeholder = f"发送到 {channel.get('name', '')}" if can_send else "频道只读"
            self.message.setPlaceholderText(placeholder)
        else:
            self.send_button.setEnabled(send_available)
        self.chat_history.set_pagination(self._history_has_more, self._history_request is not None)

    def _connected(self) -> None:
        self._opening = False
        self._connect_timer.stop()
        if self.session is not None:
            self._session_check.cancel()
            self._heartbeat_timer.start(PING_INTERVAL_SECONDS * 1000)
            if self.retry_send is not None:
                self.feedback.setText("草稿已保留，未确认的消息可以手动重发。")
            elif self._retry_count:
                self.feedback.setText("连接已恢复。")
            self._retry_count = 0
            self._update_connection_state(self.socket.state())
        if not self._managed:
            self.chat_history.clear()
            return
        self.socket.sendTextMessage(json.dumps({"type": CHANNEL_STATE_GET}))
        self._channel_timer.start(5000)

    def _channel_state_timed_out(self) -> None:
        self.feedback.setText("主机未返回频道信息，正在重新连接。")
        self.socket.abort()

    def saved_drafts(self) -> dict:
        self._store_draft()
        return dict(self._channel_drafts)

    def restore_drafts(self, drafts: dict) -> None:
        self._channel_drafts.update(drafts)
        self._restore_draft()

    def _store_draft(self) -> None:
        self._channel_drafts[self._channel_id] = (self.message.toPlainText(), self.retry_send)

    def _restore_draft(self) -> None:
        text, self.retry_send = self._channel_drafts.get(self._channel_id, ("", None))
        self.message.setPlainText(text)
        self._awaiting_send = False

    def _apply_channel_state(self, state: dict, *, online: bool = True) -> None:
        previous_ranges = {key: channel["history_ranges"] for key, channel in self._channels.items()}
        channels = state.get("channels")
        if not isinstance(channels, list) or not all(
            isinstance(item, dict) and isinstance(item.get("channel_id"), str)
            and isinstance(item.get("history_ranges"), list) for item in channels
        ):
            self.feedback.setText("主机返回的频道信息无效。")
            return
        assigned = state.get("current_channel_id")
        available = {item["channel_id"]: item for item in channels if item.get("can_read") is True}
        if assigned not in available:
            self.feedback.setText("主机未返回当前场景。")
            return
        self._channel_timer.stop()
        previous = self._channels.get(self._channel_id)
        was_ready = self._channel_state_ready
        selected = self._channel_id
        if (state.get("membership_mode") != "multiple" and assigned != self._assigned_channel_id) or selected not in available:
            selected = assigned
        changed = selected != self._channel_id or previous is None or any(
            previous.get(key) != available[selected].get(key) for key in ("history_ranges", "can_read", "status")
        )
        if changed or not was_ready:
            self._store_draft()
            self._finish_history_request()
            self._finish_sync()
        self._channel_state_ready = online
        if self._base_cache is not None:
            try:
                self._base_cache.save_channel_state(state)
            except (OSError, sqlite3.Error, ValueError):
                self.feedback.setText("本地频道信息保存失败。")
        self._channels = available
        self._unread_channels.intersection_update(available)
        self._unread_channels.discard(selected)
        self._assigned_channel_id = assigned
        lobby_id = next((item["channel_id"] for item in channels if item["kind"] == "lobby"), "")
        if "" in self._channel_drafts:
            self._channel_drafts[lobby_id] = self._channel_drafts.pop("")
        self.channel_choice.blockSignals(True)
        groups = state.get("groups") or [{"group_id": "default", "name": "频道"}]
        nav_channels = [
            dict(item, group_id=item.get("group_id") or groups[0]["group_id"],
                 unread_count=channel_id in self._unread_channels)
            for channel_id, item in available.items()
        ]
        self.channel_choice.set_channels(groups, nav_channels, selected)
        self.channel_choice.blockSignals(False)
        self._channel_id = selected
        self._set_channel_cache()
        if changed:
            self.chat_history.clear()
            if self._cache is not None:
                self._show_cached_page()
        if changed or not was_ready:
            self._restore_draft()
        self._update_channel_status()
        self._update_auxiliary_windows()
        for key, dialog in self._log_dialogs.items():
            if previous_ranges.get(key) != self._channels[key]["history_ranges"]:
                self._cancel_log_requests(dialog)
                dialog.timer.stop()
                dialog.loading = dialog.loaded = dialog.has_more = False
                dialog.pending_live.clear()
                dialog.history.clear()
                if dialog.isVisible():
                    dialog.load()
        if online and (changed or not was_ready):
            self._sync_channel()
        if online:
            imported = state.get("attributes_imported")
            self.attributes_imported = imported if type(imported) is bool else None
            self.attributesStatusChanged.emit()

    def _set_channel_cache(self) -> None:
        if self._base_cache is not None and self._channel_id in self._channels:
            self._cache = self._base_cache.for_channel(
                self._channel_id, lobby=self._channels[self._channel_id]["kind"] == "lobby",
            )

    def _update_channel_status(self) -> None:
        channel = self._channels.get(self._channel_id)
        if channel is not None:
            self.channel_header.set_channel(channel["name"], channel.get("description", ""))
        self._update_send_controls()

    def _select_channel(self, _index: int) -> None:
        selected = self.channel_choice.currentData()
        if selected not in self._channels or selected == self._channel_id:
            return
        self._store_draft()
        self._finish_history_request()
        self._finish_sync()
        self._channel_id = selected
        self._set_channel_cache()
        self.chat_history.clear()
        self._unread_channels.discard(selected)
        self.channel_choice.set_unread(selected, False)
        if self._cache is not None:
            self._show_cached_page()
        self._restore_draft()
        self._update_channel_status()
        if self.socket.state() == SocketState.ConnectedState:
            self._sync_channel()

    def _sync_channel(self) -> None:
        if self._cache is None:
            return
        self._live_during_sync = []
        try:
            position = self._cache.position()
        except (OSError, sqlite3.Error):
            position = None
        self._request_history("latest" if position is None else "after", position or 0)

    def _heartbeat(self) -> None:
        if self.socket.state() != SocketState.ConnectedState:
            return
        if self._ping_payload is not None:
            self.feedback.setText("主机未响应心跳，连接已中断。")
            self.socket.abort()
            return
        self._ping_payload = uuid4().bytes
        self.socket.ping(self._ping_payload)
        self._heartbeat_timer.start(PONG_TIMEOUT_SECONDS * 1000)

    def _pong_received(self, _elapsed: int, payload: bytes) -> None:
        if self._ping_payload is not None and payload == self._ping_payload:
            self._ping_payload = None
            self._heartbeat_timer.start(PING_INTERVAL_SECONDS * 1000)

    def _stop_heartbeat(self) -> None:
        self._heartbeat_timer.stop()
        self._ping_payload = None

    def _show_cached_page(self, before: int | None = None, *, require_contiguous: bool = False) -> bool:
        try:
            page = self._cache.page(before)
        except (OSError, sqlite3.Error):
            self.feedback.setText("无法读取本地历史。")
            self.chat_history.set_pagination(self._history_has_more)
            return False
        if require_contiguous and before is not None and page:
            # 本地可能缺了中间的一页，在线时让主机补齐这个缺口。
            if [item["sequence"] for item in page] != list(range(before - len(page), before)):
                return False
        self.chat_history.add_messages(page)
        self._history_has_more = len(page) == HISTORY_PAGE_SIZE
        self.chat_history.set_pagination(self._history_has_more)
        return bool(page)

    def _load_older(self) -> None:
        if self._cache is None or self._history_request is not None:
            self.chat_history.set_pagination(self._history_has_more, self._history_request is not None)
            return
        before = self.chat_history.oldest_sequence
        if self.is_online:
            self._request_history("latest" if before is None else "before", before or 0)
        else:
            self._show_cached_page(before)

    def _request_history(self, mode: str, cursor: int) -> None:
        self._history_request_id = str(uuid4())
        payload = json.dumps({"type": CHAT_HISTORY_GET, "mode": mode, "cursor": cursor,
                              "channel_id": self._channel_id, "request_id": self._history_request_id})
        if self.socket.sendTextMessage(payload) <= 0:
            self._finish_sync()
            self.feedback.setText("无法请求主机历史，已保留本地消息。")
            return
        self._history_request = (mode, cursor)
        self.chat_history.set_pagination(self._history_has_more, loading=True)
        self._history_timer.start(5000)

    def _finish_history_request(self) -> None:
        self._history_request = None
        self._history_timer.stop()
        self.chat_history.set_pagination(self._history_has_more)

    def _history_timed_out(self) -> None:
        self._finish_history_request()
        self._finish_sync()
        self.feedback.setText("主机未返回历史记录，已保留本地消息。")

    def _show_history_page(self, event: dict) -> None:
        if event.get("request_id") in self._log_requests:
            self._receive_log_history(event)
            return
        mode, cursor = event.get("mode"), event.get("cursor")
        if (self._history_request is None or (mode, cursor) != self._history_request
                or event.get("channel_id") != self._channel_id
                or event.get("request_id") != self._history_request_id):
            return
        self._finish_history_request()
        page = event.get("messages")
        if (not isinstance(page, list) or len(page) > HISTORY_PAGE_SIZE
                or not all(is_saved_message(item) and item.get("channel_id") == self._channel_id for item in page)):
            self._finish_sync()
            self.feedback.setText("主机历史读取失败。")
            return
        sequences = [item["sequence"] for item in page]
        if sequences != sorted(set(sequences)) or (mode == "after" and sequences and sequences[0] <= cursor):
            self._finish_sync()
            self.feedback.setText("主机返回的历史顺序无效。")
            return
        if mode in ("latest", "before"):
            self._history_has_more = event.get("has_more") is True
        synced_through = (sequences[-1] if sequences else cursor) if mode != "before" else None
        self._show_saved_messages(page, latest=mode == "latest", synced_through=synced_through)
        if mode == "after" and page and event.get("has_more") is True:
            self._request_history("after", sequences[-1])
        elif mode != "before":
            self._finish_sync()

    def _finish_sync(self) -> None:
        messages, self._live_during_sync = self._live_during_sync, None
        if messages:
            self._show_saved_messages(messages)
        self.chat_history.set_pagination(self._history_has_more, self._history_request is not None)

    def _show_live_message(self, message: dict) -> None:
        channel_id = message.get("channel_id")
        dialog = self._log_dialogs.get(channel_id)
        if dialog is not None:
            if dialog.loaded:
                dialog.history.add_message(message)
            else:
                dialog.pending_live.append(message)
        if self._managed and channel_id != self._channel_id:
            if channel_id not in self._channels or not is_saved_message(message):
                return
            if self.session is not None and message.get("member_id") != self.session.member_id:
                self._unread_channels.add(channel_id)
                self.channel_choice.set_unread(channel_id, True)
            if self._base_cache is not None:
                try:
                    cache = self._base_cache.for_channel(channel_id, lobby=self._channels[channel_id]["kind"] == "lobby")
                    cache.save([message])
                except (OSError, sqlite3.Error):
                    self.feedback.setText("本地历史保存失败。")
            return
        if self._live_during_sync is None:
            self._show_saved_messages([message])
        else:
            self._live_during_sync.append(message)

    def _show_saved_messages(self, messages: list[dict], *, latest: bool = False,
                             synced_through: int | None = None) -> None:
        if self._cache is not None:
            saved = [item for item in messages if is_saved_message(item)]
            try:
                if saved or latest:
                    self._cache.save(saved, latest=latest, synced_through=synced_through)
            except (OSError, sqlite3.Error, UnicodeError):
                self.feedback.setText("本地历史保存失败，仍可查看收到的消息。")
        self.chat_history.add_messages(messages)
        self.chat_history.set_pagination(self._history_has_more, self._history_request is not None)

    @Slot()
    def _send_message(self) -> None:
        available = self.message.send_available if self._managed else self.send_button.isEnabled()
        if not available:
            return

        text = self.message.toPlainText()
        error = chat_text_error(text)
        if error is not None:
            self.feedback.setText(SEND_ERRORS[error])
            return

        event = {"type": CHAT_SEND, "text": text}
        if self.session is not None:
            event["channel_id"] = self._channel_id
            if self.retry_send is None or self.retry_send[0] != text:
                self.retry_send = (text, str(uuid4()))
            event["request_id"] = self.retry_send[1]
        payload = json.dumps(event)
        if self.socket.sendTextMessage(payload) <= 0:
            self.feedback.setText("发送失败，请检查连接后重试。")
            return

        self._awaiting_send = True
        self._update_send_controls()
        self.feedback.setText("等待主机确认…")

    @Slot(str)
    def _show_message(self, payload: str) -> None:
        event = decode_event(payload)
        if event is None:
            self.feedback.setText("收到无法识别的频道消息。")
            return

        event_type = event.get("type")
        if event_type == CHANNEL_CHANGED:
            self._apply_channel_state(event)
        elif event_type == CHAT_ERROR:
            if "request_id" not in event or self._matches_pending(event):
                self._show_send_error(event.get("code"))
        elif event_type == CHAT_ACK:
            message = event.get("message")
            if self._matches_pending(event) and isinstance(message, dict):
                self._confirm_send()
                self._show_live_message(message)
                if isinstance(event.get("result"), dict):
                    self._show_live_message(event["result"])
        elif event_type == CHAT_MESSAGE:
            self._show_live_message(event)
            if "message_id" not in event and event.get("own") is True and self._awaiting_send:
                self._confirm_send()
        elif event_type == CHAT_HISTORY:
            self._show_history_page(event)

    def _matches_pending(self, event: dict[str, object]) -> bool:
        return self._awaiting_send and self.retry_send is not None and event.get("request_id") == self.retry_send[1]

    def _confirm_send(self) -> None:
        self.retry_send = None
        self.message.clear()
        self.feedback.clear()
        self._awaiting_send = False
        self._update_send_controls()

    def _show_send_error(self, code: object) -> None:
        message = SEND_ERRORS.get(code) if isinstance(code, str) else None
        self.feedback.setText(message or "主机拒绝发送这条消息。")
        self._awaiting_send = False
        self._update_send_controls()

    def _show_socket_error(self, _error: QAbstractSocket.SocketError) -> None:
        self.feedback.setText(f"连接错误：{self.socket.errorString()}")
        if self._connection_active and self.socket.state() == SocketState.UnconnectedState:
            QTimer.singleShot(0, self, self._connection_lost)

    def shutdown(self):
        if self._host_monitor is not None:
            self._host_monitor.stop()
        self._close_connection()
        for dialog in self.findChildren(QDialog):
            dialog.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)
