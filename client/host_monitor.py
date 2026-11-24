"""独立于聊天连接探测对应团的监听状态。"""

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkReply, QNetworkRequest

from .auth_client import server_urls


class HostMonitor(QObject):
    changed = Signal(object)

    def __init__(self, address, campaign_id, parent=None):
        super().__init__(parent)
        self.base, _ = server_urls(address)
        self.campaign_id = campaign_id
        self.manager = QNetworkAccessManager(self)
        self.manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.reply = None
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.check)

    def start(self):
        self.timer.start()
        self.check()

    def stop(self):
        self.timer.stop()
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()

    def check(self):
        if self.reply is not None:
            return
        request = QNetworkRequest(self.base.resolved(QUrl("auth/status")))
        request.setTransferTimeout(2000)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        self.reply = self.manager.get(request)
        self.reply.finished.connect(self._finished)

    def _finished(self):
        reply = self.sender()
        if reply is not self.reply:
            reply.deleteLater()
            return
        self.reply = None
        state = None
        if reply.error() == QNetworkReply.NetworkError.NoError:
            try:
                payload = json.loads(bytes(reply.readAll()))
                if isinstance(payload, dict) and isinstance(payload.get("running"), bool):
                    state = payload["running"] and payload.get("campaign_id") == self.campaign_id
            except (ValueError, UnicodeError):
                pass
        elif reply.error() == QNetworkReply.NetworkError.ConnectionRefusedError:
            state = False
        reply.deleteLater()
        self.changed.emit(state)
