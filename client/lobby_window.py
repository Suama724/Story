import json
import sqlite3
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, QUrl, QUrlQuery, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtNetwork import QAbstractSocket, QNetworkProxy, QNetworkRequest
from PySide6.QtWebSockets import QWebSocket, QWebSocketProtocol
from PySide6.QtWidgets import (
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
    HISTORY_PAGE_SIZE,
    LOBBY_PATH,
    SEND_ERRORS,
    chat_text_error,
    decode_event,
)

from .auth_client import ClientSession
from .chat_history import ChatHistory
from .history_cache import HistoryCache, is_saved_message

SocketState = QAbstractSocket.SocketState

DEFAULT_ADDRESS = f"ws://127.0.0.1:8000{LOBBY_PATH}"
STATE_LABELS = {
    SocketState.HostLookupState: "正在解析主机地址…",
    SocketState.ConnectingState: "正在连接…",
    SocketState.ConnectedState: "已连接",
    SocketState.ClosingState: "正在断开…",
    SocketState.UnconnectedState: "未连接",
}


class LobbyWindow(QWidget):
    """单大厅聊天窗口。"""

    sessionRejected = Signal()
    sessionCheckRequested = Signal()
    loginRequested = Signal()

    def __init__(self, *, session: ClientSession | None = None, address: str = DEFAULT_ADDRESS,
                 cache: HistoryCache | None = None, offline_actor: str = "") -> None:
        super().__init__()
        self.session = session
        self._managed = session is not None or bool(offline_actor)
        self._cache = cache
        self._history_request: tuple[str, int] | None = None
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.timeout.connect(self._history_timed_out)
        self._manual_disconnect = False
        self.setWindowTitle("Story · 大厅")
        self.resize(640, 440)

        self.socket = QWebSocket(parent=self)
        # 直连测试主机，避免系统代理接管局域网连接。
        self.socket.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.address = QLineEdit(address)
        self.nickname = QLineEdit()
        self.nickname.setPlaceholderText("输入临时昵称")
        self.connect_button = QPushButton("连接")
        self.disconnect_button = QPushButton("断开")
        self.connection_status = QLabel()
        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.chat_history = ChatHistory()
        self.chat_history.setPlaceholderText("暂无可查看的消息")
        self.older_button = QPushButton("查看更早消息")
        self.older_button.clicked.connect(self._load_older)
        self.older_button.setVisible(cache is not None)
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("输入消息")
        self.message.setFixedHeight(80)
        self.send_button = QPushButton("发送")
        self._awaiting_send = False
        self.retry_send: tuple[str, str] | None = None
        if self._managed:
            self.nickname.setText(session.actor_name if session is not None else offline_actor)
            self.nickname.setReadOnly(True)
            self.address.setReadOnly(True)
            self.connect_button.setText("重新连接")

        self._build_layout()
        self._connect_signals()
        self._update_connection_state(self.socket.state())
        if self._cache is not None:
            self._show_cached_page()

    def resume(self, session: ClientSession, cache: HistoryCache) -> None:
        self.session = session
        self.nickname.setText(session.actor_name)
        if self._cache is None:
            self._cache = cache
            self.older_button.show()
            self._show_cached_page()
        self._open_connection()

    def _build_layout(self) -> None:
        connection_row = QHBoxLayout()
        connection_row.addWidget(QLabel("主机地址"))
        connection_row.addWidget(self.address, stretch=1)
        connection_row.addWidget(QLabel("角色" if self._managed else "昵称"))
        connection_row.addWidget(self.nickname)
        connection_row.addWidget(self.connect_button)
        connection_row.addWidget(self.disconnect_button)

        layout = QVBoxLayout(self)
        layout.addLayout(connection_row)
        layout.addWidget(self.connection_status)
        layout.addWidget(self.feedback)
        layout.addWidget(QLabel("大厅消息"))
        layout.addWidget(self.older_button)
        layout.addWidget(self.chat_history, stretch=1)
        layout.addWidget(self.message)
        layout.addWidget(self.send_button)

    def _connect_signals(self) -> None:
        self.connect_button.clicked.connect(self._open_connection)
        self.address.returnPressed.connect(self._open_connection)
        self.nickname.returnPressed.connect(self._open_connection)
        self.disconnect_button.clicked.connect(self._close_connection)
        self.send_button.clicked.connect(self._send_message)
        self.socket.stateChanged.connect(self._update_connection_state)
        self.socket.connected.connect(self._connected)
        self.socket.textMessageReceived.connect(self._show_message)
        self.socket.errorOccurred.connect(self._show_socket_error)
        self.socket.disconnected.connect(self._disconnected)

    @Slot()
    def _open_connection(self) -> None:
        if self.socket.state() != SocketState.UnconnectedState:
            return
        if self.session is None and self._managed:
            self.loginRequested.emit()
            return

        address = QUrl(self.address.text().strip(), QUrl.ParsingMode.StrictMode)
        if (
            not address.isValid()
            or address.scheme() not in {"ws", "wss"}
            or not address.host()
        ):
            self.feedback.setText("地址无效，请输入完整的 WebSocket 地址。")
            return

        self.feedback.clear()
        self._manual_disconnect = False
        if self.session is not None:
            request = QNetworkRequest(address)
            request.setRawHeader(b"Authorization", f"Bearer {self.session.token}".encode("ascii"))
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
        self._manual_disconnect = True
        if self.socket.state() == SocketState.ConnectedState:
            self.socket.close()
        else:
            self.socket.abort()

    def _disconnected(self) -> None:
        if self.session is None or self._manual_disconnect:
            return
        if self.socket.closeCode() == QWebSocketProtocol.CloseCode.CloseCodePolicyViolated:
            self.sessionRejected.emit()
        else:
            self.sessionCheckRequested.emit()

    def _update_connection_state(self, state: SocketState) -> None:
        connected = state == SocketState.ConnectedState
        idle = state == SocketState.UnconnectedState

        self.connection_status.setText(STATE_LABELS.get(state, "正在连接…"))
        self.address.setEnabled(idle)
        self.nickname.setEnabled(idle)
        self.connect_button.setEnabled(idle)
        self.disconnect_button.setEnabled(not idle and state != SocketState.ClosingState)
        if not connected:
            self._finish_history_request()
        if not connected and self._awaiting_send:
            self._awaiting_send = False
            self.feedback.setText("连接已断开，消息未确认，草稿已保留。")

        self._update_send_controls()

    def _update_send_controls(self) -> None:
        connected = self.socket.state() == SocketState.ConnectedState
        self.send_button.setEnabled(connected and not self._awaiting_send)
        self.message.setReadOnly(self._awaiting_send or (self._managed and not connected))

    def _connected(self) -> None:
        if self._cache is None:
            self.chat_history.clear()
            return
        try:
            position = self._cache.position()
        except (OSError, sqlite3.Error):
            position = None
        self._request_history("latest" if position is None else "after", position or 0)

    def _show_cached_page(self, before: int | None = None, *, require_contiguous: bool = False) -> bool:
        try:
            page = self._cache.page(before)
        except (OSError, sqlite3.Error):
            self.feedback.setText("无法读取本地历史。")
            return False
        if require_contiguous and before is not None and page:
            # 本地可能缺了中间的一页，在线时让主机补齐这个缺口。
            if [item["sequence"] for item in page] != list(range(before - len(page), before)):
                return False
        self.chat_history.add_messages(page)
        if not page and before is not None and not require_contiguous:
            self.feedback.setText("本地没有更早的消息。")
        return bool(page)

    def _load_older(self) -> None:
        if self._cache is None or self._history_request is not None:
            return
        before = self.chat_history.oldest_sequence
        online = self.socket.state() == SocketState.ConnectedState
        if not self._show_cached_page(before, require_contiguous=online) and online:
            self._request_history("latest" if before is None else "before", before or 0)

    def _request_history(self, mode: str, cursor: int) -> None:
        payload = json.dumps({"type": CHAT_HISTORY_GET, "mode": mode, "cursor": cursor})
        if self.socket.sendTextMessage(payload) > 0:
            self._history_request = (mode, cursor)
            self.older_button.setEnabled(False)
            self._history_timer.start(5000)

    def _finish_history_request(self) -> None:
        self._history_request = None
        self._history_timer.stop()
        self.older_button.setEnabled(True)

    def _history_timed_out(self) -> None:
        self._finish_history_request()
        self.feedback.setText("主机未返回历史记录，已保留本地消息。")

    def _show_history_page(self, event: dict) -> None:
        mode, cursor = event.get("mode"), event.get("cursor")
        if self._history_request is None or (mode, cursor) != self._history_request:
            return
        self._finish_history_request()
        page = event.get("messages")
        if (not isinstance(page, list) or len(page) > HISTORY_PAGE_SIZE
                or not all(is_saved_message(item) for item in page)):
            self.feedback.setText("主机历史读取失败。")
            return
        sequences = [item["sequence"] for item in page]
        if sequences != sorted(set(sequences)) or (mode == "after" and sequences and sequences[0] <= cursor):
            self.feedback.setText("主机返回的历史顺序无效。")
            return
        self._show_saved_messages(page, latest=mode == "latest")
        if mode == "after" and page and event.get("has_more") is True:
            self._request_history("after", sequences[-1])
        elif mode == "before" and not page:
            self.feedback.setText("已到最早的消息。")

    def _show_saved_messages(self, messages: list[dict], *, latest: bool = False) -> None:
        if self._cache is not None:
            saved = [item for item in messages if is_saved_message(item)]
            try:
                if saved or latest:
                    self._cache.save(saved, latest=latest)
            except (OSError, sqlite3.Error, UnicodeError):
                self.feedback.setText("本地历史保存失败，仍可查看收到的消息。")
        self.chat_history.add_messages(messages)

    @Slot()
    def _send_message(self) -> None:
        if not self.send_button.isEnabled():
            return

        text = self.message.toPlainText()
        error = chat_text_error(text)
        if error is not None:
            self.feedback.setText(SEND_ERRORS[error])
            return

        event = {"type": CHAT_SEND, "text": text}
        if self.session is not None:
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
            self.feedback.setText("收到无法识别的大厅消息。")
            return

        event_type = event.get("type")
        if event_type == CHAT_ERROR:
            if "request_id" not in event or self._matches_pending(event):
                self._show_send_error(event.get("code"))
        elif event_type == CHAT_ACK:
            message = event.get("message")
            if self._matches_pending(event) and isinstance(message, dict):
                self._confirm_send()
                self._show_saved_messages([message])
        elif event_type == CHAT_MESSAGE:
            self._show_saved_messages([event])
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

    def closeEvent(self, event: QCloseEvent) -> None:
        self._close_connection()
        super().closeEvent(event)
