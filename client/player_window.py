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
from .attribute_dialogs import AttributeImportDialog, AttributeSheetDialog
from coc7_attributes import ATTRIBUTES, RESOURCES, SKILL_COLUMNS
from .history_cache import HistoryCache
from .lobby_window import DEFAULT_ADDRESS, LobbyWindow
from .login_history import LoginHistory, offline_password_matches
from .lobby_dialogs import PersonalInfoDialog

ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
LOGIN_ERRORS = {
    "campaign_mismatch": "角色信息错误",
    "actor_not_found": "角色信息错误",
    "invalid_credentials": "密码错误",
    "role_mismatch": "身份不匹配，请选择正确的 PL 或 OB。",
}

from .ui_style import BACKGROUND, BORDER, FOCUS, HOVER, SELECTION, STYLE, SURFACE, TEXT
from .window_title_bar import BAR_HEIGHT, window_layout


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


IDENTITY_STYLE = f"""
QListWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 6px; padding: 6px; outline: none; }}
QListWidget::item {{ background: {BACKGROUND}; border: 1px solid {BORDER}; border-radius: 4px; padding: 8px; }}
QListWidget::item:hover {{ background: {HOVER}; }}
QListWidget::item:selected {{ background: {SELECTION}; border-color: {FOCUS}; color: {TEXT}; }}
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
        self.resize(760, 386 + BAR_HEIGHT)
        self.setMinimumSize(680, 360 + BAR_HEIGHT)
        self.setStyleSheet(STYLE)
        self.auth = AuthClient(self)
        self.auth.completed.connect(self._auth_completed)
        self.auth.failed.connect(self._auth_failed)
        self.attribute_client = AuthClient(self)
        self.attribute_client.completed.connect(self._attributes_received)
        self.attribute_client.failed.connect(self._attribute_request_failed)
        self._attributes_busy = False
        self._attribute_import = None
        self._attribute_sheet = None
        self.session: ClientSession | None = None
        self.lobby: LobbyWindow | None = None
        self._busy = False
        self._pending_password = ""
        self._current_password = ""
        self._offline_record: dict | None = None
        self.history = LoginHistory(settings)
        self._http = None
        self._websocket = None
        self._drafts: dict[tuple[str, str], dict] = {}
        self._login_geometry = None
        self._login_minimum_size = self.minimumSize()
        self._login_was_maximized = False

        self.title_bar, self._content_layout = window_layout(self, self, "参与者登录")
        layout = self._content_layout
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        self.personal_info_dialog = PersonalInfoDialog(self)
        self.personal_info_dialog.attributes_button.clicked.connect(self._request_attributes)
        self.change_button = self.personal_info_dialog.change_button
        self.change_button.clicked.connect(self._change_password)
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
        self._current_password = self.password.text()
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
        dialog = PasswordDialog(self.personal_info_dialog)
        dialog.current_password.setText(self._current_password)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        current_password, new_password = dialog.current_password.text(), dialog.new_password.text()
        dialog.deleteLater()
        if not accepted or self.session is not session or self._busy:
            return
        self._pending_password = new_password
        self.personal_info_dialog.set_feedback("正在修改密码…")
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
                    self.feedback.setText("主机上的团或角色已变更，请关闭窗口后重新选择身份。")
                    self.password.clear()
                    self._update_controls()
                    return
            self.session = session
            self._current_password = self.password.text()
            self._save_login(self._current_password)
            self.password.clear()
            if self._offline_record is not None:
                self._offline_record = None
                self._show_lobby_identity(session.campaign_name, session.actor_name)
                self.lobby.resume(session, self._history_cache((session.campaign_id, session.member_id)))
            else:
                self._enter_lobby()
        elif operation == "password":
            if response.get("changed") is True:
                self._current_password = self._pending_password
                self.feedback.setText("密码已更新。")
                self._save_login(self._current_password)
            else:
                self.feedback.setText("主机返回了无法识别的改密结果。")
            self._pending_password = ""
            self.personal_info_dialog.set_feedback(self.feedback.text())
        elif operation == "nickname":
            self.session = replace(self.session, actor_name=response["actor_name"])
            self.lobby.session = self.session
            self.lobby.nickname.setText(self.session.actor_name)
            self.history.rename(self.session)
            self._show_lobby_identity(self.session.campaign_name, self.session.actor_name)
            self.feedback.setText("昵称已更新，下次登录请使用新昵称。")
        if self.lobby is not None and self.feedback.text():
            self.lobby.feedback.setText(self.feedback.text())
        self._update_controls()

    def _auth_failed(self, operation: str, code: str, message: str) -> None:
        if operation == "password":
            self.personal_info_dialog.set_feedback(message)
        self._busy = False
        self._pending_password = ""
        if operation == "login":
            self._login_failed(code, message)
        elif code == "invalid_session":
            self._reset_login("会话已失效，请重新登录。")
        else:
            self.feedback.setText(message)
        if self.lobby is not None and self.feedback.text():
            self.lobby.feedback.setText(self.feedback.text())
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
        self.lobby.personalInfoRequested.connect(self._open_personal_info)
        self.lobby.attributesStatusChanged.connect(self._attributes_status_changed)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
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
        target = self.size().expandedTo(QSize(1280, 800))
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
        self.title_bar.set_caption(campaign_name, actor_name)

    def _leave_lobby(self) -> None:
        self._close_attribute_dialogs()
        self.personal_info_dialog.close()
        self._current_password = ""
        self.personal_info_dialog.set_identity("", "", "", can_change=False)
        self.personal_info_dialog.set_feedback("")
        if self.lobby is not None:
            key = self._lobby_identity()
            if key is not None:
                self._drafts[key] = self.lobby.saved_drafts()
            self.lobby.shutdown()
            self.pages.removeWidget(self.lobby)
            self.lobby.deleteLater()
            self.lobby = None
            self._content_layout.setContentsMargins(22, 20, 22, 18)
            self._content_layout.setSpacing(12)
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
        self.title_bar.set_caption("参与者登录")
        self.feedback.setText(message)
        self._update_controls()

    def _session_rejected(self) -> None:
        self._reset_login("会话已失效，请重新登录。未发送的草稿已保留。")

    def _update_controls(self) -> None:
        for field in (
            self.saved_logins, self.address, self.campaign_name, self.actor_name,
            self.password, self.pl_role, self.ob_role, self.new_identity_button, self.manage_button,
        ):
            field.setEnabled(not self._busy)
        self.login_button.setEnabled(not self._busy)
        self._update_personal_info()
        self.feedback.setVisible(bool(self.feedback.text()) and self.lobby is None)

    def _update_personal_info(self) -> None:
        profile = self._offline_record or {}
        role = self.session.role if self.session is not None else profile.get("role", "")
        name = self.session.actor_name if self.session is not None else profile.get("actor_name", "")
        online = self.session is not None and self.lobby is not None and self.lobby.socket.state() == QAbstractSocket.SocketState.ConnectedState
        self.personal_info_dialog.set_identity(role, name, self._current_password, can_change=online and not self._busy)
        self.personal_info_dialog.setWindowTitle("个人属性" if role == "pl" else "个人信息")
        button = self.personal_info_dialog.attributes_button
        button.setVisible(role == "pl")
        if self.lobby is not None:
            self.lobby.personal_info_button.setText("个人属性" if role == "pl" else "个人信息")
        imported = self.lobby.attributes_imported if self.lobby is not None else None
        button.setText("导入属性" if imported is False else "查看属性")
        button.setEnabled(role == "pl" and online and self.lobby.is_online and not self._attributes_busy)
        button.setToolTip("" if online else "连接主机后可查看或导入属性。")

    def _request_attributes(self) -> None:
        if not self.personal_info_dialog.attributes_button.isEnabled():
            return
        self._attributes_busy = True
        self.personal_info_dialog.set_feedback("")
        self._update_personal_info()
        self.attribute_client.request(self._http, "attributes", token=self.session.token)

    def _submit_attributes(self, text: str) -> None:
        if self._attributes_busy or self.session is None or not self.lobby.is_online:
            self._attribute_import.set_pending(False, "请连接主机后重试。")
            return
        self._attributes_busy = True
        self._update_personal_info()
        self.attribute_client.request(self._http, "attributes", {"text": text}, self.session.token)

    def _attributes_received(self, _operation: str, response: dict) -> None:
        self._attributes_busy = False
        if self.session is None or self.lobby is None:
            return
        values = response.get("attributes")
        actor_name = response.get("actor_name")
        required = {key for key, _ in (*ATTRIBUTES[:8], *RESOURCES)}
        required.update(name for column in SKILL_COLUMNS for name, _ in column)
        if ("attributes" not in response or not isinstance(actor_name, str)
                or (values is not None and (not isinstance(values, dict) or not required.issubset(values)
                    or any(type(value) is not int or value < 0 for value in values.values())))):
            self._attribute_request_failed("attributes", "invalid_response", "主机返回的属性格式无效。")
            return
        self._update_personal_info()
        if not self.personal_info_dialog.isVisible():
            return
        if values is None:
            if self._attribute_import is None:
                dialog = AttributeImportDialog(self.personal_info_dialog)
                self._attribute_import = dialog
                dialog.finished.connect(self._attribute_import_closed)
                dialog.finished.connect(dialog.deleteLater)
                dialog.importRequested.connect(self._submit_attributes)
                dialog.open()
            return
        if self._attribute_import is not None:
            self._attribute_import.accept()
        if self._attribute_sheet is not None:
            self._attribute_sheet.close()
        dialog = AttributeSheetDialog(self.personal_info_dialog, actor_name, values)
        self._attribute_sheet = dialog
        dialog.finished.connect(self._attribute_sheet_closed)
        dialog.finished.connect(dialog.deleteLater)
        dialog.open()

    def _attribute_import_closed(self) -> None:
        self._attribute_import = None

    def _attribute_sheet_closed(self) -> None:
        self._attribute_sheet = None

    def _attribute_request_failed(self, _operation: str, code: str, message: str) -> None:
        self._attributes_busy = False
        if code == "invalid_session":
            self._session_rejected()
            return
        if code == "attributes_exist" and self._attribute_import is not None:
            self._attribute_import.reject()
            self._update_personal_info()
            self._request_attributes()
            return
        if self._attribute_import is not None:
            self._attribute_import.set_pending(False, message)
        else:
            self.personal_info_dialog.set_feedback(message)
        self._update_personal_info()

    def _attributes_status_changed(self) -> None:
        if self.lobby.attributes_imported is False and self._attribute_sheet is not None:
            self._attribute_sheet.close()
            self.personal_info_dialog.set_feedback("KP 已重置角色属性，请重新导入。")
        self._update_personal_info()

    def _close_attribute_dialogs(self) -> None:
        self.attribute_client.cancel()
        self._attributes_busy = False
        for dialog in (self._attribute_import, self._attribute_sheet):
            if dialog is not None:
                dialog.close()

    def _open_personal_info(self) -> None:
        if self.lobby is None:
            return
        self._update_personal_info()
        self.personal_info_dialog.show()
        self.personal_info_dialog.raise_()
        self.personal_info_dialog.activateWindow()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._close_attribute_dialogs()
        self.personal_info_dialog.close()
        self._current_password = ""
        self.personal_info_dialog.set_identity("", "", "", can_change=False)
        self.auth.cancel()
        if self.lobby is not None:
            self.lobby.shutdown()
        event.accept()
