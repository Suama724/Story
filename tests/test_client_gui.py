"""真实 Qt 角色登录、改密与聊天的验收。"""

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWidgets import QApplication, QLineEdit

from client.auth_client import ClientSession, server_urls
from client.history_cache import HistoryCache
from client.player_window import IdentityManagementDialog, PasswordDialog, PlayerWindow
from host.auth import AuthError, AuthService, DEFAULT_PASSWORD
from host.channels import archive_scene, create_scene
from host.management import add_member
from host.messages import save_lobby_message
from host.server_controller import ServerController
from host.storage import initialize_campaign
from tests.attribute_fixtures import add_ready_player

PASSWORD = "我的新密码🎲123456"
CONNECTED = QAbstractSocket.SocketState.ConnectedState
UNCONNECTED = QAbstractSocket.SocketState.UnconnectedState


class AddressTests(unittest.TestCase):
    def test_malformed_tokens_are_rejected_before_building_headers(self) -> None:
        response = {
            "member_id": "member", "campaign_id": "campaign",
            "campaign_name": "团本", "actor_name": "调查员",
        }
        for token in ("🎲" * 43, "a" * 42 + "\n", "a" * 42):
            with self.subTest(length=len(token)), self.assertRaises(ValueError):
                ClientSession.from_response({**response, "token": token})

    def test_http_and_websocket_share_origin_and_prefix(self) -> None:
        cases = (
            ("ws://127.0.0.1:8000/ws/lobby", "http://127.0.0.1:8000/", "ws://127.0.0.1:8000/ws/lobby"),
            ("https://example.com/story", "https://example.com/story/", "wss://example.com/story/ws/lobby"),
            ("wss://example.com/story/ws/lobby", "https://example.com/story/", "wss://example.com/story/ws/lobby"),
            ("http://[::1]:8000", "http://[::1]:8000/", "ws://[::1]:8000/ws/lobby"),
        )
        for text, expected_http, expected_ws in cases:
            with self.subTest(text=text):
                http, websocket = server_urls(text)
                self.assertEqual(http.toString(), expected_http)
                self.assertEqual(websocket.toString(), expected_ws)

    def test_embedded_credentials_query_and_invalid_addresses_are_rejected(self) -> None:
        for text in ("localhost", "ftp://localhost", "ws://localhost/wrong",
                     "http://user:password@localhost", "ws://localhost/ws/lobby?token=secret",
                     "http://localhost/#fragment"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                server_urls(text)


class HistoryCacheTests(unittest.TestCase):
    def test_scope_and_sync_position_survive_reopen_without_skipping_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            cache = HistoryCache(path, "campaign", "member")

            def message(sequence):
                return {"message_id": str(sequence), "sequence": sequence, "nickname": "KP", "text": "正文"}

            cache.save([message(7), message(8)], latest=True)
            cache.save([message(10)])
            reopened = HistoryCache(path, "campaign", "member")
            self.assertEqual(reopened.position(), 8)
            reopened.save([message(9), message(10)])
            self.assertEqual(reopened.position(), 10)
            self.assertEqual(reopened.page(before=9), [message(7), message(8)])
            self.assertEqual(HistoryCache(path, "campaign", "other-member").page(), [])
            self.assertEqual(HistoryCache(path, "other-campaign", "member").page(), [])


class ClientGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = initialize_campaign(Path(self.temporary.name), "玩家联调团")
        add_ready_player(self.directory, "调查员甲")
        self.settings_path = str(Path(self.temporary.name) / "player.ini")
        self.service = AuthService(self.directory)
        self.server = ServerController()
        self.server_errors = []
        self.server_messages = []
        self.server.errorOccurred.connect(self.server_errors.append)
        self.server.messageReceived.connect(self.server_messages.append)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.address = f"ws://127.0.0.1:{self.port}/ws/lobby"
        self.server.start(self.directory, "127.0.0.1", self.port)
        self.players = []
        self.wait_until(lambda: self.server.state == "running")
        self.player = self.new_player()

    def tearDown(self) -> None:
        for player in self.players:
            player.close()
        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        for player in self.players:
            player.deleteLater()
        self.server.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def wait_until(self, condition, timeout=15) -> None:
        deadline = time.monotonic() + timeout
        while True:
            self.app.processEvents()
            if condition():
                return
            if time.monotonic() > deadline:
                feedback = [player.feedback.text() for player in self.players]
                self.fail(f"客户端等待超时，{feedback}，{self.server_errors}")
            time.sleep(0.01)

    def new_player(self) -> PlayerWindow:
        player = PlayerWindow(QSettings(self.settings_path, QSettings.Format.IniFormat))
        player.address.setText(self.address)
        player.campaign_name.setText("玩家联调团")
        player.actor_name.setText("调查员甲")
        self.players.append(player)
        return player

    def login(self, player: PlayerWindow, password: str) -> None:
        player.password.setText(password)
        player.login_button.click()
        self.wait_until(lambda: not player._busy)

    def ready_player(self) -> None:
        self.login(self.player, DEFAULT_PASSWORD)
        self.wait_until(lambda: self.player.lobby is not None and self.player.lobby._channel_state_ready)

    def test_first_login_enters_lobby_and_binds_sender(self) -> None:
        self.ready_player()
        self.assertIn("调查员甲", self.player.windowTitle())
        self.assertTrue(self.player.lobby.nickname.isReadOnly())
        self.server.command("say", text="欢迎调查员 🎲")
        self.wait_until(lambda: "欢迎调查员 🎲" in self.player.lobby.chat_history.toPlainText())
        self.player.lobby.message.setPlainText("中文 🎲\n第二行  ")
        self.player.lobby.send_button.click()
        self.wait_until(lambda: not self.player.lobby._awaiting_send)
        self.assertEqual(self.player.lobby.message.toPlainText(), "")
        self.player.lobby.socket.sendTextMessage(json.dumps({
            "type": "chat.send", "text": "伪造发言人", "nickname": "KP", "member_id": "someone-else",
        }))
        self.wait_until(lambda: "伪造发言人" in self.player.lobby.chat_history.toPlainText())
        history = self.player.lobby.chat_history.toPlainText()
        self.assertEqual(history.count("调查员甲：中文 🎲\n第二行  "), 1)
        self.assertIn("调查员甲：伪造发言人", history)

    def test_lobby_resizes_and_restores_login_window_online_and_offline(self) -> None:
        self.player.show()
        self.player.resize(760, 386)
        self.app.processEvents()
        login_geometry = self.player.geometry()
        login_minimum = self.player.minimumSize()
        self.ready_player()
        self.assertGreater(self.player.height(), login_geometry.height())
        self.assertGreater(self.player.minimumHeight(), login_minimum.height())
        self.assertGreater(self.player.lobby.chat_history.height(), 200)
        self.player._session_rejected()
        self.wait_until(lambda: not self.player._busy)
        self.assertEqual(self.player.geometry(), login_geometry)
        self.assertEqual(self.player.minimumSize(), login_minimum)
        self.assertFalse(hasattr(self.player, "header"))

        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        self.login(self.player, DEFAULT_PASSWORD)
        self.assertIsNotNone(self.player.lobby)
        self.assertIsNone(self.player.session)
        self.assertGreater(self.player.height(), login_geometry.height())
        self.assertGreater(self.player.lobby.chat_history.height(), 200)
        self.player._session_rejected()
        self.assertEqual(self.player.geometry(), login_geometry)
        self.assertEqual(self.player.minimumSize(), login_minimum)

    def test_observer_sees_old_and_new_scenes_but_can_only_chat_in_lobby(self) -> None:
        scene = create_scene(self.directory, "活动现场", [])
        archive = create_scene(self.directory, "已结束现场", [])
        for channel in (scene, archive):
            save_lobby_message(self.directory, f"history-{channel}", "OB 加入前的完整记录", token=None, channel_id=channel)
        archive_scene(self.directory, archive)
        add_member(self.directory, "旁观者", role="ob", password="观众密码")
        self.player.actor_name.setText("旁观者")
        self.player.ob_role.setChecked(True)
        self.login(self.player, "观众密码")
        self.wait_until(lambda: self.player.lobby is not None and self.player.lobby._channel_state_ready)
        ob = self.player.lobby
        self.assertEqual(self.player.session.role, "ob")
        self.assertEqual(self.player.personal_info_dialog.identity.text(), "OB")
        self.assertFalse(hasattr(self.player, "header"))
        for channel in (scene, archive):
            ob.channel_choice.setCurrentIndex(ob.channel_choice.findData(channel))
            self.wait_until(lambda: "OB 加入前的完整记录" in ob.chat_history.toPlainText())
            self.assertTrue(ob.message.isReadOnly())
            self.assertFalse(ob.send_button.isEnabled())
        self.server.command("scene.create", name="后来创建的现场", member_ids=[], channel_id="future-scene")
        self.wait_until(lambda: "future-scene" in ob._channels)
        replies = []
        ob.socket.textMessageReceived.connect(lambda payload: replies.append(json.loads(payload)))
        ob.socket.sendTextMessage(json.dumps({"type": "chat.send", "channel_id": scene, "text": "越权发言", "request_id": "ob-forged"}))
        self.wait_until(lambda: any(item.get("request_id") == "ob-forged" for item in replies))
        self.assertEqual(next(item for item in replies if item.get("request_id") == "ob-forged")["code"], "channel_forbidden")
        ob.channel_choice.setCurrentIndex(ob.channel_choice.findData(ob._assigned_channel_id))
        self.wait_until(lambda: ob.send_button.isEnabled())
        ob.message.setPlainText("OB 场外聊天")
        ob.send_button.click()
        self.wait_until(lambda: "旁观者：OB 场外聊天" in ob.chat_history.toPlainText())
        self.player._request("nickname", {"nickname": "新昵称"})
        self.wait_until(lambda: not self.player._busy)
        self.assertEqual(self.player.session.actor_name, "新昵称")
        self.assertEqual(self.player.history.entries()[0]["actor_name"], "新昵称")
        ob.message.setPlainText("改名后仍在线")
        ob.send_button.click()
        self.wait_until(lambda: "新昵称：改名后仍在线" in ob.chat_history.toPlainText())
        self.player._pending_password = "新观众密码"
        self.player._request("password", {"current_password": "观众密码", "new_password": "新观众密码"})
        self.wait_until(lambda: not self.player._busy)
        self.assertEqual(self.player.feedback.text(), "密码已更新。")
        self.assertEqual(self.service.require_session(self.player.session.token).role, "ob")
        self.assertEqual(self.player.history.entries()[0]["password"], "新观众密码")

    def test_wrong_password_stays_on_login(self) -> None:
        for password in ("", "wrong"):
            self.login(self.player, password)
            self.assertIsNone(self.player.session)
            self.assertEqual(self.player.pages.currentIndex(), 0)
            self.assertEqual(self.player.feedback.text(), "密码错误")
            self.assertTrue(self.player.login_button.isEnabled())

    def test_identity_cards_new_form_and_management_only_remove_local_memory(self) -> None:
        self.ready_player()
        original_token = self.player.session.token
        self.server.command("say", text="删除登录记忆也保留历史")
        self.wait_until(lambda: "删除登录记忆也保留历史" in self.player.lobby.chat_history.toPlainText())
        cache = self.player.lobby._cache
        add_member(self.directory, "观众甲", role="ob", password="ob-password")
        second = self.new_player()
        second.actor_name.setText("观众甲")
        second.ob_role.setChecked(True)
        self.login(second, "ob-password")
        self.wait_until(lambda: second.lobby is not None and second.lobby._channel_state_ready)
        ob_token = second.session.token
        self.player._reset_login("")
        self.assertEqual(self.player.windowTitle(), "参与者登录")
        self.assertFalse(hasattr(self.player, "header"))
        self.assertEqual(self.player.saved_logins.currentItem().toolTip(), "[玩家联调团]-OB-观众甲")
        self.assertTrue(self.player.ob_role.isChecked())
        self.assertEqual(self.player.password.echoMode(), QLineEdit.EchoMode.Normal)
        self.assertEqual(self.player.password.text(), "ob-password")
        self.player.saved_logins.setCurrentRow(1)
        self.assertTrue(self.player.pl_role.isChecked())
        self.assertEqual(self.player.actor_name.text(), "调查员甲")
        self.player.new_identity_button.click()
        for field in (self.player.campaign_name, self.player.actor_name, self.player.password, self.player.address):
            self.assertEqual(field.text(), "")
        self.assertEqual(self.player.saved_logins.count(), 2)
        self.player.saved_logins.setCurrentRow(0)

        def delete_ob_record():
            dialog = self.player.findChild(IdentityManagementDialog)
            dialog.identities.setCurrentRow(0)
            dialog.delete_button.click()
            dialog.accept()

        QTimer.singleShot(0, delete_ob_record)
        self.player.manage_button.click()
        self.assertEqual(self.player.saved_logins.count(), 1)
        self.assertIsNone(self.player.saved_logins.currentItem())
        self.assertEqual(self.player.actor_name.text(), "")
        self.assertEqual(self.service.require_session(ob_token).role, "ob")
        self.assertEqual(self.service.require_session(original_token).role, "pl")
        self.assertTrue(any(message["text"] == "删除登录记忆也保留历史" for message in cache.page()))
        self.player.saved_logins.setCurrentRow(0)
        self.assertEqual(self.player.password.text(), DEFAULT_PASSWORD)

    def test_selected_role_is_checked_online_and_for_offline_history(self) -> None:
        self.player.ob_role.setChecked(True)
        self.login(self.player, DEFAULT_PASSWORD)
        self.assertIsNone(self.player.session)
        self.assertIn("身份不匹配", self.player.feedback.text())
        self.player.pl_role.setChecked(True)
        self.ready_player()
        self.player.close()
        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        reopened = self.new_player()
        reopened.ob_role.setChecked(True)
        self.login(reopened, DEFAULT_PASSWORD)
        self.assertIsNone(reopened.lobby)
        reopened.pl_role.setChecked(True)
        self.login(reopened, DEFAULT_PASSWORD)
        self.assertIsNotNone(reopened.lobby)
        self.assertIsNone(reopened.session)

    def test_campaign_name_is_required_and_checked_by_host(self) -> None:
        for name in ("", "别的团"):
            self.player.campaign_name.setText(name)
            self.login(self.player, DEFAULT_PASSWORD)
            self.assertIsNone(self.player.session)
            self.assertEqual(self.player.pages.currentIndex(), 0)
            self.assertEqual(self.player.feedback.text(), "角色信息错误")
        self.player.campaign_name.setText("玩家联调团")
        self.player.actor_name.setText("不存在的角色")
        self.login(self.player, "")
        self.assertIsNone(self.player.lobby)
        self.assertEqual(self.player.feedback.text(), "角色信息错误")
        self.player.actor_name.setText("调查员甲")
        self.ready_player()

    def test_password_dialog_and_recent_login_switching(self) -> None:
        self.ready_player()
        socket_before_change = self.player.lobby.socket

        def fill_password_dialog():
            dialog = self.player.findChild(PasswordDialog)
            dialog.current_password.setText(DEFAULT_PASSWORD)
            dialog.new_password.setText(PASSWORD)
            dialog.confirm_password.setText(PASSWORD)
            dialog.accept()

        QTimer.singleShot(0, fill_password_dialog)
        self.player.change_button.click()
        self.wait_until(lambda: not self.player._busy)
        self.assertIn("密码已更新", self.player.feedback.text())
        self.assertIs(self.player.lobby.socket, socket_before_change)
        self.assertEqual(socket_before_change.state(), CONNECTED)

        add_ready_player(self.directory, "调查员乙")
        second = self.new_player()
        second.actor_name.setText("调查员乙")
        self.login(second, DEFAULT_PASSWORD)
        reopened = PlayerWindow(QSettings(self.settings_path, QSettings.Format.IniFormat))
        self.players.append(reopened)
        self.assertIsNone(reopened.session)
        self.assertEqual(reopened.saved_logins.currentItem().toolTip(), "[玩家联调团]-PL-调查员乙")
        reopened.saved_logins.setCurrentRow(1)
        self.assertEqual(reopened.campaign_name.text(), "玩家联调团")
        self.assertEqual(reopened.actor_name.text(), "调查员甲")
        self.assertEqual(reopened.address.text(), self.address)
        self.assertEqual(reopened.password.text(), PASSWORD)
        reopened.new_identity_button.click()
        self.assertIsNone(reopened.saved_logins.currentItem())
        self.assertEqual(reopened.campaign_name.text(), "")
        self.assertEqual(reopened.actor_name.text(), "")
        self.assertEqual(reopened.password.text(), "")
        self.assertEqual(reopened.address.text(), "")

    def test_relogin_replaces_old_qt_client_and_restores_its_draft(self) -> None:
        self.ready_player()
        self.player.lobby.message.setPlainText("被顶替时保留的草稿")
        second = self.new_player()
        self.login(second, DEFAULT_PASSWORD)
        self.wait_until(lambda: second.lobby is not None and second.lobby.socket.state() == CONNECTED)
        self.wait_until(lambda: self.player.session is None)
        self.assertEqual(self.player.pages.currentIndex(), 0)
        self.assertIn("失效", self.player.feedback.text())
        self.login(self.player, DEFAULT_PASSWORD)
        self.wait_until(lambda: self.player.lobby is not None and self.player.lobby.socket.state() == CONNECTED)
        self.wait_until(lambda: second.session is None)
        self.assertEqual(self.player.lobby.message.toPlainText(), "被顶替时保留的草稿")

    def test_server_revocation_restores_saved_login(self) -> None:
        data_dir = Path(self.temporary.name) / "app-data"
        data_dir.mkdir()
        # 隔离旧注册表中的真实用户记录，走默认设置文件路径。
        (data_dir / "player.ini").touch()
        with patch("client.login_history.QStandardPaths.writableLocation", return_value=str(data_dir)):
            player = PlayerWindow()
            self.players.append(player)
            self.assertEqual(player.saved_logins.count(), 0)
            player.address.setText(self.address)
            player.campaign_name.setText("玩家联调团")
            player.actor_name.setText("调查员甲")
            self.login(player, DEFAULT_PASSWORD)
            self.wait_until(lambda: player.lobby is not None and player.lobby.socket.state() == CONNECTED)
            token = player.session.token
            self.service.logout(player.session.token)
            player._session_rejected()
            self.wait_until(lambda: not player._busy)
            self.assertIsNone(player.session)
            self.assertIsNone(player.lobby)
            self.assertEqual(player.saved_logins.currentItem().toolTip(), "[玩家联调团]-PL-调查员甲")
            player.close()

            reopened = PlayerWindow()
            self.players.append(reopened)
            self.assertIsNone(reopened.session)
            self.assertEqual(reopened.saved_logins.currentItem().toolTip(), "[玩家联调团]-PL-调查员甲")
            self.assertEqual(reopened.campaign_name.text(), "玩家联调团")
            self.assertEqual(reopened.actor_name.text(), "调查员甲")
            self.assertEqual(reopened.address.text(), self.address)
            self.assertEqual(reopened.password.text(), DEFAULT_PASSWORD)
        with self.assertRaises(AuthError):
            self.service.require_session(token)

    def test_lost_confirmation_keeps_draft_and_reuses_request_after_automatic_reconnect(self) -> None:
        self.ready_player()
        lobby = self.player.lobby
        draft = "停服时的草稿 🎲"
        lobby.socket.textMessageReceived.disconnect(lobby._show_message)
        lobby.message.setPlainText(draft)
        lobby.send_button.click()
        self.wait_until(lambda: any(event["text"] == draft for event in self.server_messages))
        self.assertTrue(lobby._awaiting_send)
        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        self.wait_until(lambda: not self.player._busy)
        self.assertIsNotNone(self.player.session)
        self.assertEqual(lobby.message.toPlainText(), draft)

        lobby.socket.textMessageReceived.connect(lobby._show_message)
        self.server.start(self.directory, "127.0.0.1", self.port)
        self.wait_until(lambda: self.server.state == "running")
        self.wait_until(lambda: lobby.socket.state() == CONNECTED)
        self.server.command("say", text="重启后的新消息")
        self.wait_until(lambda: "重启后的新消息" in lobby.chat_history.toPlainText())
        # 实时消息可能先于频道权限恢复；按钮可用后才模拟用户重发。
        self.wait_until(lobby.send_button.isEnabled)
        lobby.send_button.click()
        self.wait_until(lambda: not lobby._awaiting_send and lobby.message.toPlainText() == "")
        self.assertEqual(lobby.message.toPlainText(), "")
        self.assertEqual(lobby.chat_history.toPlainText().splitlines(), [
            f"调查员甲：{draft}", "KP：重启后的新消息",
        ])
        self.assertEqual(sum(event["text"] == draft for event in self.server_messages), 1)

    def test_missing_pong_reconnects_and_keeps_draft(self) -> None:
        self.ready_player()
        lobby = self.player.lobby
        lobby.message.setPlainText("还没写完的调查记录")
        states = []
        lobby.socket.stateChanged.connect(states.append)
        with (
            patch("client.lobby_window.PING_INTERVAL_SECONDS", 1),
            patch("client.lobby_window.PONG_TIMEOUT_SECONDS", 1),
        ):
            lobby._heartbeat_timer.start(100)
            pongs = []
            lobby.socket.pong.connect(lambda _elapsed, payload: pongs.append(payload))
            self.wait_until(lambda: bool(pongs))
            self.assertIsNone(lobby._ping_payload)
            self.assertNotIn(UNCONNECTED, states)

            lobby.socket.pong.disconnect(lobby._pong_received)
            self.wait_until(lambda: UNCONNECTED in states)
            lobby.socket.pong.connect(lobby._pong_received)
            self.wait_until(lambda: lobby.socket.state() == CONNECTED)
            self.assertEqual(lobby.message.toPlainText(), "还没写完的调查记录")
            self.assertIsNotNone(self.player.session)

    def test_retries_stop_and_manual_disconnect_cancels_backoff(self) -> None:
        self.ready_player()
        lobby = self.player.lobby
        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        self.wait_until(lobby._reconnect_timer.isActive)
        lobby.disconnect_button.click()
        self.assertFalse(lobby._reconnect_timer.isActive())
        self.assertTrue(lobby.connect_button.isEnabled())

        with socket.socket() as silent_host:
            silent_host.bind(("127.0.0.1", self.port))
            silent_host.listen()
            with (
                patch("client.lobby_window.CONNECT_TIMEOUT_MS", 120),
                patch("client.lobby_window.RECONNECT_DELAYS_MS", (60, 120)),
            ):
                lobby.connect_button.click()
                self.wait_until(lambda: "自动重连失败" in lobby.feedback.text())
                self.assertEqual(lobby._retry_count, 2)
                self.assertTrue(lobby.connection_status.text().endswith("-未连接"))
                self.assertTrue(lobby.connect_button.isEnabled())
                self.assertFalse(lobby._reconnect_timer.isActive())

        self.server.start(self.directory, "127.0.0.1", self.port)
        self.wait_until(lambda: self.server.state == "running")
        self.assertEqual(lobby.socket.state(), UNCONNECTED)
        lobby.connect_button.click()
        self.wait_until(lambda: lobby.socket.state() == CONNECTED)

    def test_session_revoked_while_disconnected_returns_to_login_with_draft(self) -> None:
        self.ready_player()
        lobby = self.player.lobby
        lobby.message.setPlainText("等登录后继续写")
        token = self.player.session.token
        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        self.service.logout(token)
        self.server.start(self.directory, "127.0.0.1", self.port)
        self.wait_until(lambda: self.player.session is None)
        self.assertEqual(self.player.pages.currentIndex(), 0)
        self.assertIn("会话已失效", self.player.feedback.text())
        self.ready_player()
        self.assertEqual(self.player.lobby.message.toPlainText(), "等登录后继续写")

    def test_automatic_reconnect_pages_history_and_merges_overlapping_live_message(self) -> None:
        self.ready_player()
        lobby = self.player.lobby
        self.server.command("say", text="掉线前")
        self.wait_until(lambda: "掉线前" in lobby.chat_history.toPlainText())
        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        for number in range(1, 56):
            save_lobby_message(self.directory, f"gap-{number}", f"漏收 {number:02d}", token=None)

        request_history = lobby._request_history
        requested = []

        def hold_first_page(mode, cursor):
            requested.append((mode, cursor))
            if len(requested) > 1:
                request_history(mode, cursor)

        with patch.object(lobby, "_request_history", side_effect=hold_first_page):
            self.server.start(self.directory, "127.0.0.1", self.port)
            self.wait_until(lambda: bool(requested))
            self.server.command("say", text="同步时的新线索 🎲")
            self.wait_until(lambda: bool(lobby._live_during_sync))
            self.assertNotIn("新线索", lobby.chat_history.toPlainText())
            request_history(*requested[0])
            self.wait_until(lambda: lobby._live_during_sync is None)

        self.assertEqual(requested, [("after", 1), ("after", 51)])
        self.assertEqual(lobby.chat_history.toPlainText().splitlines(), [
            "KP：掉线前", *(f"KP：漏收 {number:02d}" for number in range(1, 56)), "KP：同步时的新线索 🎲",
        ])
        self.assertEqual(lobby._cache.position(), 57)

    def test_saved_role_reads_paged_history_offline_and_catches_up_when_reconnected(self) -> None:
        def populate(first, last):
            for number in range(first, last + 1):
                save_lobby_message(self.directory, f"seed-{number}", f"历史 {number:03d}", token=None)

        populate(1, 55)
        self.ready_player()
        self.assertEqual(self.player.history.entries()[0]["password"], DEFAULT_PASSWORD)
        lobby = self.player.lobby
        self.wait_until(lambda: "历史 055" in lobby.chat_history.toPlainText())
        self.assertEqual(len(lobby.chat_history.toPlainText().splitlines()), 50)
        lobby.chat_history.request_older()
        self.wait_until(lambda: "历史 001" in lobby.chat_history.toPlainText())

        reopened = self.new_player()
        self.login(reopened, "wrong")
        self.assertIsNone(reopened.lobby)
        self.player.close()
        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        for password in ("", "wrong"):
            self.login(reopened, password)
            self.assertIsNone(reopened.lobby)
            self.assertEqual(reopened.feedback.text(), "密码错误")
        for field in (reopened.actor_name, reopened.campaign_name):
            saved_value = field.text()
            field.setText("不存在")
            self.login(reopened, DEFAULT_PASSWORD)
            self.assertIsNone(reopened.lobby)
            self.assertEqual(reopened.feedback.text(), "角色信息错误")
            field.setText(saved_value)
        self.login(reopened, DEFAULT_PASSWORD)
        self.assertIsNone(reopened.session)
        lobby = reopened.lobby
        self.assertTrue(lobby.connection_status.text().endswith("-未连接"))
        self.assertTrue(lobby.message.isReadOnly())
        self.assertFalse(lobby.send_button.isEnabled())
        self.assertIn("历史 055", lobby.chat_history.toPlainText())
        lobby.chat_history.request_older()
        self.assertIn("历史 001", lobby.chat_history.toPlainText())

        populate(56, 110)
        self.server.start(self.directory, "127.0.0.1", self.port)
        self.wait_until(lambda: self.server.state == "running")
        with patch("client.player_window.QInputDialog.getText", return_value=(DEFAULT_PASSWORD, True)):
            lobby.connect_button.click()
        self.wait_until(lambda: reopened.session is not None and reopened.lobby.socket.state() == CONNECTED)
        lobby = reopened.lobby
        self.wait_until(lambda: "历史 110" in lobby.chat_history.toPlainText())
        lines = lobby.chat_history.toPlainText().splitlines()
        self.assertEqual(lines, [f"KP：历史 {number:03d}" for number in range(1, 111)])
        self.assertTrue(lobby.send_button.isEnabled())

    def test_password_rejected_by_host_cannot_unlock_offline_history(self) -> None:
        self.ready_player()
        self.service.change_password(self.player.session.token, DEFAULT_PASSWORD, PASSWORD)
        reopened = self.new_player()
        self.login(reopened, DEFAULT_PASSWORD)
        self.assertEqual(reopened.feedback.text(), "密码错误")
        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        self.login(reopened, DEFAULT_PASSWORD)
        self.assertIsNone(reopened.lobby)
        self.assertEqual(reopened.feedback.text(), "密码错误")

    def test_reconnect_keeps_selected_channel_and_archiving_preserves_other_drafts(self) -> None:
        self.ready_player()
        lobby = self.player.lobby
        lobby_id = lobby._channel_id
        lobby.message.setPlainText("大厅未发草稿")
        lobby.disconnect_button.click()
        self.wait_until(lambda: lobby.socket.state() == UNCONNECTED)
        states = []
        self.server.channelsReceived.connect(lambda request_id, state: states.append((request_id, state)))
        self.server.command("scene.create", name="掉线后分组", member_ids=[self.player.session.member_id],
                            channel_id="offline-scene", request_id="offline-assignment")
        self.wait_until(lambda: bool(states))
        lobby.connect_button.click()
        self.wait_until(lambda: lobby._channel_state_ready and lobby._assigned_channel_id == "offline-scene")
        self.assertEqual(lobby._channel_id, lobby_id)
        lobby.channel_choice.setCurrentIndex(lobby.channel_choice.findData("offline-scene"))
        lobby.message.setPlainText("场景中的发言")
        lobby.send_button.click()
        self.wait_until(lambda: not lobby._awaiting_send)
        lobby.disconnect_button.click()
        self.wait_until(lambda: lobby.socket.state() == UNCONNECTED)
        self.server.stop()
        self.wait_until(lambda: not self.server.active)
        archive_scene(self.directory, "offline-scene")
        self.server.start(self.directory, "127.0.0.1", self.port)
        self.wait_until(lambda: self.server.state == "running")
        lobby.connect_button.click()
        self.wait_until(lambda: lobby._channel_state_ready and lobby._assigned_channel_id == lobby_id)
        self.assertEqual(lobby._channel_id, "offline-scene")
        self.assertFalse(lobby.send_button.isEnabled())
        lobby.channel_choice.setCurrentIndex(lobby.channel_choice.findData(lobby_id))
        self.assertEqual(lobby.message.toPlainText(), "大厅未发草稿")
        lobby.channel_choice.setCurrentIndex(lobby.channel_choice.findData("offline-scene"))
        self.wait_until(lambda: "场景中的发言" in lobby.chat_history.toPlainText())
        self.assertFalse(lobby.send_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
