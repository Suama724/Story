import json

from PySide6.QtCore import Qt, QUrl, QUrlQuery, Signal, Slot
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
    CHAT_ERROR,
    CHAT_MESSAGE,
    CHAT_SEND,
    LOBBY_PATH,
    MAX_TEXT_LENGTH,
    chat_text_error,
    decode_event,
)

from .auth_client import ClientSession

SocketState = QAbstractSocket.SocketState

DEFAULT_ADDRESS = f"ws://127.0.0.1:8000{LOBBY_PATH}"
STATE_LABELS = {
    SocketState.HostLookupState: "正在解析主机地址…",
    SocketState.ConnectingState: "正在连接…",
    SocketState.ConnectedState: "已连接",
    SocketState.ClosingState: "正在断开…",
    SocketState.UnconnectedState: "未连接",
}


SEND_ERRORS = {
    "invalid_text": "消息正文必须是文本。",
    "empty_text": "请输入消息，正文不能只有空白。",
    "text_too_long": f"消息最多 {MAX_TEXT_LENGTH:,} 个字符。",
}


class LobbyWindow(QWidget):
    """单大厅聊天窗口。"""

    sessionRejected = Signal()
    sessionCheckRequested = Signal()

    def __init__(self, *, session: ClientSession | None = None, address: str = DEFAULT_ADDRESS) -> None:
        super().__init__()
        self.session = session
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
        self.chat_history = QPlainTextEdit()
        self.chat_history.setReadOnly(True)
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("输入消息")
        self.message.setFixedHeight(80)
        self.send_button = QPushButton("发送")
        self._awaiting_send = False
        if session is not None:
            self.nickname.setText(session.actor_name)
            self.nickname.setReadOnly(True)
            self.address.setReadOnly(True)
            self.connect_button.setText("重新连接")

        self._build_layout()
        self._connect_signals()
        self._update_connection_state(self.socket.state())

    def _build_layout(self) -> None:
        connection_row = QHBoxLayout()
        connection_row.addWidget(QLabel("主机地址"))
        connection_row.addWidget(self.address, stretch=1)
        connection_row.addWidget(QLabel("角色" if self.session else "昵称"))
        connection_row.addWidget(self.nickname)
        connection_row.addWidget(self.connect_button)
        connection_row.addWidget(self.disconnect_button)

        layout = QVBoxLayout(self)
        layout.addLayout(connection_row)
        layout.addWidget(self.connection_status)
        layout.addWidget(self.feedback)
        layout.addWidget(QLabel("大厅消息"))
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
        self.socket.connected.connect(self.chat_history.clear)
        self.socket.textMessageReceived.connect(self._show_message)
        self.socket.errorOccurred.connect(self._show_socket_error)
        self.socket.disconnected.connect(self._disconnected)

    @Slot()
    def _open_connection(self) -> None:
        if self.socket.state() != SocketState.UnconnectedState:
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
        if not connected and self._awaiting_send:
            self._awaiting_send = False
            self.feedback.setText("连接已断开，消息未确认，草稿已保留。")

        self._update_send_controls()

    def _update_send_controls(self) -> None:
        connected = self.socket.state() == SocketState.ConnectedState
        self.send_button.setEnabled(connected and not self._awaiting_send)
        self.message.setReadOnly(self._awaiting_send)

    @Slot()
    def _send_message(self) -> None:
        if not self.send_button.isEnabled():
            return

        text = self.message.toPlainText()
        error = chat_text_error(text)
        if error is not None:
            self.feedback.setText(SEND_ERRORS[error])
            return

        payload = json.dumps({"type": CHAT_SEND, "text": text})
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
            self._show_send_error(event.get("code"))
        elif event_type == CHAT_MESSAGE:
            nickname = event.get("nickname")
            text = event.get("text")
            if isinstance(nickname, str) and isinstance(text, str):
                self.chat_history.appendPlainText(f"{nickname}：{text}")
                if event.get("own") is True and self._awaiting_send:
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
