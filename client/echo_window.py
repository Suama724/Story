from PySide6.QtCore import QUrl, Slot
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


SocketState = QAbstractSocket.SocketState

DEFAULT_ADDRESS = "ws://127.0.0.1:8000/ws"
STATE_LABELS = {
    SocketState.HostLookupState: "正在解析主机地址…",
    SocketState.ConnectingState: "正在连接…",
    SocketState.ConnectedState: "已连接",
    SocketState.ClosingState: "正在断开…",
    SocketState.UnconnectedState: "未连接",
}


class EchoWindow(QWidget):
    """用于验证连接和文本往返的 M0 调试窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Story · M0 连通测试")
        self.resize(640, 440)

        self.socket = QWebSocket(parent=self)
        # M0 直连测试主机，避免系统代理接管局域网连接。
        self.socket.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.address = QLineEdit(DEFAULT_ADDRESS)
        self.connect_button = QPushButton("连接")
        self.disconnect_button = QPushButton("断开")
        self.connection_status = QLabel()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("输入测试消息，例如：地下室，听得见吗？")
        self.message.setFixedHeight(80)
        self.send_button = QPushButton("发送")

        self._build_layout()
        self._connect_signals()
        self._update_connection_state(self.socket.state())

    def _build_layout(self) -> None:
        connection_row = QHBoxLayout()
        connection_row.addWidget(QLabel("主机地址"))
        connection_row.addWidget(self.address, stretch=1)
        connection_row.addWidget(self.connect_button)
        connection_row.addWidget(self.disconnect_button)

        layout = QVBoxLayout(self)
        layout.addLayout(connection_row)
        layout.addWidget(self.connection_status)
        layout.addWidget(QLabel("收发日志"))
        layout.addWidget(self.log, stretch=1)
        layout.addWidget(self.message)
        layout.addWidget(self.send_button)

    def _connect_signals(self) -> None:
        self.connect_button.clicked.connect(self._open_connection)
        self.address.returnPressed.connect(self._open_connection)
        self.disconnect_button.clicked.connect(self._close_connection)
        self.send_button.clicked.connect(self._send_message)
        self.socket.stateChanged.connect(self._update_connection_state)
        self.socket.textMessageReceived.connect(self._show_echo)
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
            self.log.appendPlainText("地址无效，请输入完整的 WebSocket 地址。")
            return

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
        self.connect_button.setEnabled(idle)
        self.disconnect_button.setEnabled(not idle and state != SocketState.ClosingState)
        self.send_button.setEnabled(connected)

    @Slot()
    def _send_message(self) -> None:
        text = self.message.toPlainText()
        if not text.strip():
            self.log.appendPlainText("请输入测试消息。")
            return

        if self.socket.sendTextMessage(text) <= 0:
            self.log.appendPlainText("发送失败，请检查连接后重试。")
            return

        self.log.appendPlainText(f"发送 > {text}")
        self.message.clear()

    @Slot(str)
    def _show_echo(self, text: str) -> None:
        self.log.appendPlainText(f"回显 < {text}")

    def _show_socket_error(self, _error: QAbstractSocket.SocketError) -> None:
        self.log.appendPlainText(f"连接错误：{self.socket.errorString()}")
