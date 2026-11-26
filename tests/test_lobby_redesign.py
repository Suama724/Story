"""组织方新版窗口、真实在线状态、多频道关系与旧库升级的验收。"""

import os
import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QSettings, QTimer, Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QLabel, QPushButton

from client.player_window import PlayerWindow
from client.role_window import RoleWindow
from client.ui_style import SLIM_SCROLLBAR_STYLE
from host import migrations
from host.auth import AuthService
from host.channels import (
    archive_scene, create_channel_group, create_scene, get_channel_access,
    local_channel_state, player_channel_state, update_channel,
)
from client.attribute_dialogs import AttributeSheetDialog
from host.host_window import HostWindow, MemberDialog, SceneDialog
from host.dice import local_dice_config
from host.dice_dialog import DiceManagementDialog
from host.management import add_member
from host.messages import ChatSendError, save_lobby_message
from host.storage import campaign_info, initialize_campaign, open_campaign
from tests.legacy_channel import seed_legacy_scene
from tests.attribute_fixtures import ATTRIBUTE_COMMAND, add_ready_player
from host.attributes import player_attributes


class MultiChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = initialize_campaign(Path(self.temp.name), "新版团")
        self.pl = add_ready_player(self.directory, "调查员")
        self.ob = add_member(self.directory, "看客", role="ob")
        self.auth = AuthService(self.directory)
        self.pl_token = self.auth.login("新版团", "调查员", "123456")["token"]
        self.ob_token = self.auth.login("新版团", "看客", "123456", "ob")["token"]

    def test_parallel_memberships_survive_independent_archive_and_reopen(self):
        first = create_scene(self.directory, "码头", [self.pl, self.ob])
        second = create_scene(self.directory, "钟楼", [self.pl, self.ob])
        for channel in (first, second):
            save_lobby_message(self.directory, f"pl-{channel}", "可发言", token=self.pl_token, channel_id=channel)
            with self.assertRaises(ChatSendError):
                save_lobby_message(self.directory, f"ob-{channel}", "不能越权", token=self.ob_token, channel_id=channel)
        archive_scene(self.directory, first)
        save_lobby_message(self.directory, "remaining", "仍在钟楼", token=self.pl_token, channel_id=second)
        with open_campaign(self.directory) as connection:
            self.assertFalse(get_channel_access(connection, self.pl, first).can_send)
            self.assertTrue(get_channel_access(connection, self.pl, second).can_send)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        state = local_channel_state(self.directory)
        self.assertIn(second, next(m for m in state["members"] if m["member_id"] == self.pl)["channel_ids"])

    def test_groups_metadata_and_visibility_do_not_leak_empty_private_groups(self):
        group = create_channel_group(self.directory, "调查现场", group_id="stable-group")
        self.assertEqual(create_channel_group(self.directory, "调查现场", group_id=group), group)
        empty = create_channel_group(self.directory, "备用")
        scene = create_scene(self.directory, "码头", [self.pl], group_id=group, description="夜间调查")
        message, _ = save_lobby_message(self.directory, "before-rename", "保留消息", token=self.pl_token, channel_id=scene)
        state = local_channel_state(self.directory)
        revision = state["revision"]
        update_channel(self.directory, scene, "旧码头", empty, "更新简介", expected_revision=revision)
        with self.assertRaises(ValueError):
            update_channel(self.directory, scene, "过时编辑", group, "", expected_revision=revision)
        state = player_channel_state(self.directory, self.pl_token)
        channel = next(c for c in state["channels"] if c["channel_id"] == scene)
        self.assertEqual((channel["name"], channel["group_id"], channel["description"]), ("旧码头", empty, "更新简介"))
        self.assertNotIn(group, [g["group_id"] for g in state["groups"]])
        with open_campaign(self.directory) as connection:
            self.assertEqual(connection.execute("SELECT message_id FROM messages").fetchone()[0], message["message_id"])
            self.assertEqual(connection.execute("SELECT count(*) FROM channel_memberships WHERE member_id = ? AND end_sequence IS NULL", (self.pl,)).fetchone()[0], 1)

    def test_v7_upgrade_preserves_identity_history_and_existing_membership(self):
        with patch.object(migrations, "SCHEMA_VERSION", 7):
            old = initialize_campaign(Path(self.temp.name), "旧团")
            member = add_member(old, "旧人物")
            scene = seed_legacy_scene(old, "旧频道", [member])
            auth = AuthService(old)
            token = auth.login("旧团", "旧人物", "123456")["token"]
            message, _ = save_lobby_message(old, "old", "旧记录", token=token, channel_id=scene)
        state = local_channel_state(old)
        self.assertEqual(state["groups"][0]["name"], "频道")
        self.assertTrue(all(c["group_id"] == state["groups"][0]["group_id"] for c in state["channels"]))
        second = create_scene(old, "新频道", [member])
        self.assertFalse(player_channel_state(old, token)["attributes_imported"])
        player_attributes(old, token, text=ATTRIBUTE_COMMAND)
        for channel in (scene, second):
            save_lobby_message(old, f"new-{channel}", "升级后发言", token=token, channel_id=channel)
        with open_campaign(old) as connection:
            self.assertEqual(campaign_info(connection)["schema_version"], migrations.SCHEMA_VERSION)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT message_id FROM messages WHERE request_id = 'old'").fetchone()[0], message["message_id"])


class LobbyRedesignGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.window = HostWindow(self.root)
        self.players = []
        self.wait(lambda: not self.window._busy)
        self.window.new_name.setText("界面验收团")
        self.window.open_button.click()
        self.wait(lambda: not self.window._busy)
        self.assertIsNotNone(self.window.campaign, self.window.feedback.text())
        self.directory = self.window.campaign["directory"]

    def tearDown(self):
        for player in self.players:
            player.close()
            player.deleteLater()
        self.window.server.stop()
        self.wait(lambda: not self.window.server.active and not self.window._busy)
        self.window.close()
        self.wait(lambda: not self.window._busy)
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def wait(self, predicate, timeout=12):
        until = time.monotonic() + timeout
        while True:
            self.app.processEvents()
            if predicate():
                return
            if time.monotonic() >= until:
                self.fail(self.window.feedback.text() + "\n" + self.window.logs.toPlainText())
            time.sleep(.01)

    def start(self, window=None):
        window = window or self.window
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        window.listen_port.setValue(port)
        window.network_button.click()
        self.wait(lambda: window.server.state == "running")
        return port

    def login(self, name, port, role="pl"):
        player = PlayerWindow(QSettings(str(self.root / f"{name}.ini"), QSettings.Format.IniFormat))
        self.players.append(player)
        player.address.setText(f"ws://127.0.0.1:{port}/ws/lobby")
        player.campaign_name.setText(self.window.campaign["name"])
        player.actor_name.setText(name)
        player.password.setText("123456")
        if role == "ob":
            player.ob_role.setChecked(True)
        player.login_button.click()
        self.wait(lambda: player.lobby is not None and player.lobby._channel_state_ready)
        return player

    def create_from_group_button(self, group_id, name, members):
        self.wait(lambda: not self.window._busy and self.window._channel_request is None)

        def fill():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, SceneDialog)
            self.assertEqual(dialog.group_choice.currentData(), group_id)
            dialog.name.setText(name)
            for index in range(dialog.members.count()):
                item = dialog.members.item(index)
                if item.data(Qt.ItemDataRole.UserRole) in members:
                    item.setCheckState(Qt.CheckState.Checked)
            dialog.accept()

        button = next(button for button in self.window.channel_choice.findChildren(QPushButton)
                      if button.accessibleName() == f"在{next(g['name'] for g in self.window._groups if g['group_id'] == group_id)}中新建频道")
        QTimer.singleShot(0, fill)
        button.click()
        self.wait(lambda: not self.window._busy and self.window._channel_request is None)
        return next(key for key, channel in self.window._channels.items() if channel["name"] == name)

    def test_dialog_migration_and_real_online_presence(self):
        member = add_member(self.directory, "调查员")
        self.window.members_refresh.click()
        self.wait(lambda: not self.window._busy)
        self.window.show()
        self.app.processEvents()
        sidebar = self.window._sidebar
        rail_buttons = (
            self.window.network_status, self.window.dice_management_button, self.window.participants_button,
            self.window.logs_button, self.window.network_button,
        )
        self.assertEqual([button.toolTip() for button in rail_buttons],
                         ["连接配置", "骰子", "参与者管理", "服务器日志", "服务器未开启"])
        for button in rail_buttons:
            self.assertEqual(button.toolButtonStyle(), Qt.ToolButtonStyle.ToolButtonIconOnly)
            self.assertFalse(button.icon().isNull())
            self.assertIs(button.parentWidget(), rail_buttons[0].parentWidget())
        self.assertIs(sidebar.actions.itemAt(0).widget(), rail_buttons[0])
        self.assertIs(sidebar.actions.itemAt(sidebar.actions.count() - 1).widget(), rail_buttons[-1])
        divider = sidebar.findChild(QFrame, "channelSidebarDivider")
        self.assertGreater(divider.y(), 0)
        self.assertLess(divider.geometry().bottom(), sidebar.rect().bottom())
        self.assertEqual(self.window.channel_choice.verticalScrollBar().styleSheet(), SLIM_SCROLLBAR_STYLE)

        avatar = sidebar.findChild(QLabel, "sidebarAvatar")
        self.assertEqual(sidebar.actions.indexOf(avatar), sidebar.actions.indexOf(self.window.logs_button) + 1)
        self.assertEqual(sidebar.actions.indexOf(avatar), sidebar.actions.indexOf(self.window.network_button) - 1)
        self.assertEqual(avatar.focusPolicy(), Qt.FocusPolicy.NoFocus)
        tooltip = self.window.findChild(QLabel, "sidebarTooltip")
        point = avatar.rect().center()
        QApplication.sendEvent(avatar, QHelpEvent(QEvent.Type.ToolTip, point, avatar.mapToGlobal(point)))
        self.assertTrue(tooltip.isVisible())
        self.assertEqual(tooltip.text(), "个人")
        QTest.mouseClick(self.window.windowHandle(), Qt.MouseButton.LeftButton, pos=avatar.mapTo(self.window, point))
        self.assertFalse(any(dialog.isVisible() for dialog in self.window.findChildren(QDialog)))
        self.assertEqual(self.window.server.state, "stopped")

        connection = self.window.network_status
        point = connection.rect().center()
        QApplication.sendEvent(connection, QHelpEvent(QEvent.Type.ToolTip, point, connection.mapToGlobal(point)))
        self.assertTrue(tooltip.isVisible())
        self.assertEqual(tooltip.text(), "连接配置")
        QTest.mouseClick(self.window.windowHandle(), Qt.MouseButton.LeftButton,
                         pos=connection.mapTo(self.window, QPoint(3, point.y())))
        self.assertTrue(self.window.connection_dialog.isVisible())
        self.assertEqual(self.window.connection_dialog.windowTitle(), "连接配置")
        self.assertTrue(self.window.listen_host.isEnabled())
        self.assertFalse(hasattr(self.window, "switch_button"))
        self.assertFalse(hasattr(self.window, "visibility_choice"))
        self.assertFalse(hasattr(self.window, "move_members_button"))
        self.window.participants_button.click()
        self.assertTrue(self.window.participants_dialog.isVisible())
        self.assertEqual([self.window.member_table.horizontalHeaderItem(c).text() for c in range(3)], ["身份", "名称", "密码"])
        previous_icon = self.window.network_button.icon().cacheKey()
        port = self.start()
        self.assertFalse(self.window.listen_host.isEnabled())
        self.assertEqual(self.window.network_button.toolTip(), "服务器已开启")
        self.assertNotEqual(self.window.network_button.icon().cacheKey(), previous_icon)
        player = self.login("调查员", port)
        player_avatar = player.lobby.personal_info_button
        host_pixmap = avatar.pixmap()
        player_pixmap = player_avatar.icon().pixmap(player_avatar.iconSize(), host_pixmap.devicePixelRatio())
        self.assertEqual(host_pixmap.deviceIndependentSize().toSize(), player_avatar.iconSize())
        host_image, player_image = host_pixmap.toImage(), player_pixmap.toImage()
        for x, y in ((0, 0), (host_image.width() // 4, host_image.height() // 2),
                     (host_image.width() // 2, host_image.height() * 3 // 8)):
            self.assertEqual(host_image.pixelColor(x, y), player_image.pixelColor(x, y))
        self.wait(lambda: member in self.window._online_members)
        self.assertEqual(self.window.connection_dialog.members.item(0, 2).text(), "在线")
        self.window.connection_dialog.close()
        self.assertEqual(self.window.server.state, "running")
        self.window.logs_button.click()
        self.assertTrue(self.window.runtime_log_dialog.isVisible())
        self.assertEqual(self.window.runtime_log_dialog.windowTitle(), "服务器日志")
        self.assertIn("调查员加入了", self.window.logs.toPlainText())
        player.lobby.connection_button.click()
        self.wait(lambda: member not in self.window._online_members)
        self.assertEqual(self.window.connection_dialog.members.item(0, 2).text(), "离线")
        self.window.network_button.click()
        self.wait(lambda: self.window.server.state == "stopped")
        self.assertEqual(self.window.network_button.toolTip(), "服务器未开启")
        self.assertEqual(self.window.network_button.icon().cacheKey(), previous_icon)

    def test_player_import_and_organizer_reset_update_live_speaking_permission(self):
        member = add_member(self.directory, "调查员")
        scene = create_scene(self.directory, "图书馆", [member])
        save_lobby_message(self.directory, "clue", "保留的线索", token=None, channel_id=scene)
        self.window.members_refresh.click()
        self.wait(lambda: not self.window._busy)
        unready = next(item for item in self.window.campaign["members"] if item["member_id"] == member)
        editor = MemberDialog(self.window, unready)
        self.assertFalse(editor.attributes_button.isEnabled())
        editor.deleteLater()
        player = self.login("调查员", self.start())
        self.window.show()
        player.show()
        lobby = player.lobby
        self.assertIn(scene, lobby._channels)
        lobby.channel_choice.setCurrentIndex(lobby.channel_choice.findData(scene))
        self.wait(lambda: "保留的线索" in lobby.chat_history.toPlainText())
        self.assertFalse(lobby.message.send_available)
        with self.assertRaises(ChatSendError) as denied:
            save_lobby_message(self.directory, "forged", ".ra 侦查", token=player.session.token, channel_id=scene)
        self.assertEqual(denied.exception.code, "attributes_required")

        lobby.personal_info_button.click()
        button = player.personal_info_dialog.attributes_button
        self.assertEqual(button.text(), "导入属性")
        button.click()
        self.wait(lambda: player._attribute_import is not None)
        paste = player._attribute_import
        paste.text.setPlainText(ATTRIBUTE_COMMAND.replace("str50", "str51"))
        paste.submit_button.click()
        self.wait(lambda: paste.submit_button.isEnabled())
        self.assertIn("不一致", paste.feedback.text())
        self.assertFalse(lobby.message.send_available)
        paste.text.setPlainText(f"| {ATTRIBUTE_COMMAND} |\n| --- |\n| |")
        paste.submit_button.click()
        self.wait(lambda: player._attribute_sheet is not None and lobby.message.send_available)
        self.assertFalse(hasattr(player._attribute_sheet, "reset_button"))
        self.assertEqual(player._attribute_sheet.attributes_table.item(0, 1).text(), "50")
        self.assertEqual(player._attribute_sheet.skills_table.item(0, 2).text(), "5")
        self.assertEqual(button.text(), "查看属性")
        self.wait(lambda: any(m["member_id"] == member and m["attributes_imported"]
                              for m in self.window.campaign["members"]) and not self.window._busy)

        self.window.participants_button.click()
        self.window.member_table.selectRow(next(
            i for i in range(self.window.member_table.rowCount())
            if self.window.member_table.item(i, 1).text() == "调查员"
        ))
        seen = []

        def reset_sheet():
            sheet = QApplication.activeModalWidget()
            if isinstance(sheet, AttributeSheetDialog):
                seen.append(sheet.attributes_table.item(0, 1).text())
                sheet.reset_button.click()

        def open_sheet():
            editor = QApplication.activeModalWidget()
            if isinstance(editor, MemberDialog):
                QTimer.singleShot(0, reset_sheet)
                editor.attributes_button.click()
                seen.append(editor.attributes_button.isEnabled())
                editor.reject()

        QTimer.singleShot(0, open_sheet)
        self.window.edit_button.click()
        self.assertEqual(seen, ["50", False])
        self.wait(lambda: not lobby.message.send_available and player._attribute_sheet is None)
        self.assertEqual(button.text(), "导入属性")
        self.assertIn("保留的线索", lobby.chat_history.toPlainText())
        self.assertEqual(lobby._channel_id, scene)
        self.assertIsNone(player_attributes(self.directory, player.session.token)["attributes"])

    @unittest.skipUnless(
        shutil.which("node") and (Path(__file__).resolve().parents[1] / "dice_component/node_modules/bcdice").is_dir(),
        "运行 npm ci --prefix dice_component 安装骰核",
    )
    def test_dice_settings_and_private_result_in_host_chat(self):
        self.window.show()
        sidebar = self.window.participants_button.parentWidget().layout()
        self.assertEqual(sidebar.indexOf(self.window.dice_management_button) + 1,
                         sidebar.indexOf(self.window.participants_button))
        self.assertEqual(self.window.dice_management_button.text(), "骰子")
        outcomes = []
        labels = []

        def edit_dice_settings():
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, DiceManagementDialog):
                labels.extend([dialog.windowTitle(), dialog.preset.itemText(0), dialog.preset.itemText(1)])
                compact_height = dialog.height()
                outcomes.append(dialog.custom_panel.isHidden())
                dialog.preset.setCurrentIndex(1)
                QApplication.processEvents()
                outcomes.append(dialog.custom_panel.isVisible() and dialog.height() > compact_height)
                dialog.critical_max.setValue(95)
                dialog.accept()
                outcomes.append(dialog.isVisible() and dialog.feedback.isVisible())
                dialog.preset.setCurrentIndex(0)
                QApplication.processEvents()
                outcomes.append(dialog.custom_panel.isHidden() and dialog.feedback.isHidden()
                                and dialog.height() == compact_height)
                dialog.preset.setCurrentIndex(1)
                dialog.critical_max.setValue(5)
                dialog.fumble_min.setValue(94)
                dialog.accept()

        QTimer.singleShot(0, edit_dice_settings)
        self.window.dice_management_button.click()
        self.assertEqual(labels, ["骰子", "COC7 原版", "村规"])
        self.assertEqual(outcomes, [True, True, True, True])
        config = local_dice_config(self.directory)
        self.assertEqual((config.custom, config.critical_max, config.fumble_min), (True, 5, 94))

        self.start()
        self.window.message_input.setPlainText(".rh 1d1")
        QTest.keyClick(self.window.message_input, Qt.Key.Key_Return)
        self.wait(lambda: "骰子\n【暗骰】" in self.window.messages.toPlainText() and self.window._pending_send is None)
        self.assertIn("1D1 [1] = 1", self.window.messages.toPlainText())
        self.assertEqual(self.window.messages.newest_public_sequence, 0)

    def test_group_buttons_parallel_client_channels_metadata_and_complete_history(self):
        member = add_ready_player(self.directory, "调查员")
        observer = add_member(self.directory, "看客", role="ob")
        self.window.members_refresh.click()
        self.wait(lambda: not self.window._busy)
        with patch("host.host_window.QInputDialog.getText", return_value=("调查", True)):
            self.window.create_group_button.click()
        self.wait(lambda: not self.window._busy)
        group = next(g["group_id"] for g in self.window._groups if g["name"] == "调查")
        port = self.start()
        player = self.login("调查员", port)
        lobby_id = player.lobby._channel_id
        first = self.create_from_group_button(group, "码头", [member, observer])
        second = self.create_from_group_button(group, "钟楼", [member, observer])
        self.wait(lambda: second in player.lobby._channels)
        self.assertEqual(player.lobby._channel_id, lobby_id)
        self.window.server.command("say", text="码头有新线索", channel_id=first)
        unread_role = Qt.ItemDataRole.AccessibleDescriptionRole
        player_tree = player.lobby.channel_choice
        self.wait(lambda: player_tree._items[first].data(0, unread_role) == "有未读消息")
        player_tree.setCurrentIndex(player_tree.findData(first))
        self.wait(lambda: player.lobby._history_request is None)
        self.assertEqual(player_tree._items[first].data(0, unread_role), "")
        for channel in (first, second, lobby_id):
            player.lobby.channel_choice.setCurrentIndex(player.lobby.channel_choice.findData(channel))
            self.wait(lambda: player.lobby._history_request is None)
            self.assertTrue(player.lobby.message.send_available)
            player.lobby.message.setPlainText(f"发送到 {channel}")
            QTest.keyClick(player.lobby.message, Qt.Key.Key_Return)
            self.wait(lambda: not player.lobby._awaiting_send)
        self.window.channel_choice.setCurrentIndex(self.window.channel_choice.findData(first))
        self.wait(lambda: not self.window._busy)
        self.window.channel_members_button.click()
        self.assertEqual(self.window._members_dialogs[first].members.rowCount(), 2)
        self.window._members_dialogs[first].close()

        # More than two history pages; the log window stays bound to its opening channel.
        for number in range(111):
            save_lobby_message(self.directory, f"history-{number}", f"完整记录 {number:03d}", token=None, channel_id=first)
        self.window.channel_log_button.click()
        dialog = self.window._history_dialogs[first]
        self.wait(lambda: dialog.loaded and not self.window._busy)
        while dialog.has_more:
            dialog.history.request_older()
            self.wait(lambda: not self.window._busy)
        self.assertIn("完整记录 000", dialog.history.toPlainText())
        self.assertIn("完整记录 110", dialog.history.toPlainText())
        self.window.channel_choice.setCurrentIndex(self.window.channel_choice.findData(second))
        self.wait(lambda: not self.window._busy)
        self.assertEqual(dialog.channel_id, first)
        self.assertIn("码头", dialog.windowTitle())

        self.window.channel_management_button.click()
        management = self.window.channel_management_dialog
        row = next(row for row in range(management.table.rowCount())
                   if management.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == second)
        management.table.selectRow(row)
        management.name.setText("高塔")
        management.description.setPlainText("夜间调查")
        management.save.click()
        self.wait(lambda: self.window._channels[second]["name"] == "高塔" and self.window._channel_request is None)
        self.assertEqual(self.window.channel_title.text(), "高塔")
        self.assertEqual(self.window.channel_info_button.text(), "夜间调查")
        self.wait(lambda: player.lobby._channels[second]["name"] == "高塔")
        # Saving unchanged values must not make a later edit falsely stale.
        management.save.click()
        self.wait(lambda: self.window._channel_request is None)
        management.description.setPlainText("更新后的简介")
        management.save.click()
        self.wait(lambda: self.window._channel_request is None)
        self.assertEqual(self.window._channels[second]["description"], "更新后的简介")
        self.assertEqual(self.window.channel_info_button.text(), "更新后的简介")

    def test_closing_listening_main_page_returns_to_identity_selection(self):
        role = RoleWindow(self.root)
        role.show()
        role._open_organizer()
        host = role._window
        self.wait(lambda: not host._busy)
        host.new_name.setText("关闭验收团")
        host.open_button.click()
        self.wait(lambda: not host._busy)
        self.start(host)
        host.network_status.click()
        self.assertTrue(host.connection_dialog.isVisible())
        host.title_bar.close_button.click()
        self.wait(lambda: role._window is None and role.isVisible())
        role.deleteLater()
        self.app.processEvents()

    def test_late_channel_state_is_discarded_after_returning_to_selector(self):
        state = local_channel_state(self.directory)
        self.window._queued_channel_state = state
        self.window._switch_campaign()
        self.wait(lambda: self.window.campaign is None and not self.window._busy)
        self.assertIsNone(self.window._queued_channel_state)
        self.window._populate_channels(state)
        self.assertIsNone(self.window.campaign)
        self.window.open_button.click()
        self.wait(lambda: self.window.campaign is not None and not self.window._busy)
        self.assertEqual(self.window._channel_id, self.window.campaign["lobby_id"])


if __name__ == "__main__":
    unittest.main()
