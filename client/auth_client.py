"""在 Qt 事件循环中办理 HTTP 登录及会话操作。"""

import json
import re
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkReply, QNetworkRequest

from chat_protocol import LOBBY_PATH


def server_urls(text: str) -> tuple[QUrl, QUrl]:
    url = QUrl(text.strip(), QUrl.ParsingMode.StrictMode)
    schemes = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}
    if (not url.isValid() or url.scheme() not in schemes or not url.host()
            or url.userInfo() or url.hasQuery() or url.hasFragment()):
        raise ValueError("请输入主机的连接地址和端口。")
    path = url.path().rstrip("/")
    if path.endswith(LOBBY_PATH):
        prefix = path[:-len(LOBBY_PATH)]
    elif url.scheme() in ("http", "https") or not path:
        prefix = path
    else:
        raise ValueError("大厅地址应以 /ws/lobby 结尾。")
    websocket = QUrl(url)
    websocket.setScheme(schemes[url.scheme()])
    websocket.setPath(prefix + LOBBY_PATH)
    http = QUrl(url)
    http.setScheme("https" if websocket.scheme() == "wss" else "http")
    http.setPath(prefix + "/")
    return http, websocket


@dataclass(frozen=True)
class ClientSession:
    token: str = field(repr=False)
    campaign_id: str
    campaign_name: str
    member_id: str
    actor_name: str

    @classmethod
    def from_response(cls, response: dict) -> "ClientSession":
        fields = ("token", "campaign_id", "campaign_name", "member_id", "actor_name")
        if (any(not isinstance(response.get(name), str) or not response[name] for name in fields)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", response["token"]) is None):
            raise ValueError("主机返回的登录信息不完整。")
        return cls(**{name: response[name] for name in fields})


class AuthClient(QObject):
    completed = Signal(str, object)
    failed = Signal(str, str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.manager = QNetworkAccessManager(self)
        self.manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self._reply: QNetworkReply | None = None
        self._operation = ""

    def request(self, base: QUrl, operation: str, body: dict | None = None, token: str = "") -> None:
        if self._reply is not None:
            raise RuntimeError("正在等待主机响应。")
        request = QNetworkRequest(base.resolved(QUrl(f"auth/{operation}")))
        request.setTransferTimeout(15000)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setRawHeader(b"Content-Type", b"application/json; charset=utf-8")
        if token:
            request.setRawHeader(b"Authorization", f"Bearer {token}".encode("ascii"))
        self._operation = operation
        if operation == "session":
            self._reply = self.manager.get(request)
        else:
            payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
            self._reply = self.manager.post(request, payload)
        self._reply.finished.connect(self._finished)

    def cancel(self) -> None:
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.abort()

    @Slot()
    def _finished(self) -> None:
        reply = self.sender()
        if reply is not self._reply:
            reply.deleteLater()
            return
        self._reply = None
        operation = self._operation
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        payload = bytes(reply.readAll())
        network_error = reply.error()
        reply.deleteLater()
        try:
            response = json.loads(payload) if len(payload) <= 65536 else None
        except (ValueError, UnicodeError):
            response = None
        if isinstance(status, int) and 200 <= status < 300:
            if isinstance(response, dict) and network_error == QNetworkReply.NetworkError.NoError:
                self.completed.emit(operation, response)
            else:
                self.failed.emit(operation, "invalid_response", "主机返回了无法识别的响应。")
        elif isinstance(status, int):
            code, message = "http_error", f"主机拒绝了请求（HTTP {status}）。"
            if isinstance(response, dict) and isinstance(response.get("error"), str):
                code = response["error"]
                if isinstance(response.get("message"), str):
                    message = response["message"]
            if code == "campaign_not_configured":
                message = "请在主机工作台选择团并启动监听。"
            self.failed.emit(operation, code, message)
        elif network_error == QNetworkReply.NetworkError.SslHandshakeFailedError:
            self.failed.emit(operation, "network_error", "加密连接校验失败，请检查主机地址和证书。")
        elif network_error in (
            QNetworkReply.NetworkError.ConnectionRefusedError, QNetworkReply.NetworkError.HostNotFoundError,
            QNetworkReply.NetworkError.TimeoutError, QNetworkReply.NetworkError.RemoteHostClosedError,
            QNetworkReply.NetworkError.TemporaryNetworkFailureError,
        ):
            self.failed.emit(operation, "host_unreachable", "无法连接主机，请检查地址或监听状态。")
        else:
            self.failed.emit(operation, "network_error", "无法连接主机，请检查地址、监听状态或稍后重试。")
