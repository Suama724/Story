"""参与者新版导航、独立状态探测、只读资料与滚动历史的验收。"""

import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QSettings, QTimer, Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget

from client.chat_composer import ChatComposer
from client.chat_history import ChatHistory
from client.lobby_window import LobbyWindow
from client.player_window import PasswordDialog, PlayerWindow
from client.ui_style import BACKGROUND, SURFACE
from host.channels import create_channel_group, create_scene
from host.management import add_member
from host.messages import save_lobby_message
from tests.attribute_fixtures import add_ready_player
from host.server_controller import ServerController
from host.storage import initialize_campaign


class ScrollHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def records(start, stop):
        return [{"sequence": n, "message_id": str(n), "nickname": "KP", "text": f"记录 {n:05d}"}
                for n in range(start, stop)]

    def test_conversation_ignores_outer_blank_lines_but_preserves_body_spacing(self):
        view = ChatHistory(conversation=True)
        view.resize(500, 300)
        view.show()
        records = self.records(1, 4)
        records[0]["text"] = "Normal line"
        records[1]["text"] = "\r\n \t\r\nNormal line\r\n \t\r\n"
        records[2]["text"] = "  First line\n\nSecond line  \u2028 \u2029"
        view.add_messages(records)
        self.app.processEvents()

        self.assertEqual(view.toPlainText(), "KP\nNormal line\nKP\nNormal line\nKP\n  First line\n\nSecond line  ")
        document = view.document()
        layout = document.documentLayout()
        first, second, third = [layout.blockBoundingRect(document.findBlockByNumber(i)) for i in range(3)]
        self.assertEqual(first.height(), second.height())
        self.assertEqual(second.top() - first.bottom(), third.top() - second.bottom())
        view.close()

    def test_prepend_preserves_reading_anchor_and_live_append_does_not_jump(self):
        view = ChatHistory(conversation=True)
        view.resize(600, 320)
        view.show()
        view.add_messages(self.records(5001, 5101))
        self.app.processEvents()
        bar = view.verticalScrollBar()
        bar.setValue(0)
        anchor = view.firstVisibleBlock().text()
        for end in range(5001, 1, -100):
            view.add_messages(self.records(max(1, end - 100), end))
        self.app.processEvents()
        self.assertEqual(view.firstVisibleBlock().text(), anchor)
        self.assertEqual(view.oldest_sequence, 1)
        self.assertEqual(view.document().blockCount(), 5100)
        bar.setValue(20)
        anchor, value = view.firstVisibleBlock().text(), bar.value()
        view.add_messages(self.records(5101, 5102))
        self.assertEqual(bar.value(), value)
        self.assertEqual(view.firstVisibleBlock().text(), anchor)
        bar.setValue(bar.maximum())
        view.add_messages(self.records(5102, 5103))
        self.assertEqual(bar.value(), bar.maximum())
        view.add_messages(self.records(1, 5103))
        self.assertEqual(view.document().blockCount(), 5102)
        view.close()

    def test_scroll_requests_one_page_at_a_time_and_stops_at_history_end(self):
        view = ChatHistory()
        view.resize(500, 300)
        view.show()
        view.add_messages(self.records(51, 101))
        self.app.processEvents()
        requests = []
        view.olderRequested.connect(lambda: requests.append(view.oldest_sequence))
        view.set_pagination(True)
        view.verticalScrollBar().setValue(0)
        view.request_older()
        self.assertEqual(requests, [51])
        view.add_messages(self.records(1, 51))
        view.set_pagination(False)
        view.verticalScrollBar().setValue(0)
        view.request_older()
        self.assertEqual(requests, [51])
        view.close()


class ChannelNavigationTests(unittest.TestCase):
    def test_participant_lobby_surfaces_and_scrolled_message_fade(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            player = PlayerWindow(QSettings(str(Path(folder) / "player.ini"), QSettings.Format.IniFormat))
            player.show()
            try:
                player._open_local_history({"address": "ws://127.0.0.1:1/ws/lobby", "actor_name": "调查员",
                                            "campaign_name": "调查团", "campaign_id": "test", "member_id": "actor"})
                app.processEvents()
                lobby = player.lobby
                panel = lobby.channel_splitter.widget(1)
                title_edge = player.title_bar.mapTo(player, QPoint(20, player.title_bar.height() - 1))

                def pixel(image, point):
                    ratio = image.devicePixelRatio()
                    return image.pixelColor(int(point.x() * ratio), int(point.y() * ratio)).name()

                image = player.grab().toImage()
                self.assertEqual(pixel(image, title_edge), BACKGROUND)
                edge = panel.mapTo(player, QPoint())
                for y in (1, panel.height() - 2):
                    self.assertEqual(pixel(image, edge + QPoint(-20, y)), BACKGROUND)
                    self.assertEqual(pixel(image, edge + QPoint(1, y)), BACKGROUND)
                    self.assertEqual(pixel(image, edge + QPoint(20, y)), SURFACE)
                header_edge = lobby.channel_header.mapTo(player, QPoint(20, lobby.channel_header.height() - 1))
                self.assertEqual(pixel(image, header_edge), SURFACE)

                history = lobby.chat_history
                self.assertFalse(history._top_fade.isVisible())
                history.add_messages(ScrollHistoryTests.records(1, 40))
                bar = history.verticalScrollBar()
                bar.setValue(bar.maximum() // 2)
                self.assertTrue(history._top_fade.isVisible())
                self.assertEqual(history._top_fade.pos(), history.viewport().pos())
                bar.setValue(bar.minimum())
                self.assertFalse(history._top_fade.isVisible())

                player._reset_login("")
                app.processEvents()
                self.assertNotEqual(pixel(player.grab().toImage(), title_edge), BACKGROUND)
            finally:
                player.close()

    def test_dragging_resizes_both_panes_and_channel_group_still_expands(self):
        app = QApplication.instance() or QApplication([])
        lobby = LobbyWindow(offline_actor="调查员")
        self.addCleanup(lobby.close)
        lobby.resize(820, 550)
        lobby.show()
        tree = lobby.channel_choice
        tree.set_channels(
            [{"group_id": "g", "name": "调查现场"}],
            [{"group_id": "g", "channel_id": "c", "name": "码头", "status": "active", "unread_count": 3}],
            None,
        )
        app.processEvents()
        group = tree.topLevelItem(0)
        channel = group.child(0)
        self.assertEqual(channel.text(0), "码头")
        self.assertEqual(channel.data(0, Qt.ItemDataRole.AccessibleDescriptionRole), "有未读消息")
        group_center = tree.visualItemRect(group).center()
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=group_center)
        self.assertFalse(group.isExpanded())
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=group_center)
        self.assertTrue(group.isExpanded())

        splitter = lobby.channel_splitter
        sidebar_width, chat_width = splitter.sizes()
        handle = splitter.handle(1)
        center = handle.rect().center()
        QTest.mousePress(handle, Qt.MouseButton.LeftButton, pos=center)
        QTest.mouseMove(handle, center - QPoint(72, 0))
        QTest.mouseRelease(handle, Qt.MouseButton.LeftButton, pos=handle.rect().center())
        app.processEvents()
        self.assertLess(splitter.sizes()[0], sidebar_width)
        self.assertGreater(splitter.sizes()[1], chat_width)
        self.assertEqual(lobby.chat_history.width(), splitter.sizes()[1])


class ComposerTests(unittest.TestCase):
    def test_shift_return_grows_upward_and_return_sends_the_whole_draft(self):
        app = QApplication.instance() or QApplication([])
        pane = QWidget()
        layout = QVBoxLayout(pane)
        history = ChatHistory(conversation=True)
        composer = ChatComposer()
        layout.addWidget(history, 1)
        layout.addWidget(composer)
        pane.resize(460, 320)
        pane.show()
        app.processEvents()
        first_height, bottom = composer.height(), composer.geometry().bottom()
        sent = []
        composer.sendRequested.connect(lambda: sent.append(composer.toPlainText()))
        composer.send_available = True
        QTest.keyClick(composer, Qt.Key.Key_Return)
        QTest.keyClicks(composer, "First line")
        QTest.keyClick(composer, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyClicks(composer, "Second line")
        app.processEvents()
        self.assertEqual(sent, [])
        self.assertEqual(composer.geometry().bottom(), bottom)
        self.assertEqual(composer.height(), first_height + composer.fontMetrics().lineSpacing())
        QTest.keyClick(composer, Qt.Key.Key_Enter)
        self.assertEqual(sent, ["First line\nSecond line"])
        composer.clear()
        app.processEvents()
        self.assertEqual(composer.height(), first_height)
        pane.close()


class ParticipantUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = initialize_campaign(self.root, "参与者界面团")
        self.member = add_ready_player(self.directory, "林舟")
        add_member(self.directory, "旁观者", role="ob")
        add_member(self.directory, "其他PL")
        group = create_channel_group(self.directory, "调查现场")
        self.first = create_scene(self.directory, "旧码头", [self.member], group_id=group, description="夜间调查")
        self.second = create_scene(self.directory, "钟楼", [self.member], group_id=group)
        for n in range(1, 126):
            save_lobby_message(self.directory, f"record-{n}", f"记录 {n:03d}", token=None, channel_id=self.first)
        self.server = ServerController()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.players = []
        self.server.start(self.directory, "127.0.0.1", self.port)
        self.wait(lambda: self.server.state == "running")
        self.player = self.login()
        self.lobby = self.player.lobby

    def tearDown(self):
        for player in self.players:
            player.close()
        self.server.stop()
        self.wait(lambda: not self.server.active)
        for player in self.players:
            player.deleteLater()
        self.server.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def wait(self, check, timeout=12):
        deadline = time.monotonic() + timeout
        while True:
            self.app.processEvents()
            if check():
                return
            if time.monotonic() >= deadline:
                self.fail("等待界面或网络状态超时")
            time.sleep(.01)

    def login(self):
        player = PlayerWindow(QSettings(str(self.root / "player.ini"), QSettings.Format.IniFormat))
        self.players.append(player)
        player.address.setText(f"ws://127.0.0.1:{self.port}/ws/lobby")
        player.campaign_name.setText("参与者界面团")
        player.actor_name.setText("林舟")
        player.password.setText("123456")
        player.login_button.click()
        self.wait(lambda: player.lobby is not None and not player._busy)
        if player.session is not None:
            self.wait(lambda: player.lobby.is_online and player.lobby._history_request is None)
        return player

    def select(self, channel):
        self.lobby.channel_choice.setCurrentIndex(self.lobby.channel_choice.findData(channel))
        self.wait(lambda: self.lobby._history_request is None)

    def click_sidebar(self, button, point=None):
        point = button.rect().center() if point is None else point
        QTest.mouseClick(self.player.windowHandle(), Qt.MouseButton.LeftButton, pos=button.mapTo(self.player, point))

    def test_layout_and_monitor_distinguish_manual_disconnect_from_host_shutdown(self):
        self.player.show()
        self.app.processEvents()
        self.assertFalse(hasattr(self.player, "header"))
        self.assertEqual(self.player.title_bar.caption.text(), "参与者界面团")
        sidebar = self.lobby._sidebar
        server_status = sidebar.server_status
        personal = self.lobby.personal_info_button
        connection = self.lobby.connection_button
        channels = self.lobby.channel_choice
        self.assertIsInstance(server_status, QToolButton)
        self.assertEqual(server_status.text(), "刷新服务器状态")
        self.assertEqual(channels.findChildren(QToolButton), [])
        self.assertEqual(channels.findChildren(QPushButton), [])
        group_row = channels.visualItemRect(channels.topLevelItem(0))
        server_center = server_status.mapTo(sidebar, server_status.rect().center())
        group_center = channels.viewport().mapTo(sidebar, group_row.center())
        self.assertLessEqual(abs(group_center.y() - server_center.y()), 1)
        self.assertLess(server_center.x(), channels.geometry().left())
        self.assertLess(personal.geometry().bottom(), connection.geometry().top())
        self.assertEqual(personal.toolTip(), "个人")
        self.assertGreater(personal.iconSize().width(), connection.iconSize().width())
        for button in (server_status, personal, connection):
            self.assertEqual(button.toolButtonStyle(), Qt.ToolButtonStyle.ToolButtonIconOnly)
            self.assertFalse(button.icon().isNull())
        header = self.lobby.channel_header
        self.assertLessEqual(header.height(), 50)
        self.assertEqual(header.description_button.accessibleName(), "频道简介")
        self.assertLess(header.description_button.geometry().right(), header.members_button.geometry().left())
        for button, tooltip in ((header.members_button, "频道成员"), (header.history_button, "聊天记录")):
            self.assertEqual(button.toolTip(), tooltip)
            self.assertEqual(button.toolButtonStyle(), Qt.ToolButtonStyle.ToolButtonIconOnly)
            self.assertFalse(button.icon().isNull())
        self.assertEqual(server_status.toolTip(), "服务器已开启")
        self.assertEqual(connection.toolTip(), "本机已连接")
        running_icon = server_status.icon().cacheKey()
        connected_icon = connection.icon().cacheKey()
        point = server_status.rect().center()
        event = QHelpEvent(QEvent.Type.ToolTip, point, server_status.mapToGlobal(point))
        QApplication.sendEvent(server_status, event)
        tooltip = self.lobby.findChild(QLabel, "sidebarTooltip")
        self.assertTrue(tooltip.isVisible())
        self.assertEqual(tooltip.text(), "服务器已开启")
        self.assertGreater(tooltip.x(), server_status.mapTo(self.lobby, server_status.rect().topRight()).x())
        monitor = self.lobby._host_monitor
        self.assertTrue(monitor.timer.isActive())
        self.wait(lambda: monitor.reply is None)
        self.click_sidebar(server_status)
        self.assertTrue(tooltip.isVisible())
        self.assertEqual(tooltip.text(), "正在检查服务器…")
        request = monitor.reply
        self.assertIsNotNone(request)
        server_status.click()
        self.assertIs(monitor.reply, request)
        self.wait(lambda: tooltip.text() == "服务器已开启" and monitor.reply is None)
        self.assertTrue(self.lobby.is_online)
        self.assertFalse(self.player.personal_info_dialog.isVisible())
        self.click_sidebar(connection, QPoint(3, connection.height() // 2))
        self.wait(lambda: connection.toolTip() == "本机未连接")
        self.assertEqual(server_status.toolTip(), "服务器已开启")
        self.assertEqual(server_status.icon().cacheKey(), running_icon)
        disconnected_icon = connection.icon().cacheKey()
        self.assertNotEqual(disconnected_icon, connected_icon)
        self.assertFalse(self.lobby._reconnect_timer.isActive())
        self.server.stop()
        self.wait(lambda: not self.server.active)
        self.wait(lambda: server_status.toolTip() == "服务器未开启")
        self.assertNotEqual(server_status.icon().cacheKey(), running_icon)
        self.assertEqual(connection.icon().cacheKey(), disconnected_icon)
        monitor.timer.stop()
        self.server.start(self.directory, "127.0.0.1", self.port)
        self.wait(lambda: self.server.state == "running")
        self.assertEqual(server_status.toolTip(), "服务器未开启")
        server_status.setFocus()
        QTest.keyClick(server_status, Qt.Key.Key_Space)
        self.wait(lambda: server_status.toolTip() == "服务器已开启")
        self.assertEqual(connection.toolTip(), "本机未连接")
        self.click_sidebar(connection)
        self.wait(lambda: self.lobby.is_online)
        self.assertEqual(connection.toolTip(), "本机已连接")
        self.assertEqual(connection.icon().cacheKey(), connected_icon)

    def test_main_scroll_and_log_have_independent_paging_and_cached_member_data(self):
        self.select(self.first)
        self.assertEqual(self.lobby.channel_header.title.text(), "旧码头")
        self.assertEqual(self.lobby.channel_info_button.text(), "夜间调查")
        self.lobby.chat_history.request_older()
        self.wait(lambda: self.lobby._history_request is None)
        self.assertIn("记录 026", self.lobby.chat_history.toPlainText())
        self.lobby.chat_history.request_older()
        self.wait(lambda: self.lobby._history_request is None)
        self.assertIn("记录 001", self.lobby.chat_history.toPlainText())
        self.assertFalse(self.lobby.chat_history.has_more)
        self.lobby.channel_info_button.click()
        self.assertEqual(self.lobby._info_dialogs[self.first].content.toPlainText(), "夜间调查")
        self.lobby.channel_members_button.click()
        members = self.lobby._members_dialogs[self.first]
        self.wait(lambda: members.table.rowCount() == 3)
        names = {members.table.item(row, 1).text() for row in range(members.table.rowCount())}
        self.assertEqual(names, {"KP", "林舟", "旁观者"})
        self.assertTrue(all(set(m) == {"role", "actor_name"} for m in self.lobby._channels[self.first]["members"]))
        self.lobby.channel_log_button.click()
        log = self.lobby._log_dialogs[self.first]
        self.wait(lambda: log.loaded)
        self.select(self.second)
        while log.has_more:
            log.history.request_older()
            self.wait(lambda: not log.loading)
        self.assertEqual(log.channel_id, self.first)
        self.assertIn("记录 001", log.history.toPlainText())
        self.assertIn("记录 125", log.history.toPlainText())
        self.assertEqual(self.lobby._channel_id, self.second)
        self.lobby.connection_button.click()
        self.wait(lambda: not self.lobby.is_online)
        self.select(self.first)
        self.lobby._open_channel_log()
        self.assertIn("本地聊天记录", log.windowTitle())
        while log.has_more:
            log.history.request_older()
        self.assertIn("记录 001", log.history.toPlainText())

    def test_offline_login_reuses_group_navigation_and_local_log(self):
        self.select(self.first)
        while self.lobby.chat_history.has_more:
            self.lobby.chat_history.request_older()
            self.wait(lambda: self.lobby._history_request is None)
        self.player.close()
        self.server.stop()
        self.wait(lambda: not self.server.active)
        reopened = self.login()
        self.assertIsNone(reopened.session)
        self.lobby = reopened.lobby
        self.wait(lambda: self.lobby._sidebar.server_status.toolTip() == "服务器未开启")
        self.assertEqual(self.lobby.connection_button.toolTip(), "本机未连接")
        self.select(self.first)
        self.lobby._open_channel_log()
        log = self.lobby._log_dialogs[self.first]
        while log.has_more:
            log.history.request_older()
        self.assertIn("记录 001", log.history.toPlainText())
        self.assertEqual(self.lobby.channel_choice.topLevelItemCount(), 2)
        self.assertFalse(self.lobby.message.send_available)

    def test_open_log_is_reloaded_when_history_permissions_narrow(self):
        lobby_id = next(key for key, channel in self.lobby._channels.items() if channel["kind"] == "lobby")
        self.server.command("members.move", member_ids=[self.member], channel_id=lobby_id)
        self.wait(lambda: self.lobby._assigned_channel_id == lobby_id)
        self.server.command("visibility.set", mode="broad")
        self.wait(lambda: self.lobby._channels[self.first]["history_ranges"] == [[1, None]])
        self.select(self.first)
        self.server.command("say", channel_id=self.first, text="参与区间外的新记录")
        self.wait(lambda: "参与区间外的新记录" in self.lobby.chat_history.toPlainText())
        self.lobby._open_channel_log()
        log = self.lobby._log_dialogs[self.first]
        self.wait(lambda: log.loaded)
        self.assertIn("参与区间外的新记录", log.history.toPlainText())
        self.server.command("visibility.set", mode="secret")
        self.wait(lambda: self.lobby._channels[self.first]["history_ranges"] == [[1, 125]]
                  and log.loaded and not log.loading)
        self.assertNotIn("参与区间外的新记录", log.history.toPlainText())
        self.assertLessEqual(log.history.newest_sequence, 125)

    def test_personal_info_password_change_updates_display_and_offline_login(self):
        self.player.show()
        self.app.processEvents()
        personal = self.lobby.personal_info_button
        point = QPoint(2, personal.height() // 2)
        event = QHelpEvent(QEvent.Type.ToolTip, point, personal.mapToGlobal(point))
        QApplication.sendEvent(personal, event)
        self.click_sidebar(personal, point)
        dialog = self.player.personal_info_dialog
        self.assertTrue(dialog.isVisible())
        self.assertEqual((dialog.identity.text(), dialog.name.text(), dialog.password.text()), ("PL", "林舟", "123456"))
        self.assertTrue(all(field.isReadOnly() for field in (dialog.identity, dialog.name, dialog.password)))
        self.assertTrue(dialog.change_button.isEnabled())
        self.assertFalse(hasattr(self.player, "_identity_menu"))
        self.assertFalse(hasattr(self.player, "logout_button"))
        self.assertFalse(hasattr(self.player, "_logout"))
        sidebar = self.lobby.connection_button.parentWidget().layout()
        self.assertEqual(sidebar.indexOf(self.lobby.personal_info_button) + 1, sidebar.indexOf(self.lobby.connection_button))
        new_password = "修改后的密码123"

        def fill():
            change = self.player.findChild(PasswordDialog)
            change.new_password.setText(new_password)
            change.confirm_password.setText(new_password)
            change.accept()

        QTimer.singleShot(0, fill)
        dialog.change_button.click()
        self.wait(lambda: not self.player._busy)
        self.assertEqual(dialog.password.text(), new_password)
        self.assertEqual(self.player.history.entries()[0]["password"], new_password)
        self.assertEqual(dialog.feedback.text(), "密码已更新。")
        self.assertTrue(self.lobby.is_online)
        self.player.close()
        self.server.stop()
        self.wait(lambda: not self.server.active)
        reopened = PlayerWindow(QSettings(str(self.root / "player.ini"), QSettings.Format.IniFormat))
        self.players.append(reopened)
        reopened.login_button.click()
        self.wait(lambda: reopened.lobby is not None and not reopened._busy)
        reopened.lobby.personal_info_button.click()
        offline_info = reopened.personal_info_dialog
        self.assertEqual(offline_info.password.text(), new_password)
        self.assertEqual(offline_info.name.text(), "林舟")
        self.assertFalse(offline_info.change_button.isEnabled())
        self.assertFalse(self.player.personal_info_dialog.isVisible())
        self.assertEqual(self.player.personal_info_dialog.password.text(), "")

    def test_rejected_password_change_keeps_verified_password(self):
        self.lobby.personal_info_button.click()
        dialog = self.player.personal_info_dialog

        def fill():
            change = self.player.findChild(PasswordDialog)
            change.current_password.setText("错误的原密码")
            change.new_password.setText("不能保存的新密码")
            change.confirm_password.setText("不能保存的新密码")
            change.accept()

        QTimer.singleShot(0, fill)
        dialog.change_button.click()
        self.wait(lambda: not self.player._busy)
        self.assertEqual(dialog.password.text(), "123456")
        self.assertEqual(self.player.history.entries()[0]["password"], "123456")
        self.assertTrue(dialog.feedback.isVisible())
        self.assertNotEqual(dialog.feedback.text(), "密码已更新。")
        self.assertTrue(dialog.change_button.isEnabled())
