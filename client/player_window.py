"""角色登录、登录记忆与大厅中的密码修改。"""

from pathlib import Path
from dataclasses import replace

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QInputDialog, QListWidget, QListWidgetItem, QPushButton, QRadioButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .auth_client import AuthClient, ClientSession, server_urls
from .history_cache import HistoryCache
from .lobby_window import DEFAULT_ADDRESS, LobbyWindow
from .login_history import LoginHistory, offline_password_matches

ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
LOGIN_ERRORS = {
    "campaign_mismatch": "角色信息错误",
    "actor_not_found": "角色信息错误",
    "invalid_credentials": "密码错误",
    "role_mismatch": "身份不匹配，请选择正确的 PL 或 OB。",
}

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


def identity_caption(record: dict) -> str:
    return f"[{record['campaign_name']}]-{record.get('role', 'pl').upper()}-{record['actor_name']}"


def add_identity_card(widget: QListWidget, record: dict) -> None:
    item = QListWidgetItem(f"{record['campaign_name']}\n{record.get('role', 'pl').upper()} - {record['actor_name']}")
    item.setData(Qt.ItemDataRole.UserRole, record)
    item.setData(Qt.ItemDataRole.AccessibleTextRole, identity_caption(record))
    item.setToolTip(identity_caption(record))
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    item.setSizeHint(QSize(180, 76))
    widget.addItem(item)


IDENTITY_STYLE = """
QListWidget { background: white; border: 1px solid #ccd6df; border-radius: 6px; padding: 6px; outline: none; }
QListWidget::item { background: #f6f8fa; border: 1px solid #ccd6df; border-radius: 4px; padding: 8px; }
QListWidget::item:selected { background: #e6eff7; border-color: #326b97; color: #203548; }
"""


def identity_list() -> QListWidget:
    widget = QListWidget()
    widget.setStyleSheet(IDENTITY_STYLE)
    widget.setSpacing(6)
    widget.setWordWrap(True)
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    return widget


class IdentityManagementDialog(QDialog):
    def __init__(self, parent: QWidget, history: LoginHistory) -> None:
        super().__init__(parent)
        self.setWindowTitle("身份管理")
        self.resize(380, 420)
        self.history = history
        self.changed = False
        layout = QVBoxLayout(self)
        self.identities = identity_list()
        self.identities.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for record in history.entries():
            add_identity_card(self.identities, record)
        layout.addWidget(self.identities, 1)
        self.feedback = label()
        layout.addWidget(self.feedback)
        buttons = QHBoxLayout()
        self.delete_button = QPushButton("删除所选身份")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete_selected)
        self.identities.itemSelectionChanged.connect(
            lambda: self.delete_button.setEnabled(bool(self.identities.selectedItems())))
        buttons.addWidget(self.delete_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _delete_selected(self) -> None:
        for item in self.identities.selectedItems():
            record = item.data(Qt.ItemDataRole.UserRole)
            if not self.history.delete(record):
                self.feedback.setText("身份删除失败，请关闭后重试。")
                return
            self.identities.takeItem(self.identities.row(item))
            self.changed = True
        self.feedback.setText("已删除本机保存的登录身份。")


class PlayerWindow(QWidget):
    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        self.setObjectName("player")
        self.setWindowTitle("参与者登录")
        self.resize(760, 386)
        self.setMinimumSize(680, 360)
        self.setStyleSheet(STYLE)
        self.auth = AuthClient(self)
        self.auth.completed.connect(self._auth_completed)
        self.auth.failed.connect(self._auth_failed)
        self.session: ClientSession | None = None
        self.lobby: LobbyWindow | None = None
        self._busy = False
        self._pending_password = ""
        self._offline_record: dict | None = None
        self.history = LoginHistory(settings)
        self._http = None
        self._websocket = None
        self._drafts: dict[tuple[str, str], dict] = {}
        self._login_geometry = None
        self._login_minimum_size = self.minimumSize()
        self._login_was_maximized = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        self.header = QWidget()
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        self.identity = label("", "muted")
        header.addWidget(self.identity)
        header.addStretch()
        self.logout_button = QPushButton("退出登录")
        self.logout_button.clicked.connect(self._logout)
        self.change_button = QPushButton("修改密码")
        self.change_button.clicked.connect(self._change_password)
        self.nickname_button = QPushButton("修改昵称")
        self.nickname_button.clicked.connect(self._change_nickname)
        header.addWidget(self.nickname_button)
        header.addWidget(self.change_button)
        header.addWidget(self.logout_button)
        layout.addWidget(self.header)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._login_page())
        layout.addWidget(self.pages, 1)
        self.feedback = label()
        layout.addWidget(self.feedback)
        self._load_history()
        self._update_controls()

    def _login_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        self.saved_logins = identity_list()
        self.saved_logins.setAccessibleName("选择身份")
        self.saved_logins.currentItemChanged.connect(self._select_saved_login)
        self.saved_logins.itemClicked.connect(self._select_saved_login)
        layout.addWidget(self.saved_logins, 1)
        right = QVBoxLayout()
        right.setSpacing(14)
        card = QFrame()
        card.setObjectName("card")
        form = QFormLayout(card)
        form.setContentsMargins(22, 20, 22, 20)
        form.setSpacing(14)
        self.address = QLineEdit()
        self.address.setPlaceholderText(DEFAULT_ADDRESS)
        self.campaign_name = QLineEdit()
        self.campaign_name.setPlaceholderText("组织者当前开启的团名")
        self.actor_name = QLineEdit()
        self.actor_name.setMaxLength(160)
        self.actor_name.setPlaceholderText("PL 填角色名，OB 填组织者创建的昵称")
        self.password = QLineEdit()
        self.password.setMaxLength(512)
        self.password.setPlaceholderText("新角色默认密码为 123456")
        role_row = QHBoxLayout()
        self.pl_role = QRadioButton("PL")
        self.ob_role = QRadioButton("OB")
        self.role_group = QButtonGroup(self)
        self.role_group.addButton(self.pl_role, 0)
        self.role_group.addButton(self.ob_role, 1)
        self.pl_role.setChecked(True)
        role_row.addWidget(self.pl_role)
        role_row.addWidget(self.ob_role)
        role_row.addStretch()
        form.addRow("团名", self.campaign_name)
        form.addRow("身份", role_row)
        form.addRow("名称", self.actor_name)
        form.addRow("密码", self.password)
        form.addRow("组织方地址", self.address)
        right.addWidget(card, 1)
        buttons = QHBoxLayout()
        self.manage_button = QPushButton("身份管理")
        self.manage_button.clicked.connect(self._manage_identities)
        self.login_button = QPushButton("登入")
        self.login_button.setObjectName("primary")
        self.login_button.clicked.connect(self._login)
        self.password.returnPressed.connect(self._login)
        self.actor_name.returnPressed.connect(self._login)
        self.new_identity_button = QPushButton("新身份")
        self.new_identity_button.clicked.connect(self._new_identity)
        for button in (self.manage_button, self.login_button, self.new_identity_button):
            buttons.addWidget(button, 1)
        right.addLayout(buttons)
        layout.addLayout(right, 2)
        return page

    def _load_history(self, selected: dict | None = None, *, select_first: bool = True) -> None:
        self.saved_logins.blockSignals(True)
        self.saved_logins.clear()
        selected_row = -1
        for record in self.history.entries():
            add_identity_card(self.saved_logins, record)
            if selected is not None and record == selected:
                selected_row = self.saved_logins.count() - 1
        if selected_row < 0 and select_first and self.saved_logins.count():
            selected_row = 0
        self.saved_logins.setCurrentRow(selected_row)
        self.saved_logins.blockSignals(False)
        self._select_saved_login()

    def _selected_role(self) -> str:
        return "ob" if self.ob_role.isChecked() else "pl"

    def _selected_record(self) -> dict | None:
        item = self.saved_logins.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _select_saved_login(self, *_args: object) -> None:
        record = self._selected_record()
        self.address.setText(record["address"] if record else "")
        self.campaign_name.setText(record["campaign_name"] if record else "")
        self.actor_name.setText(record["actor_name"] if record else "")
        self.password.setText(record["password"] if record else "")
        selected_role = self.ob_role if record and record.get("role", "pl") == "ob" else self.pl_role
        selected_role.setChecked(True)

    def _new_identity(self) -> None:
        self.saved_logins.setCurrentRow(-1)
        self.saved_logins.clearSelection()
        for field in (self.campaign_name, self.actor_name, self.password, self.address):
            field.clear()
        self.pl_role.setChecked(True)
        self.feedback.clear()
        self.campaign_name.setFocus()

    def _manage_identities(self) -> None:
        selected = self._selected_record()
        dialog = IdentityManagementDialog(self, self.history)
        dialog.exec()
        if dialog.changed:
            self._load_history(selected, select_first=False)
        dialog.deleteLater()

    def _saved_profile(self) -> dict | None:
        campaign = self.campaign_name.text().strip()
        actor = self.actor_name.text().strip().translate(ASCII_LOWER)
        for record in self.history.entries():
            if (record["campaign_name"] != campaign or record["actor_name"].translate(ASCII_LOWER) != actor
                    or record.get("role", "pl") != self._selected_role()):
                continue
            try:
                _, address = server_urls(record["address"])
            except ValueError:
                continue
            if self._websocket == address:
                return record
        return None

    def _open_local_history(self, record: dict) -> None:
        self._http, self._websocket = server_urls(record["address"])
        self._offline_record = record
        self.password.clear()
        self.feedback.setText("正在查看本机已同步的历史。" if self._lobby_identity() else "本机暂无已同步的历史。")
        self._enter_lobby()
        self._update_controls()

    def _login_from_history(self) -> None:
        if self._busy or self._offline_record is None:
            return
        record = self._offline_record
        password = record["password"]
        if not password:
            password, accepted = QInputDialog.getText(self, "重新连接", "身份密码", QLineEdit.EchoMode.Normal)
            if not accepted or not 1 <= len(password) <= 256:
                return
        self.password.setText(password)
        self._request("login", {"campaign_name": record["campaign_name"],
                                "actor_name": record["actor_name"], "password": password,
                                "role": record.get("role", "pl")})

    def _save_login(self, password: str) -> None:
        if not self.history.save(self.session, self._websocket.toString(), password,
                                 remember=True):
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
        actor_name, password = self.actor_name.text().strip(), self.password.text()
        if not campaign_name or not 1 <= len(actor_name) <= 80:
            self.feedback.setText("角色信息错误")
            return
        self._request("login", {"campaign_name": campaign_name, "actor_name": actor_name,
                                "password": password, "role": self._selected_role()})

    def _request(self, operation: str, body: dict | None = None) -> None:
        self._busy = True
        self.feedback.setText("正在等待主机响应…")
        self._update_controls()
        self.auth.request(self._http, operation, body, self.session.token if self.session else "")

    def _change_password(self) -> None:
        if self._busy or not self.change_button.isEnabled():
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
                session = ClientSession.from_response(response)
                expected_role = (self._offline_record or {}).get("role", self._selected_role())
                if session.role != expected_role:
                    raise ValueError(LOGIN_ERRORS["role_mismatch"])
            except ValueError as error:
                self.feedback.setText(str(error))
                self._update_controls()
                return
            if self._offline_record is not None:
                identity = self._lobby_identity()
                if identity is not None and (session.campaign_id, session.member_id) != identity:
                    self.feedback.setText("主机上的团或角色已变更，请返回登录页重新选择。")
                    self.password.clear()
                    self._update_controls()
                    return
            self.session = session
            self._save_login(self.password.text())
            self.password.clear()
            if self._offline_record is not None:
                self._offline_record = None
                self._show_lobby_identity(session.campaign_name, session.actor_name)
                self.lobby.resume(session, self._history_cache((session.campaign_id, session.member_id)))
            else:
                self._enter_lobby()
        elif operation == "password":
            if response.get("changed") is True:
                self.feedback.setText("密码已更新。")
                self._save_login(self._pending_password)
            else:
                self.feedback.setText("主机返回了无法识别的改密结果。")
            self._pending_password = ""
        elif operation == "nickname":
            self.session = replace(self.session, actor_name=response["actor_name"])
            self.lobby.session = self.session
            self.lobby.nickname.setText(self.session.actor_name)
            self.history.rename(self.session)
            self._show_lobby_identity(self.session.campaign_name, self.session.actor_name)
            self.feedback.setText("昵称已更新，下次登录请使用新昵称。")
        elif operation == "logout":
            self._reset_login("已退出登录。")
        self._update_controls()

    def _auth_failed(self, operation: str, code: str, message: str) -> None:
        self._busy = False
        self._pending_password = ""
        if operation == "login":
            self._login_failed(code, message)
        elif operation == "logout":
            message = "已退出登录。" if code == "invalid_session" else "已清除本机登录，主机未确认退出。"
            self._reset_login(message)
        elif code == "invalid_session":
            self._reset_login("会话已失效，请重新登录。")
        else:
            self.feedback.setText(message)
        self._update_controls()

    def _login_failed(self, code: str, message: str) -> None:
        password = self.password.text()
        if code == "host_unreachable" and self._offline_record is None:
            record = self._saved_profile()
            if record is None:
                self.feedback.setText("角色信息错误")
            elif not offline_password_matches(record, password):
                self.feedback.setText("密码错误")
            else:
                self._open_local_history(record)
            return

        if code == "invalid_credentials":
            record = self._offline_record or self._saved_profile()
            if record is not None:
                self.history.forget_rejected_password(record, password)
            if self._offline_record is not None:
                self._offline_record["password"] = ""
        if self._offline_record is not None:
            self.password.clear()
        self.feedback.setText(LOGIN_ERRORS.get(code, message))

    def _lobby_identity(self) -> tuple[str, str] | None:
        if self.session is not None:
            return self.session.campaign_id, self.session.member_id
        if self._offline_record is not None:
            campaign, member = self._offline_record.get("campaign_id"), self._offline_record.get("member_id")
            if isinstance(campaign, str) and campaign and isinstance(member, str) and member:
                return campaign, member
        return None

    def _history_cache(self, identity: tuple[str, str]) -> HistoryCache:
        return HistoryCache(Path(self.history.settings.fileName()).with_name("history.sqlite3"), *identity)

    def _enter_lobby(self) -> None:
        self._login_geometry = self.geometry()
        self._login_minimum_size = self.minimumSize()
        self._login_was_maximized = self.isMaximized()
        profile = self._offline_record
        campaign_name = self.session.campaign_name if self.session is not None else profile["campaign_name"]
        actor_name = self.session.actor_name if self.session is not None else profile["actor_name"]
        key = self._lobby_identity()
        cache = self._history_cache(key) if key is not None else None
        self.lobby = LobbyWindow(session=self.session, address=self._websocket.toString(), cache=cache,
                                 offline_actor=actor_name)
        self.lobby.loginRequested.connect(self._login_from_history)
        self.lobby.sessionRejected.connect(self._session_rejected)
        self.lobby.socket.stateChanged.connect(self._update_controls)
        if key is not None:
            self.lobby.restore_drafts(self._drafts.pop(key, {}))
        self.pages.addWidget(self.lobby)
        self.pages.setCurrentWidget(self.lobby)
        self._show_lobby_identity(campaign_name, actor_name)
        self._update_controls()
        self._adapt_lobby_size()
        if self.session is not None:
            self.lobby._open_connection()

    def _adapt_lobby_size(self) -> None:
        self.layout().activate()
        minimum = self.minimumSizeHint().expandedTo(QSize(880, 620))
        target = self.size().expandedTo(QSize(960, 720))
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            decorations = self.frameGeometry().size() - self.size()
            limit = available.size() - decorations
            minimum = minimum.boundedTo(limit)
            target = target.boundedTo(limit)
        self.setMinimumSize(minimum)
        if not self.isMaximized():
            self.resize(target.expandedTo(minimum))
            if screen is not None:
                frame = self.frameGeometry()
                self.move(max(available.left(), min(frame.left(), available.right() - frame.width() + 1)),
                          max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1)))

    def _show_lobby_identity(self, campaign_name: str, actor_name: str) -> None:
        role = self.session.role if self.session else (self._offline_record or {}).get("role", "pl")
        self.identity.setText(f"[{campaign_name}]-{actor_name}" + (" · OB 观众" if role == "ob" else ""))
        self.setWindowTitle(f"Story · {actor_name}")

    def _leave_lobby(self) -> None:
        if self.lobby is not None:
            key = self._lobby_identity()
            if key is not None:
                self._drafts[key] = self.lobby.saved_drafts()
            self.lobby._close_connection()
            self.pages.removeWidget(self.lobby)
            self.lobby.deleteLater()
            self.lobby = None
            self.header.hide()
            self.setMinimumSize(self._login_minimum_size)
            if self._login_geometry is not None:
                self.layout().activate()
                if self._login_was_maximized:
                    self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
                else:
                    self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMaximized)
                    self.setGeometry(self._login_geometry)
                self._login_geometry = None

    def _reset_login(self, message: str) -> None:
        self.auth.cancel()
        self._leave_lobby()
        self.session = None
        self._offline_record = None
        self._busy = False
        self._pending_password = ""
        self._load_history()
        self.pages.setCurrentIndex(0)
        self.identity.clear()
        self.setWindowTitle("参与者登录")
        self.feedback.setText(message)
        self._update_controls()

    def _session_rejected(self) -> None:
        self._reset_login("会话已失效，请重新登录。未发送的草稿已保留。")

    def _logout(self) -> None:
        if self._busy:
            return
        if self._offline_record is not None:
            self._reset_login("")
            return
        if self.session is None:
            return
        self._leave_lobby()
        self.pages.setCurrentIndex(0)
        self._request("logout")

    def _update_controls(self) -> None:
        for field in (
            self.saved_logins, self.address, self.campaign_name, self.actor_name,
            self.password, self.pl_role, self.ob_role, self.new_identity_button, self.manage_button,
        ):
            field.setEnabled(not self._busy)
        for button in (self.login_button, self.change_button, self.logout_button):
            button.setEnabled(not self._busy)
        online = self.lobby is not None and self.lobby.socket.state() == QAbstractSocket.SocketState.ConnectedState
        self.change_button.setEnabled(online and not self._busy)
        self.change_button.setVisible(online)
        is_ob = self.session is not None and self.session.role == "ob"
        self.nickname_button.setVisible(is_ob)
        self.nickname_button.setEnabled(online and is_ob and not self._busy)
        self.logout_button.setText("返回登录" if self._offline_record is not None else "退出登录")
        self.logout_button.setVisible(self.lobby is not None or self.session is not None)
        self.header.setVisible(self.lobby is not None)

    def _change_nickname(self) -> None:
        if not self.nickname_button.isEnabled() or self.session is None:
            return
        session = self.session
        nickname, accepted = QInputDialog.getText(self, "修改 OB 昵称", "昵称", text=session.actor_name)
        if accepted and self.session is session and not self._busy:
            self._request("nickname", {"nickname": nickname})

    def closeEvent(self, event: QCloseEvent) -> None:
        self.auth.cancel()
        if self.lobby is not None:
            self.lobby._close_connection()
        event.accept()
