"""主机窗口的选团、离线管理与真实监听进程验收。"""

import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from websockets.asyncio.client import connect

from host.auth import AuthService, DEFAULT_PASSWORD
from host.host_window import HostWindow
from host.management import add_member, campaign_details, discover_campaigns, edit_member
from host.storage import open_campaign


class HostGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.window = HostWindow(self.root)
        self.wait_until(lambda: not self.window._busy)

    def tearDown(self) -> None:
        self.window.server.stop()
        self.wait_until(lambda: not self.window.server.active, timeout=10)
        self.wait_until(lambda: not self.window._busy)
        self.window.close()
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

    def create_from_form(self) -> dict:
        self.window.new_name.setText("主机调试团 🎲")
        self.window.new_description.setPlainText("团本简介\n第二行")
        self.window.create_button.click()
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
        self.window.switch_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.campaign_list.count(), 1)
        self.window.open_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.campaign["campaign_id"], campaign["campaign_id"])
        self.assertFalse(self.window.server.active)

    def test_blank_campaign_name_leaves_no_partial_campaign(self) -> None:
        self.window.new_name.setText("  ")
        self.window.create_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertEqual(self.window.pages.currentIndex(), 0)
        self.assertEqual(discover_campaigns(self.root)[0], [])

    def test_start_stop_restart_and_live_member_edit(self) -> None:
        campaign = self.create_from_form()
        player = add_member(campaign["directory"], "调查员甲")
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
        self.assertEqual(logs[-2:], ["调查员甲加入了", "调查员甲离开了"])
        self.wait_until(lambda: "主持人调试消息" in self.window.messages.toPlainText())
        self.wait_until(lambda: not self.window.message_input.toPlainText())
        self.assertEqual(self.window.messages.toPlainText().count("主持人调试消息"), 1)
        self.wait_until(lambda: self.window.member_table.item(1, 2).text() == "654321")
        for _ in range(2):
            self.window.network_button.click()
            self.wait_until(lambda: self.window.server.state == "stopped")
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


if __name__ == "__main__":
    unittest.main()
