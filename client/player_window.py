"""角色登录、登录记忆与大厅中的密码修改。"""

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .auth_client import AuthClient, ClientSession, server_urls
from .lobby_window import DEFAULT_ADDRESS, LobbyWindow
from .login_history import LoginHistory

STYLE = """
QWidget { color: #203548; font-size: 14px; }
QWidget#player { background: #e9eef3; }
QFrame#card { background: white; border: 1px solid #ccd6df; border-radius: 6px; }
QLabel#title { font-size: 24px; font-weight: 600; }
QLabel#muted { color: #687b8d; }
QLineEdit, QPlainTextEdit, QComboBox { background: white; border: 1px solid #ccd6df; border-radius: 4px; padding: 7px; selection-background-color: #d5e8f8; }
QLineEdit:focus, QPlainTextEdit:focus { border: 1px solid #326b97; }
QPushButton { background: #f6f8fa; border: 1px solid #bbcbd8; border-radius: 4px; padding: 8px 14px; }
QPushButton:hover { background: #e6eff7; }
QPushButton:focus { border: 2px solid #326b97; }
QPushButton#primary { background: #326b97; color: white; }
QPushButton:disabled, QPushButton#primary:disabled { background: #edf1f5; color: #95a3af; border-color: #d5dee6; }
"""


def label(text: str = "", name: str = "") -> QLabel:
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    widget.setWordWrap(True)
    return widget


class PasswordDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("修改密码")
        self.setMinimumWidth(400)
        self.current_password, self.new_password, self.confirm_password = (
            QLineEdit() for _ in range(3)
        )
        form = QFormLayout()
        for caption, field in (
            ("当前密码", self.current_password),
            ("新密码", self.new_password),
            ("再次输入", self.confirm_password),
        ):
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.setMaxLength(512)
            form.addRow(caption, field)
        self.feedback = label()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存新密码")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.feedback)
        layout.addWidget(buttons)

    def accept(self) -> None:
        current = self.current_password.text()
        password = self.new_password.text()
        if not 1 <= len(current) <= 256 or not 1 <= len(password) <= 256 or not password.strip():
            self.feedback.setText("请填写当前密码和新密码，密码最多 256 个字符。")
        elif password != self.confirm_password.text():
            self.feedback.setText("两次输入的新密码不一致。")
        elif password == current:
            self.feedback.setText("新密码须与当前密码不同。")
        else:
            super().accept()


class PlayerWindow(QWidget):
    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        self.setObjectName("player")
        self.setWindowTitle("Story · 玩家登录")
        self.resize(780, 620)
        self.setMinimumSize(680, 540)
        self.setStyleSheet(STYLE)
        self.auth = AuthClient(self)
        self.auth.completed.connect(self._auth_completed)
        self.auth.failed.connect(self._auth_failed)
        self.session: ClientSession | None = None
        self.lobby: LobbyWindow | None = None
        self._busy = False
        self._pending_password = ""
        self.history = LoginHistory(settings)
        self._http = None
        self._websocket = None
        self._drafts: dict[tuple[str, str], str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(16)
        header = QHBoxLayout()
        header.addWidget(label("STORY", "title"))
        header.addStretch()
        self.identity = label("以角色身份加入跑团", "muted")
        header.addWidget(self.identity)
        self.logout_button = QPushButton("退出登录")
        self.logout_button.clicked.connect(self._logout)
        self.change_button = QPushButton("修改密码")
        self.change_button.clicked.connect(self._change_password)
        header.addWidget(self.change_button)
        header.addWidget(self.logout_button)
        layout.addLayout(header)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._login_page())
        layout.addWidget(self.pages, 1)
        self.feedback = label()
        layout.addWidget(self.feedback)
        self._load_history()
        self._update_controls()

    def _card(self, title: str, hint: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addStretch()
        card = QFrame()
        card.setObjectName("card")
        contents = QVBoxLayout(card)
        contents.setContentsMargins(26, 24, 26, 24)
        contents.setSpacing(18)
        contents.addWidget(label(title, "title"))
        contents.addWidget(label(hint, "muted"))
        outer.addWidget(card)
        outer.addStretch()
        return page, contents

    def _login_page(self) -> QWidget:
        page, layout = self._card("加入一个团", "选择已保存的角色，或填写组织者提供的登录信息。")
        self.saved_logins = QComboBox()
        self.saved_logins.currentIndexChanged.connect(self._select_saved_login)
        self.address = QLineEdit(DEFAULT_ADDRESS)
        self.campaign_name = QLineEdit()
        self.campaign_name.setPlaceholderText("组织者当前开启的团名")
        self.actor_name = QLineEdit()
        self.actor_name.setMaxLength(160)
        self.actor_name.setPlaceholderText("组织者在团里创建的角色名")
        self.password = QLineEdit()
        self.password.setMaxLength(512)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("新角色默认密码为 123456")
        self.remember_password = QCheckBox("记住密码")
        form = QFormLayout()
        form.setSpacing(12)
        form.addRow("选择角色", self.saved_logins)
        form.addRow("主机地址与端口", self.address)
        form.addRow("团名", self.campaign_name)
        form.addRow("角色名", self.actor_name)
        form.addRow("密码", self.password)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.login_button = QPushButton("登录并进入大厅")
        self.login_button.setObjectName("primary")
        self.login_button.clicked.connect(self._login)
        self.password.returnPressed.connect(self._login)
        self.actor_name.returnPressed.connect(self._login)
        buttons.addWidget(self.remember_password)
        buttons.addStretch()
        buttons.addWidget(self.login_button)
        layout.addLayout(buttons)
        return page

    def _load_history(self) -> None:
        self.saved_logins.blockSignals(True)
        self.saved_logins.clear()
        for record in self.history.entries():
            self.saved_logins.addItem(f"[{record['campaign_name']}]-{record['actor_name']}", record)
        self.saved_logins.addItem("新登录")
        self.saved_logins.setCurrentIndex(0)
        self.saved_logins.blockSignals(False)
        self._select_saved_login()

    def _select_saved_login(self) -> None:
        record = self.saved_logins.currentData()
        self.address.setText(record["address"] if record else DEFAULT_ADDRESS)
        self.campaign_name.setText(record["campaign_name"] if record else "")
        self.actor_name.setText(record["actor_name"] if record else "")
        self.password.setText(record["password"] if record else "123456")
        self.remember_password.setChecked(record is None or bool(record["password"]))

    def _save_login(self, password: str) -> None:
        remembered = password if self.remember_password.isChecked() else ""
        if not self.history.save(self.session, self._websocket.toString(), remembered):
            self.feedback.setText("角色记忆保存失败，请检查应用数据目录的写入权限。")

    def _login(self) -> None:
        if self._busy or self.pages.currentIndex() != 0:
            return
        try:
            self._http, self._websocket = server_urls(self.address.text())
        except ValueError as error:
            self.feedback.setText(str(error))
            return
        campaign_name = self.campaign_name.text().strip()
        if not campaign_name:
            self.feedback.setText("请填写团名。")
            return
        actor_name, password = self.actor_name.text().strip(), self.password.text()
        if not 1 <= len(actor_name) <= 80 or not 1 <= len(password) <= 256:
            self.feedback.setText("请填写角色名和密码。角色名最多 80 个字符，密码最多 256 个字符。")
            return
        self._request("login", {"campaign_name": campaign_name, "actor_name": actor_name, "password": password})

    def _request(self, operation: str, body: dict | None = None) -> None:
        self._busy = True
        self.feedback.setText("正在等待主机响应…")
        self._update_controls()
        self.auth.request(self._http, operation, body, self.session.token if self.session else "")

    def _change_password(self) -> None:
        if self._busy or self.session is None:
            return
        session = self.session
        dialog = PasswordDialog(self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        current_password, new_password = dialog.current_password.text(), dialog.new_password.text()
        dialog.deleteLater()
        if not accepted or self.session is not session or self._busy:
            return
        self._pending_password = new_password
        self._request("password", {
            "current_password": current_password, "new_password": new_password,
        })

    def _auth_completed(self, operation: str, response: dict) -> None:
        self._busy = False
        self.feedback.setText("")
        if operation == "login":
            try:
                self.session = ClientSession.from_response(response)
            except ValueError as error:
                self._reset_login(str(error))
                return
            self._save_login(self.password.text())
            self.password.clear()
            self._enter_lobby()
        elif operation == "password":
            if response.get("changed") is True:
                self.feedback.setText("密码已更新。")
                self._save_login(self._pending_password)
            else:
                self.feedback.setText("主机返回了无法识别的改密结果。")
            self._pending_password = ""
        elif operation == "logout":
            self._reset_login("已退出登录。")
        elif operation == "session" and self.lobby and self.lobby.connect_button.isEnabled():
            self.lobby.feedback.setText("连接已中断，可以点击「重新连接」。")
        self._update_controls()

    def _auth_failed(self, operation: str, code: str, message: str) -> None:
        self._busy = False
        self._pending_password = ""
        if operation == "logout":
            message = "已退出登录。" if code == "invalid_session" else "已清除本机登录，主机未确认退出。"
            self._reset_login(message)
        elif code == "invalid_session":
            self._reset_login("会话已失效，请重新登录。")
        else:
            self.feedback.setText(message)
        self._update_controls()

    def _enter_lobby(self) -> None:
        self.lobby = LobbyWindow(session=self.session, address=self._websocket.toString())
        self.lobby.sessionRejected.connect(self._session_rejected)
        self.lobby.sessionCheckRequested.connect(self._check_session)
        key = (self._http.toString(), self.session.member_id)
        self.lobby.message.setPlainText(self._drafts.pop(key, ""))
        self.pages.addWidget(self.lobby)
        self.pages.setCurrentWidget(self.lobby)
        self.identity.setText(f"[{self.session.campaign_name}]-{self.session.actor_name}")
        self.setWindowTitle(f"Story · {self.session.actor_name}")
        QTimer.singleShot(0, self.lobby._open_connection)

    def _leave_lobby(self) -> None:
        if self.lobby is not None:
            key = (self._http.toString(), self.session.member_id)
            self._drafts[key] = self.lobby.message.toPlainText()
            self.lobby._close_connection()
            self.pages.removeWidget(self.lobby)
            self.lobby.deleteLater()
            self.lobby = None

    def _reset_login(self, message: str) -> None:
        self.auth.cancel()
        self._leave_lobby()
        self.session = None
        self._busy = False
        self._pending_password = ""
        self._load_history()
        self.pages.setCurrentIndex(0)
        self.identity.setText("以角色身份加入跑团")
        self.setWindowTitle("Story · 玩家登录")
        self.feedback.setText(message)
        self._update_controls()

    def _session_rejected(self) -> None:
        self._reset_login("会话已失效，请重新登录。未发送的草稿已保留。")

    def _check_session(self) -> None:
        if self.session is not None and not self._busy:
            self._request("session")

    def _logout(self) -> None:
        if self._busy or self.session is None:
            return
        self._leave_lobby()
        self.pages.setCurrentIndex(0)
        self._request("logout")

    def _update_controls(self) -> None:
        for field in (
            self.saved_logins, self.address, self.campaign_name, self.actor_name,
            self.password, self.remember_password,
        ):
            field.setEnabled(not self._busy)
        for button in (self.login_button, self.change_button, self.logout_button):
            button.setEnabled(not self._busy)
        self.change_button.setVisible(self.session is not None and self.lobby is not None)
        self.logout_button.setVisible(self.session is not None)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.auth.cancel()
        if self.lobby is not None:
            self.lobby._close_connection()
        event.accept()
