"""分线关系、阅读策略、归档、旧库升级与缓存隔离。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from client.history_cache import HistoryCache
from host import migrations
from host.auth import AuthService, DEFAULT_PASSWORD
from host.channels import (
    BroadReadPolicy, archive_scene, create_scene, get_channel_access,
    player_channel_state, set_visibility_mode,
)
from host.management import add_member
from host.messages import ChatSendError, lobby_history, save_lobby_message
from host.storage import campaign_info, initialize_campaign, open_campaign


class ChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = initialize_campaign(Path(self.temporary.name), "分线团")
        self.a = add_member(self.directory, "A")
        self.b = add_member(self.directory, "B")
        self.auth = AuthService(self.directory)
        self.token_a = self.auth.login("分线团", "A", DEFAULT_PASSWORD)["token"]
        self.token_b = self.auth.login("分线团", "B", DEFAULT_PASSWORD)["token"]

    def test_visibility_and_speaking_are_independent_and_archive_is_read_only(self) -> None:
        scene = create_scene(self.directory, "地下室", [self.a])
        save_lobby_message(self.directory, "clue", "秘密线索", token=self.token_a, channel_id=scene)
        self.assertEqual(len(player_channel_state(self.directory, self.token_b)["channels"]), 1)
        with self.assertRaises(ChatSendError):
            lobby_history(self.directory, self.token_b, "latest", 0, scene)
        with open_campaign(self.directory) as connection:
            access = get_channel_access(connection, self.b, scene, policy=BroadReadPolicy())
            self.assertTrue(access.can_read)
            self.assertFalse(access.can_send)
        set_visibility_mode(self.directory, "broad")
        self.assertEqual(lobby_history(self.directory, self.token_b, "latest", 0, scene)["messages"][0]["text"], "秘密线索")
        with self.assertRaises(ChatSendError):
            save_lobby_message(self.directory, "forged", "闯入", token=self.token_b, channel_id=scene)
        archive_scene(self.directory, scene)
        archive_scene(self.directory, scene)
        state = player_channel_state(self.directory, self.token_a)
        self.assertNotEqual(state["current_channel_id"], scene)
        self.assertFalse(next(item for item in state["channels"] if item["channel_id"] == scene)["can_send"])
        with self.assertRaises(ChatSendError):
            save_lobby_message(self.directory, "after-end", "继续", token=self.token_a, channel_id=scene)
        # 已经保存的同一请求在归档后仍能得到原确认。
        message, created = save_lobby_message(self.directory, "clue", "秘密线索", token=self.token_a, channel_id=scene)
        self.assertFalse(created)
        self.assertEqual(message["sequence"], 1)

    def test_participation_intervals_filter_history_and_live_access(self) -> None:
        scene = create_scene(self.directory, "客厅", [])
        for number in range(1, 7):
            save_lobby_message(self.directory, f"line-{number}", str(number), token=None, channel_id=scene)
        with open_campaign(self.directory) as connection, connection:
            connection.executemany(
                "INSERT INTO channel_memberships(member_id, channel_id, start_sequence, end_sequence) VALUES (?, ?, ?, ?)",
                [(self.a, scene, 2, 3), (self.a, scene, 5, 5)],
            )
            access = get_channel_access(connection, self.a, scene)
            self.assertEqual([n for n in range(1, 7) if access.includes(n)], [2, 3, 5])
            self.assertFalse(access.can_send)
        self.assertEqual([item["sequence"] for item in lobby_history(self.directory, self.token_a, "latest", 0, scene)["messages"]], [2, 3, 5])
        self.assertEqual([item["sequence"] for item in lobby_history(self.directory, self.token_a, "after", 3, scene)["messages"]], [5])
        self.assertEqual([item["sequence"] for item in lobby_history(self.directory, self.token_a, "before", 5, scene)["messages"]], [2, 3])

    def test_scene_creation_is_atomic_idempotent_and_rejects_occupied_members(self) -> None:
        scene = create_scene(self.directory, "地下室", [self.a], channel_id="scene-request")
        self.assertEqual(create_scene(self.directory, "地下室", [self.a], channel_id="scene-request"), scene)
        for participants in ([self.b], [self.b, "missing"], [self.a]):
            with self.subTest(participants=participants), self.assertRaises(ValueError):
                create_scene(self.directory, "另一间", participants, channel_id="scene-request" if participants == [self.b] else "other")
        with open_campaign(self.directory) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM channels WHERE kind = 'scene'").fetchone()[0], 1)
            lobby = campaign_info(connection)["lobby_id"]
        with self.assertRaises(ChatSendError):
            save_lobby_message(self.directory, "lobby-forgery", "回大厅发言", token=self.token_a, channel_id=lobby)

    def test_v4_upgrade_preserves_messages_ids_and_defaults_to_secret(self) -> None:
        with patch.object(migrations, "SCHEMA_VERSION", 4):
            directory = initialize_campaign(Path(self.temporary.name), "旧团")
            with open_campaign(directory) as connection:
                before = campaign_info(connection)
                # v4 写入路径由旧数据结构直接建种子，保留原序号与消息 ID。
                connection.execute(
                    """INSERT INTO messages VALUES ('old-message', ?, 1, ?, ?, 'KP', 'old-request', 'text', '旧记录', '2026-11-10')""",
                    (before["lobby_id"], before["kp_member_id"], before["kp_actor_id"]),
                )
                connection.commit()
        with open_campaign(directory) as connection:
            self.assertEqual(campaign_info(connection), {**before, "schema_version": migrations.SCHEMA_VERSION})
            self.assertEqual(connection.execute("SELECT message_id FROM messages").fetchone()[0], "old-message")
            self.assertEqual(connection.execute("SELECT visibility_mode FROM campaigns").fetchone()[0], "secret")


class ChannelCacheTests(unittest.TestCase):
    def test_legacy_cache_migrates_and_channel_sequences_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE messages(campaign_id, member_id, sequence, message_id, nickname, text)")
                connection.execute("CREATE TABLE sync_positions(campaign_id, member_id, sequence)")
                connection.execute("INSERT INTO messages VALUES ('c', 'm', 1, 'old', 'KP', '旧大厅')")
                connection.execute("INSERT INTO sync_positions VALUES ('c', 'm', 1)")
            root = HistoryCache(path, "c", "m")
            self.assertEqual(root.position(), 1)
            scene = root.for_channel("scene")
            scene.save([{"message_id": "scene-one", "sequence": 1, "nickname": "KP", "text": "地下室"}], latest=True)
            self.assertEqual(scene.page()[0]["text"], "地下室")
            self.assertEqual(root.page()[0]["text"], "旧大厅")

    def test_policy_changes_prune_cached_content_and_reset_gap_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = HistoryCache(Path(directory) / "history.sqlite3", "c", "m")
            state = {"current_channel_id": "lobby", "channels": [
                {"channel_id": "lobby", "kind": "lobby", "history_ranges": [[1, None]]},
                {"channel_id": "scene", "kind": "scene", "history_ranges": [[1, None]]},
            ]}
            root.save_channel_state(state)
            scene = root.for_channel("scene")
            records = [{"message_id": str(n), "sequence": n, "nickname": "KP", "text": str(n)} for n in range(1, 7)]
            scene.save(records, latest=True)
            state["channels"][1]["history_ranges"] = [[2, 3], [5, 5]]
            root.save_channel_state(state)
            self.assertIsNone(scene.position())
            self.assertEqual([item["sequence"] for item in scene.page()], [2, 3, 5])
            scene.save(scene.page(), synced_through=5)
            self.assertEqual(scene.position(), 5)
            state["channels"].pop()
            root.save_channel_state(state)
            self.assertEqual(scene.page(), [])
            self.assertEqual(root.channel_state(), state)
