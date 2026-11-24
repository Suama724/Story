"""主机窗口的选团、离线管理与真实监听进程验收。"""

import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from websockets.asyncio.client import connect

from chat_protocol import HISTORY_PAGE_SIZE
from host.auth import AuthService, DEFAULT_PASSWORD
from host.host_window import HostWindow, SceneDialog
from host.campaign_dialogs import CampaignManagementDialog, DeleteCampaignDialog
from host.channels import create_scene, local_channel_state
from client.player_window import PlayerWindow
from host.management import add_member, campaign_details, discover_campaigns, edit_member
from host.messages import save_lobby_message
from tests.attribute_fixtures import add_ready_player
from host.storage import open_campaign


class HostGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.window = HostWindow(self.root)
        self.players = []
        self.wait_until(lambda: not self.window._busy)

    def tearDown(self) -> None:
        for player in self.players:
            player.close()
            player.deleteLater()
        self.window.server.stop()
        self.wait_until(lambda: not self.window.server.active, timeout=10)
        self.wait_until(lambda: not self.window._busy)
        self.window.close()
        self.wait_until(lambda: not self.window._busy)
        self.window.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def wait_until(self, condition, timeout=15) -> None:
        deadline = time.monotonic() + timeout
        while True:
            self.app.processEvents()
            if condition():
                return
            if time.monotonic() > deadline:
                self.fail(f"等待超时，{self.window.feedback.text()}\n{self.window.logs.toPlainText()}")
            time.sleep(0.01)

    def create_from_form(self, name="主机调试团 🎲") -> dict:
        self.window.new_name.setText(name)
        self.window.new_description.setPlainText("团本简介\n第二行")
        self.window.open_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.pages.currentIndex(), 1, self.window.feedback.text())
        return self.window.campaign

    def free_port(self) -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def start_listener(self) -> int:
        port = self.free_port()
        self.window.listen_port.setValue(port)
        self.window.network_button.click()
        self.wait_until(lambda: self.window.server.state == "running")
        return port

    def test_create_select_and_manage_without_listener(self) -> None:
        self.assertEqual(self.window.pages.currentIndex(), 0)
        self.assertFalse(self.window.server.active)
        campaign = self.create_from_form()
        self.assertEqual(campaign["kp_name"], "KP")
        self.assertEqual(campaign["description"], "团本简介\n第二行")
        self.assertEqual(len(campaign["members"]), 1)
        self.assertTrue(self.window.add_button.isEnabled())
        self.assertFalse(self.window.send_button.isEnabled())
        directory = campaign["directory"]
        player = add_member(directory, "调查员乙")
        edit_member(directory, player, "角色改名")
        self.window.members_refresh.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.member_table.rowCount(), 2)
        self.assertEqual(self.window.member_table.item(1, 2).text(), DEFAULT_PASSWORD)
        self.window._switch_campaign()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.campaign_list.count(), 1)
        self.window.open_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.campaign["campaign_id"], campaign["campaign_id"])
        self.assertFalse(self.window.server.active)

    def test_blank_campaign_name_leaves_no_partial_campaign(self) -> None:
        self.window.new_name.setText("  ")
        self.window.open_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.pages.currentIndex(), 0)
        self.assertEqual(discover_campaigns(self.root)[0], [])

    def test_reopen_campaign_and_page_older_history_without_listening(self) -> None:
        campaign = self.create_from_form()
        self.assertFalse(self.window.messages.has_more)
        total = HISTORY_PAGE_SIZE * 2 + 1
        for sequence in range(1, total + 1):
            save_lobby_message(campaign["directory"], f"seed-{sequence}", f"历史 {sequence:03d}", token=None)
        self.window._switch_campaign()
        self.wait_until(lambda: not self.window._busy)
        self.window.open_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertFalse(self.window.server.active)
        for count in (HISTORY_PAGE_SIZE, HISTORY_PAGE_SIZE * 2):
            self.assertEqual(self.window.messages.toPlainText().splitlines(), [
                f"KP：历史 {sequence:03d}" for sequence in range(total - count + 1, total + 1)
            ])
            self.assertTrue(self.window.messages.has_more)
            self.window.messages.request_older()
            self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.messages.toPlainText().splitlines(), [
            f"KP：历史 {sequence:03d}" for sequence in range(1, total + 1)
        ])
        self.assertFalse(self.window.messages.has_more)

        self.window._switch_campaign()
        self.wait_until(lambda: not self.window._busy)
        other = self.create_from_form("另一场调查")
        self.assertNotEqual(other["campaign_id"], campaign["campaign_id"])
        self.assertEqual(self.window.messages.toPlainText(), "")

    def test_selector_same_name_reuses_campaign_and_rules_only_emit_signal(self) -> None:
        self.window.show()
        self.app.processEvents()
        selector_geometry = self.window.geometry()
        self.assertTrue(self.window.masthead.isHidden())
        campaign = self.create_from_form()
        self.assertGreater(self.window.height(), selector_geometry.height())
        add_member(campaign["directory"], "角色")
        add_member(campaign["directory"], "观众", role="ob")
        self.window._switch_campaign()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.geometry(), selector_geometry)
        self.assertIn("PL: 1 人   OB: 1 人", self.window.campaign_list.item(0).text())
        self.assertEqual(self.window.new_name.text(), campaign["name"])
        self.assertEqual(self.window.open_button.text(), "进入")
        self.window.new_button.click()
        self.assertEqual(self.window.new_name.text(), "")
        self.assertEqual(self.window.new_description.toPlainText(), "")
        self.assertEqual(self.window.rules_choice.currentData(), "coc7")
        self.assertIsNone(self.window.campaign_list.currentItem())
        self.window.new_name.setText("新名字")
        self.assertEqual(self.window.open_button.text(), "新建并进入")
        self.window.new_name.setText(campaign["name"])
        self.assertEqual(self.window.open_button.text(), "进入")
        events = []
        self.window.rulesetSelected.connect(events.append)
        self.window.rules_choice.addItem("未来规则", "future")
        self.window.rules_choice.setCurrentIndex(1)
        self.assertEqual(events, ["future"])
        self.window.new_description.setPlainText("不能覆盖已有团简介")
        self.window.open_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.campaign["campaign_id"], campaign["campaign_id"])
        self.assertEqual(self.window.campaign["description"], campaign["description"])
        self.assertEqual(self.window.campaign["ruleset"], "coc7")
        self.assertEqual(len(discover_campaigns(self.root)[0]), 1)

    def test_campaign_management_can_hide_then_explicitly_delete_archive(self) -> None:
        campaign = self.create_from_form()
        self.window._switch_campaign()
        self.wait_until(lambda: not self.window._busy)

        def manage(delete_archive):
            dialog = self.window.findChild(CampaignManagementDialog)
            if dialog is None:
                QTimer.singleShot(10, lambda: manage(delete_archive))
                return
            self.assertEqual(dialog.archive_field.text(), str(campaign["directory"]))
            self.assertFalse(dialog.import_button.isEnabled())

            def confirm():
                confirmation = dialog.findChild(DeleteCampaignDialog)
                self.assertFalse(confirmation.delete_archive.isChecked())
                confirmation.delete_archive.setChecked(delete_archive)
                confirmation.accept()

            QTimer.singleShot(0, confirm)
            dialog.delete_button.click()
            dialog.accept()

        QTimer.singleShot(0, lambda: manage(False))
        self.window.manage_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.campaign_list.count(), 0)
        self.assertTrue(campaign["directory"].exists())
        self.app.sendPostedEvents(None, 0)
        self.app.processEvents()
        QTimer.singleShot(0, lambda: manage(True))
        self.window.manage_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertFalse(campaign["directory"].exists())
        self.assertEqual(discover_campaigns(self.root)[0], [])

    def test_cancelling_campaign_deletion_preserves_archive_and_visibility(self) -> None:
        campaign = self.create_from_form()
        entries, _ = self.window.library.entries()
        dialog = CampaignManagementDialog(self.window, self.window.library, entries)
        with patch.object(DeleteCampaignDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            dialog.delete_button.click()
        self.assertFalse(dialog.changed)
        self.assertTrue(campaign["directory"].exists())
        self.assertFalse(self.window.library.entries()[0][0]["hidden"])
        self.assertEqual(campaign_details(campaign["directory"])["campaign_id"], campaign["campaign_id"])
        dialog.deleteLater()

    def test_start_stop_restart_and_live_member_edit(self) -> None:
        campaign = self.create_from_form()
        player = add_ready_player(campaign["directory"], "调查员甲")
        self.window.members_refresh.click()
        self.wait_until(lambda: not self.window._busy)
        ready = AuthService(campaign["directory"]).login(campaign["name"], "调查员甲", DEFAULT_PASSWORD)
        port = self.start_listener()
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assertTrue(self.window.add_button.isEnabled())
        second_payload = json.dumps({"type": "chat.send", "request_id": "after-rename", "text": "改名后发言"})

        async def chat() -> dict:
            async with connect(f"ws://127.0.0.1:{port}/ws/lobby", additional_headers={
                "Authorization": f"Bearer {ready['token']}"
            }, proxy=None) as websocket:
                await websocket.send(json.dumps({"type": "chat.send", "text": "来自玩家 🎲"}))
                received = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                self.assertEqual(received["nickname"], "调查员甲")
                edit_member(campaign["directory"], player, "角色改名")
                await websocket.send(second_payload)
                second_ack = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                self.assertEqual(second_ack["type"], "chat.ack")
                received = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                self.assertEqual(received, second_ack["message"])
                self.assertEqual(received["nickname"], "角色改名")
                self.window.message_input.setPlainText("主持人调试消息")
                self.window.send_button.click()
                self.app.processEvents()
                received = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                self.assertEqual(received["nickname"], "KP")
                self.assertEqual(received["text"], "主持人调试消息")
                request = Request(
                    f"http://127.0.0.1:{port}/auth/password",
                    data=json.dumps({"current_password": DEFAULT_PASSWORD, "new_password": "654321"}).encode(),
                    headers={"Authorization": f"Bearer {ready['token']}", "Content-Type": "application/json"},
                )
                with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                    self.assertEqual(json.load(response), {"changed": True})
                return second_ack

        original_ack = asyncio.run(chat())
        self.wait_until(lambda: "调查员甲离开了" in self.window.logs.toPlainText())
        logs = self.window.logs.toPlainText().splitlines()
        # Windows 关闭网络连接时可能同时输出诊断日志，单独核对成员事件的次数与顺序。
        member_events = [line for line in logs if line in ("调查员甲加入了", "调查员甲离开了")]
        self.assertEqual(member_events, ["调查员甲加入了", "调查员甲离开了"])
        self.wait_until(lambda: "主持人调试消息" in self.window.messages.toPlainText())
        self.wait_until(lambda: not self.window.message_input.toPlainText())
        self.assertEqual(self.window.messages.toPlainText().count("主持人调试消息"), 1)
        self.wait_until(lambda: self.window.member_table.item(1, 2).text() == "654321")
        for _ in range(2):
            self.window.network_button.click()
            self.wait_until(lambda: self.window.server.state == "stopped" and not self.window._busy)
            self.assertTrue(self.window.add_button.isEnabled())
            self.assertEqual(campaign_details(campaign["directory"])["campaign_id"], campaign["campaign_id"])
            self.window.network_button.click()
            self.wait_until(lambda: self.window.server.state == "running")

        async def retry_after_restart() -> None:
            async with connect(f"ws://127.0.0.1:{port}/ws/lobby", additional_headers={
                "Authorization": f"Bearer {ready['token']}"
            }, proxy=None) as websocket:
                await websocket.send(second_payload)
                self.assertEqual(json.loads(await asyncio.wait_for(websocket.recv(), timeout=5)), original_ack)

        asyncio.run(retry_after_restart())
        with open_campaign(campaign["directory"]) as connection:
            rows = connection.execute("SELECT sequence, nickname, text FROM messages ORDER BY sequence").fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                (1, "调查员甲", "来自玩家 🎲"), (2, "角色改名", "改名后发言"), (3, "KP", "主持人调试消息"),
            ])
        self.window.server.stop()
        self.wait_until(lambda: not self.window.server.active)
        self.window._switch_campaign()
        self.wait_until(lambda: not self.window._busy)
        self.window.open_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.messages.toPlainText().splitlines(), [
            "调查员甲：来自玩家 🎲", "角色改名：改名后发言", "KP：主持人调试消息",
        ])

    def test_occupied_port_preserves_existing_listener(self) -> None:
        self.create_from_form()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.window.listen_port.setValue(port)
            self.window.network_button.click()
            self.wait_until(lambda: self.window.server.state == "stopped")
            self.assertIn("无法监听", self.window.feedback.text())
            self.assertEqual(listener.getsockname()[1], port)
            self.assertTrue(self.window.listen_port.isEnabled())

    def test_parent_pipe_close_stops_owned_server(self) -> None:
        self.create_from_form()
        self.start_listener()
        self.window.server.process.closeWriteChannel()
        self.wait_until(lambda: self.window.server.state == "stopped")

    def test_scene_creation_and_archive_work_offline_and_survive_reopen(self) -> None:
        campaign = self.create_from_form()
        add_member(campaign["directory"], "离线角色")
        self.window.members_refresh.click()
        self.wait_until(lambda: not self.window._busy)

        def fill_scene():
            dialog = self.window.findChild(SceneDialog)
            dialog.name.setText("离线场景")
            dialog.members.item(0).setCheckState(Qt.CheckState.Checked)
            dialog.accept()

        QTimer.singleShot(0, fill_scene)
        self.window._create_scene()
        self.wait_until(lambda: len(self.window._channels) == 2 and not self.window._busy)
        scene = self.window._channel_id
        self.assertEqual(self.window._channels[scene]["name"], "离线场景")
        self.assertFalse(self.window.server.active)
        self.assertTrue(self.window.archive_scene_button.isEnabled())
        with patch("host.host_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.window.archive_scene_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window._channels[scene]["status"], "archived")
        self.window._switch_campaign()
        self.wait_until(lambda: not self.window._busy)
        self.window.open_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window._channels[scene]["status"], "archived")

    def test_scene_form_private_chat_broad_observation_and_archive(self) -> None:
        campaign = self.create_from_form()
        for name in ("A", "B"):
            add_ready_player(campaign["directory"], name)
        self.window.members_refresh.click()
        self.wait_until(lambda: not self.window._busy)
        port = self.start_listener()
        for name in ("A", "B"):
            player = PlayerWindow(QSettings(str(self.root / f"{name}.ini"), QSettings.Format.IniFormat))
            self.players.append(player)
            player.address.setText(f"ws://127.0.0.1:{port}/ws/lobby")
            player.campaign_name.setText(campaign["name"])
            player.actor_name.setText(name)
            player.password.setText(DEFAULT_PASSWORD)
            player.login_button.click()
            self.wait_until(lambda: player.lobby is not None and player.lobby._channel_state_ready)
        a, b = (player.lobby for player in self.players)
        self.window.message_input.setPlainText("大厅欢迎")
        self.window.send_button.click()
        self.wait_until(lambda: "大厅欢迎" in a.chat_history.toPlainText() and "大厅欢迎" in b.chat_history.toPlainText()
                        and self.window.create_group_button.isEnabled())
        a.message.setPlainText("大厅草稿")
        b.message.setPlainText("B 的草稿")

        def fill_scene():
            dialog = self.window.findChild(SceneDialog)
            dialog.name.setText("地下室")
            for index in range(dialog.members.count()):
                if dialog.members.item(index).text() == "A":
                    dialog.members.item(index).setCheckState(Qt.CheckState.Checked)
            dialog.accept()

        QTimer.singleShot(0, fill_scene)
        self.window._create_scene()
        self.wait_until(lambda: len(self.window._channels) == 2 and not self.window._busy)
        scene = next(key for key, value in self.window._channels.items() if value["kind"] == "scene")
        lobby = campaign["lobby_id"]
        self.wait_until(lambda: a._assigned_channel_id == scene)
        self.assertEqual(b.channel_choice.count(), 1)
        a.channel_choice.setCurrentIndex(a.channel_choice.findData(scene))
        self.assertNotIn("大厅欢迎", a.chat_history.toPlainText())
        self.window.message_input.setPlainText("地下室线索 🎲")
        self.window.send_button.click()
        self.wait_until(lambda: "地下室线索 🎲" in a.chat_history.toPlainText())
        self.assertNotIn("地下室线索", b.chat_history.toPlainText())
        a.message.setPlainText("地下室草稿")
        a.channel_choice.setCurrentIndex(a.channel_choice.findData(lobby))
        self.assertEqual(a.message.toPlainText(), "大厅草稿")
        self.assertNotIn("地下室线索", a.chat_history.toPlainText())
        self.assertTrue(a.send_button.isEnabled())
        a.channel_choice.setCurrentIndex(a.channel_choice.findData(scene))
        self.assertEqual(a.message.toPlainText(), "地下室草稿")

        replies = []
        b.socket.textMessageReceived.connect(lambda payload: replies.append(json.loads(payload)))
        b.socket.sendTextMessage(json.dumps({"type": "chat.history.get", "channel_id": scene, "mode": "latest", "cursor": 0, "request_id": "forged-history"}))
        self.wait_until(lambda: any(item.get("request_id") == "forged-history" for item in replies))
        self.assertEqual(next(item for item in replies if item.get("request_id") == "forged-history")["error"], "channel_read_forbidden")

        self.window._channel_operation("visibility.set", mode="broad")
        self.wait_until(lambda: scene in b._channels and self.window._channel_request is None)
        self.assertEqual(b.message.toPlainText(), "B 的草稿")
        b.channel_choice.setCurrentIndex(b.channel_choice.findData(scene))
        self.wait_until(lambda: "地下室线索 🎲" in b.chat_history.toPlainText())
        self.assertFalse(b.send_button.isEnabled())
        self.window.message_input.setPlainText("旁观可见的新线索")
        self.window.send_button.click()
        self.wait_until(lambda: "旁观可见的新线索" in b.chat_history.toPlainText() and self.window.create_group_button.isEnabled())
        b.socket.sendTextMessage(json.dumps({"type": "chat.send", "channel_id": scene, "text": "旁观者越权", "request_id": "forged-send"}))
        self.wait_until(lambda: any(item.get("request_id") == "forged-send" for item in replies))
        self.assertEqual(next(item for item in replies if item.get("request_id") == "forged-send")["code"], "channel_forbidden")
        self.window._channel_operation("visibility.set", mode="secret")
        self.wait_until(lambda: scene not in b._channels and self.window.archive_scene_button.isEnabled())
        self.assertEqual(b._channel_id, lobby)
        self.assertEqual(b.message.toPlainText(), "B 的草稿")
        self.assertEqual(b._base_cache.for_channel(scene).page(), [])

        with patch("host.host_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.window.archive_scene_button.click()
        self.wait_until(lambda: a._assigned_channel_id == lobby)
        self.assertEqual(a._channel_id, scene)
        a.channel_choice.setCurrentIndex(a.channel_choice.findData(lobby))
        self.assertEqual(a.message.toPlainText(), "大厅草稿")
        a.channel_choice.setCurrentIndex(a.channel_choice.findData(scene))
        self.wait_until(lambda: "地下室线索 🎲" in a.chat_history.toPlainText())
        self.assertFalse(a.send_button.isEnabled())
        self.assertEqual(a.message.toPlainText(), "地下室草稿")

    def test_opening_channel_then_immediately_switching_campaign_flushes_read_position(self) -> None:
        campaign = self.create_from_form()
        member = add_ready_player(campaign["directory"], "A")
        scene = create_scene(campaign["directory"], "地下室", [member])
        token = AuthService(campaign["directory"]).login(campaign["name"], "A", DEFAULT_PASSWORD)["token"]
        save_lobby_message(campaign["directory"], "read-before-switch", "读后立即切团", token=token, channel_id=scene)
        self.window._switch_campaign()
        self.wait_until(lambda: not self.window._busy)
        self.window.open_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.window.channel_choice.setCurrentIndex(self.window.channel_choice.findData(scene))
        self.wait_until(lambda: not self.window._busy)
        self.window._switch_campaign()
        self.wait_until(lambda: not self.window._busy and self.window.campaign is None)
        state = next(item for item in local_channel_state(campaign["directory"])["channels"] if item["channel_id"] == scene)
        self.assertEqual(state["unread_count"], 0)


if __name__ == "__main__":
    unittest.main()
