"""当前系统用户的只读历史副本，按团和成员隔离。"""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from chat_protocol import HISTORY_PAGE_SIZE


def is_saved_message(message: object) -> bool:
    return (isinstance(message, dict) and type(message.get("sequence")) is int
            and 0 < message["sequence"] < 2**63
            and all(isinstance(message.get(key), str) for key in ("message_id", "nickname", "text")))


class HistoryCache:
    def __init__(self, path: Path, campaign_id: str, member_id: str) -> None:
        self.path = path
        self.scope = (campaign_id, member_id)

    @contextmanager
    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=0.25)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("""CREATE TABLE IF NOT EXISTS messages (
                campaign_id TEXT NOT NULL, member_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                message_id TEXT NOT NULL, nickname TEXT NOT NULL, text TEXT NOT NULL,
                PRIMARY KEY (campaign_id, member_id, sequence), UNIQUE (campaign_id, member_id, message_id)
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS sync_positions (
                campaign_id TEXT NOT NULL, member_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                PRIMARY KEY (campaign_id, member_id)
            )""")
            yield connection

    def page(self, before: int | None = None) -> list[dict]:
        with self._open() as connection:
            rows = connection.execute(
                """SELECT message_id, sequence, nickname, text FROM messages
                   WHERE campaign_id = ? AND member_id = ? AND sequence <= ?
                   ORDER BY sequence DESC LIMIT ?""",
                (*self.scope, before - 1 if before is not None else 2**63 - 1, HISTORY_PAGE_SIZE),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def position(self) -> int | None:
        with self._open() as connection:
            return self._position(connection)

    def _position(self, connection: sqlite3.Connection) -> int | None:
        row = connection.execute(
            "SELECT sequence FROM sync_positions WHERE campaign_id = ? AND member_id = ?", self.scope,
        ).fetchone()
        return row[0] if row is not None else None

    def save(self, messages: list[dict], *, latest: bool = False) -> None:
        with self._open() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """INSERT INTO messages (campaign_id, member_id, sequence, message_id, nickname, text)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                ((*self.scope, item["sequence"], item["message_id"], item["nickname"], item["text"])
                 for item in messages),
            )
            previous_position = position = self._position(connection)
            if position is None:
                if not latest:
                    return
                position = messages[-1]["sequence"] if messages else 0

            # 实时消息可能先于补同步返回，只有连续存入的记录才能推进游标。
            for row in connection.execute(
                """SELECT sequence FROM messages WHERE campaign_id = ? AND member_id = ? AND sequence > ?
                   ORDER BY sequence""", (*self.scope, position),
            ):
                if row[0] != position + 1:
                    break
                position = row[0]
            if position != previous_position:
                connection.execute(
                    """INSERT INTO sync_positions VALUES (?, ?, ?) ON CONFLICT (campaign_id, member_id)
                       DO UPDATE SET sequence = excluded.sequence""", (*self.scope, position),
                )
