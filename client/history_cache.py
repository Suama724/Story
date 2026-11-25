"""当前系统用户的历史副本，按团、成员和频道隔离。"""

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from chat_protocol import HISTORY_PAGE_SIZE


def is_saved_message(message: object) -> bool:
    return (isinstance(message, dict) and type(message.get("sequence")) is int
            and 0 < message["sequence"] < 2**63
            and all(isinstance(message.get(key), str) for key in ("message_id", "nickname", "text")))


class HistoryCache:
    def __init__(self, path: Path, campaign_id: str, member_id: str, channel_id: str = "") -> None:
        self.path = path
        self.scope = (campaign_id, member_id, channel_id)

    def for_channel(self, channel_id: str, *, lobby: bool = False) -> "HistoryCache":
        # 旧版缓存全部属于大厅，沿用空字符串键保存旧消息和游标。
        return HistoryCache(self.path, *self.scope[:2], "" if lobby else channel_id)

    @contextmanager
    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=0.25)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                old_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
                if old_columns and "channel_id" not in old_columns:
                    connection.execute("ALTER TABLE messages RENAME TO legacy_messages")
                    connection.execute("ALTER TABLE sync_positions RENAME TO legacy_sync_positions")
                connection.execute("""CREATE TABLE IF NOT EXISTS messages (
                campaign_id TEXT NOT NULL, member_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                message_id TEXT NOT NULL, nickname TEXT NOT NULL, text TEXT NOT NULL, kind TEXT,
                PRIMARY KEY (campaign_id, member_id, channel_id, sequence),
                UNIQUE (campaign_id, member_id, channel_id, message_id))""")
                connection.execute("""CREATE TABLE IF NOT EXISTS sync_positions (
                campaign_id TEXT NOT NULL, member_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, PRIMARY KEY (campaign_id, member_id, channel_id))""")
                if old_columns and "channel_id" not in old_columns:
                    connection.execute(
                        """INSERT INTO messages (campaign_id, member_id, channel_id, sequence, message_id, nickname, text)
                           SELECT campaign_id, member_id, '', sequence, message_id, nickname, text FROM legacy_messages""",
                    )
                    connection.execute("INSERT INTO sync_positions SELECT campaign_id, member_id, '', sequence FROM legacy_sync_positions")
                    connection.execute("DROP TABLE legacy_messages")
                    connection.execute("DROP TABLE legacy_sync_positions")
                elif old_columns and "kind" not in old_columns:
                    connection.execute("ALTER TABLE messages ADD COLUMN kind TEXT")
                connection.execute("""CREATE TABLE IF NOT EXISTS channel_states (
                    campaign_id TEXT NOT NULL, member_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (campaign_id, member_id))""")
            yield connection

    def channel_state(self) -> dict | None:
        with self._open() as connection:
            row = connection.execute("SELECT payload FROM channel_states WHERE campaign_id = ? AND member_id = ?",
                                     self.scope[:2]).fetchone()
        return json.loads(row[0]) if row is not None else None

    def save_channel_state(self, state: dict) -> None:
        previous = self.channel_state()
        old_ranges = {item["channel_id"]: item["history_ranges"] for item in previous["channels"]} if previous else {}
        with self._open() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            allowed_keys = []
            for channel in state["channels"]:
                key = "" if channel["kind"] == "lobby" else channel["channel_id"]
                allowed_keys.append(key)
                scope = (*self.scope[:2], key)
                ranges = channel["history_ranges"]
                clauses = " OR ".join("(sequence >= ? AND sequence <= ?)" for _ in ranges) or "0"
                bounds = [bound for start, end in ranges for bound in (start, end if end is not None else 2**63 - 1)]
                connection.execute(
                    f"DELETE FROM messages WHERE campaign_id = ? AND member_id = ? AND channel_id = ? AND NOT ({clauses})",
                    (*scope, *bounds),
                )
                if channel["channel_id"] in old_ranges and old_ranges[channel["channel_id"]] != ranges:
                    connection.execute("DELETE FROM sync_positions WHERE campaign_id = ? AND member_id = ? AND channel_id = ?", scope)
            placeholders = ",".join("?" for _ in allowed_keys) or "NULL"
            for table in ("messages", "sync_positions"):
                connection.execute(
                    f"DELETE FROM {table} WHERE campaign_id = ? AND member_id = ? AND channel_id NOT IN ({placeholders})",
                    (*self.scope[:2], *allowed_keys),
                )
            connection.execute(
                "INSERT INTO channel_states VALUES (?, ?, ?) ON CONFLICT(campaign_id, member_id) DO UPDATE SET payload = excluded.payload",
                (*self.scope[:2], json.dumps(state, ensure_ascii=False)),
            )

    def page(self, before: int | None = None) -> list[dict]:
        with self._open() as connection:
            rows = connection.execute(
                """SELECT message_id, sequence, nickname, text, kind FROM messages
                   WHERE campaign_id = ? AND member_id = ? AND channel_id = ? AND sequence <= ?
                   ORDER BY sequence DESC LIMIT ?""",
                (*self.scope, before - 1 if before is not None else 2**63 - 1, HISTORY_PAGE_SIZE),
            ).fetchall()
        return [{key: value for key, value in dict(row).items() if value is not None} for row in reversed(rows)]

    def position(self) -> int | None:
        with self._open() as connection:
            return self._position(connection)

    def _position(self, connection: sqlite3.Connection) -> int | None:
        row = connection.execute(
            "SELECT sequence FROM sync_positions WHERE campaign_id = ? AND member_id = ? AND channel_id = ?", self.scope,
        ).fetchone()
        return row[0] if row is not None else None

    def save(self, messages: list[dict], *, latest: bool = False, synced_through: int | None = None) -> None:
        with self._open() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """INSERT INTO messages (campaign_id, member_id, channel_id, sequence, message_id, nickname, text, kind)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT DO UPDATE SET kind = excluded.kind
                   WHERE messages.message_id = excluded.message_id AND messages.kind IS NULL
                         AND excluded.kind IS NOT NULL""",
                ((*self.scope, item["sequence"], item["message_id"], item["nickname"], item["text"], item.get("kind"))
                 for item in messages),
            )
            previous_position = position = self._position(connection)
            if synced_through is not None:
                position = max(position or 0, synced_through)
            if position is None:
                if not latest:
                    return
                position = messages[-1]["sequence"] if messages else 0

            # 实时消息可能先于补同步返回，只有连续存入的记录才能推进游标。
            for row in connection.execute(
                """SELECT sequence FROM messages WHERE campaign_id = ? AND member_id = ? AND channel_id = ? AND sequence > ?
                   ORDER BY sequence""", (*self.scope, position),
            ):
                if row[0] != position + 1:
                    break
                position = row[0]
            if position != previous_position:
                connection.execute(
                    """INSERT INTO sync_positions VALUES (?, ?, ?, ?) ON CONFLICT (campaign_id, member_id, channel_id)
                       DO UPDATE SET sequence = excluded.sequence""", (*self.scope, position),
                )
