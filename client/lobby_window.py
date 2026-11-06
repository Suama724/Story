import json

from PySide6.QtCore import Qt, QUrl, QUrlQuery, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtNetwork import QAbstractSocket, QNetworkProxy
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from chat_protocol import CHAT_MESSAGE, CHAT_SEND, LOBBY_PATH, decode_event

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

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Story · 大厅")
        self.resize(640, 440)

        self.socket = QWebSocket(parent=self)
        # 直连测试主机，避免系统代理接管局域网连接。
        self.socket.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.address = QLineEdit(DEFAULT_ADDRESS)
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
        self.message.setPlaceholderText("输入测试消息")
        self.message.setFixedHeight(80)
        self.send_button = QPushButton("发送")

        self._build_layout()
        self._connect_signals()
        self._update_connection_state(self.socket.state())

    def _build_layout(self) -> None:
        connection_row = QHBoxLayout()
        connection_row.addWidget(QLabel("主机地址"))
        connection_row.addWidget(self.address, stretch=1)
        connection_row.addWidget(QLabel("昵称"))
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

        nickname = self.nickname.text().strip()
        if not nickname or len(nickname.splitlines()) != 1:
            self.feedback.setText("请输入单行昵称。")
            return

        query = QUrlQuery(address)
        query.removeAllQueryItems("nickname")
        encoded_nickname = QUrl.toPercentEncoding(nickname).data().decode("ascii")
        query.addQueryItem("nickname", encoded_nickname)
        address.setQuery(query)

        self.feedback.clear()
        self.socket.open(address)

    @Slot()
    def _close_connection(self) -> None:
        if self.socket.state() == SocketState.ConnectedState:
            self.socket.close()
        else:
            self.socket.abort()

    def _update_connection_state(self, state: SocketState) -> None:
        connected = state == SocketState.ConnectedState
        idle = state == SocketState.UnconnectedState

        self.connection_status.setText(STATE_LABELS.get(state, "正在连接…"))
        self.address.setEnabled(idle)
        self.nickname.setEnabled(idle)
        self.connect_button.setEnabled(idle)
        self.disconnect_button.setEnabled(not idle and state != SocketState.ClosingState)
        self.send_button.setEnabled(connected)

    @Slot()
    def _send_message(self) -> None:
        text = self.message.toPlainText()
        if not text.strip():
            self.feedback.setText("请输入测试消息。")
            return

        payload = json.dumps({"type": CHAT_SEND, "text": text})
        if self.socket.sendTextMessage(payload) <= 0:
            self.feedback.setText("发送失败，请检查连接后重试。")
            return

        self.feedback.clear()
        self.message.clear()

    @Slot(str)
    def _show_message(self, payload: str) -> None:
        event = decode_event(payload)
        if event is None or event.get("type") != CHAT_MESSAGE:
            self.feedback.setText("收到无法识别的大厅消息。")
            return

        nickname = event.get("nickname")
        text = event.get("text")
        if isinstance(nickname, str) and isinstance(text, str):
            self.chat_history.appendPlainText(f"{nickname}：{text}")

    def _show_socket_error(self, _error: QAbstractSocket.SocketError) -> None:
        self.feedback.setText(f"连接错误：{self.socket.errorString()}")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._close_connection()
        super().closeEvent(event)
