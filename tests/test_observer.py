"""OB 身份、自动旁观、历史范围及旧团迁移的回归。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from host import migrations
from host.auth import AuthError, AuthService
from host.channels import archive_scene, create_scene, get_channel_access, move_members, player_channel_state, set_visibility_mode
from host.management import add_member, edit_member, list_members
from host.messages import ChatSendError, lobby_history, save_lobby_message
from host.storage import campaign_info, initialize_campaign, open_campaign
from tests.legacy_channel import seed_legacy_scene
from tests.attribute_fixtures import add_ready_player


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = initialize_campaign(Path(self.temp.name), "旁观团")
        self.pl = add_ready_player(self.directory, "调查员")
        self.ob = add_member(self.directory, "看客", role="ob", password="观众密码")
        self.auth = AuthService(self.directory)
        self.token = self.auth.login("旁观团", "看客", "观众密码", "ob")["token"]

    def test_automatic_observation_late_join_archive_and_both_policies(self):
        scene = create_scene(self.directory, "地下室", [self.pl])
        save_lobby_message(self.directory, "old", "加入前的秘密", token=None, channel_id=scene)
        archived = create_scene(self.directory, "旧房间", [])
        save_lobby_message(self.directory, "archive", "已归档的秘密", token=None, channel_id=archived)
        archive_scene(self.directory, archived)
        add_member(self.directory, "迟到观众", role="ob")
        late_token = self.auth.login("旁观团", "迟到观众", "123456", "ob")["token"]
        for mode in ("broad", "secret"):
            set_visibility_mode(self.directory, mode)
            for token in (self.token, late_token):
                state = player_channel_state(self.directory, token)
                channels = {item["channel_id"]: item for item in state["channels"]}
                self.assertTrue(channels[state["current_channel_id"]]["can_send"])
                for key in (scene, archived):
                    self.assertEqual(channels[key]["history_ranges"], ((1, None),))
                    self.assertFalse(channels[key]["can_send"])
                    self.assertEqual(len(lobby_history(self.directory, token, "latest", 0, key)["messages"]), 1)
        future = create_scene(self.directory, "新房间", [])
        self.assertIn(future, [item["channel_id"] for item in player_channel_state(self.directory, self.token)["channels"]])
        with open_campaign(self.directory) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM channel_memberships WHERE member_id = ?", (self.ob,)).fetchone()[0], 0)

    def test_lobby_send_scene_forgery_and_pl_assignment_are_enforced(self):
        scene = create_scene(self.directory, "现场", [self.pl])
        message, _ = save_lobby_message(self.directory, "ooc", "场外聊天", token=self.token)
        self.assertEqual(message["nickname"], "看客")
        with self.assertRaises(ChatSendError) as refused:
            save_lobby_message(self.directory, "forged", "插入故事", token=self.token, channel_id=scene)
        self.assertEqual(refused.exception.code, "channel_forbidden")
        with self.assertRaises(ValueError):
            move_members(self.directory, [self.ob], scene)
        another = create_scene(self.directory, "OB 同时参与", [self.ob])
        with self.assertRaises(ChatSendError):
            save_lobby_message(self.directory, "ob-member-readonly", "仍然不能发言", token=self.token, channel_id=another)
        with open_campaign(self.directory) as connection:
            self.assertTrue(get_channel_access(connection, self.pl, message["channel_id"]).can_send)
        other = initialize_campaign(Path(self.temp.name), "其他团")
        with open_campaign(other) as connection:
            foreign_channel = campaign_info(connection)["lobby_id"]
        with open_campaign(self.directory) as connection:
            self.assertFalse(get_channel_access(connection, self.ob, foreign_channel).can_read)

    def test_nickname_password_identity_and_name_uniqueness(self):
        before, _ = save_lobby_message(self.directory, "before", "旧昵称记录", token=self.token)
        result = self.auth.change_nickname(self.token, "新看客")
        self.assertEqual(result["member_id"], self.ob)
        after, _ = save_lobby_message(self.directory, "after", "新昵称记录", token=self.token)
        self.assertEqual((before["nickname"], after["nickname"]), ("看客", "新看客"))
        with self.assertRaises(AuthError):
            self.auth.login("旁观团", "看客", "观众密码", "ob")
        for nickname in ("调查员", "KP", "   "):
            with self.assertRaises(AuthError):
                self.auth.change_nickname(self.token, nickname)
        token_pl = self.auth.login("旁观团", "调查员", "123456")["token"]
        with self.assertRaises(AuthError) as refused:
            self.auth.change_nickname(token_pl, "改角色名")
        self.assertEqual(refused.exception.status_code, 403)
        self.auth.change_password(self.token, "观众密码", "新密码")
        with self.assertRaises(AuthError):
            self.auth.login("旁观团", "新看客", "观众密码", "ob")
        ready = self.auth.login("旁观团", "新看客", "新密码", "ob")
        self.assertEqual(ready["role"], "ob")
        member = next(item for item in list_members(self.directory) if item["member_id"] == self.ob)
        self.assertEqual(member["password"], "新密码")
        edit_member(self.directory, self.ob, "KP重命名的观众")
        self.assertEqual(self.auth.require_session(ready["token"]).actor_name, "KP重命名的观众")
        with self.assertRaises(ValueError):
            add_member(self.directory, "调查员", role="ob")

    def test_v6_upgrade_preserves_references_and_live_credentials(self):
        with patch.object(migrations, "SCHEMA_VERSION", 6):
            directory = initialize_campaign(Path(self.temp.name), "旧团")
            pl = add_member(directory, "老角色")
            auth = AuthService(directory)
            token = auth.login("旧团", "老角色", "123456")["token"]
            scene = seed_legacy_scene(directory, "旧场景", [pl])
            message, _ = save_lobby_message(directory, "old", "旧消息", token=token, channel_id=scene)
        with open_campaign(directory) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT message_id FROM messages").fetchone()[0], message["message_id"])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE actors SET member_id = 'missing'")
        self.assertEqual(auth.require_session(token).member_id, pl)
        add_member(directory, "新观众", role="ob")
        ob_token = auth.login("旧团", "新观众", "123456", "ob")["token"]
        self.assertEqual(lobby_history(directory, ob_token, "latest", 0, scene)["messages"][0]["message_id"], message["message_id"])
