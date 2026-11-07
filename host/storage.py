"""主机独占的团数据库及初始化入口。"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .migrations import migrate

DATABASE_NAME = "story.sqlite3"


def _connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    mode = "rwc" if create else "rw"
    connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def campaign_info(connection: sqlite3.Connection) -> dict[str, object]:
    """读取可跨进程重开的稳定身份与默认测试场次。"""
    row = connection.execute(
        """SELECT c.id AS campaign_id, c.name, c.ruleset, c.created_at,
                  m.id AS kp_member_id, m.name AS kp_name,
                  a.id AS kp_actor_id,
                  s.id AS session_id, ch.id AS lobby_id
           FROM campaigns AS c
           JOIN members AS m ON m.campaign_id = c.id AND m.role = 'kp'
           JOIN actors AS a ON a.member_id = m.id AND a.kind = 'kp'
           JOIN play_sessions AS s ON s.campaign_id = c.id
           JOIN channels AS ch ON ch.session_id = s.id AND ch.kind = 'lobby'
           ORDER BY s.started_at, s.id LIMIT 1"""
    ).fetchone()
    if row is None:
        raise ValueError("团数据库缺少初始化记录。")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    return {**dict(row), "schema_version": version}


@contextmanager
def open_campaign(directory: Path) -> Iterator[sqlite3.Connection]:
    """打开已有团并迁移，路径错误时不会创建新数据库。"""
    path = directory.resolve() / DATABASE_NAME
    with closing(_connect(path)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            migrate(connection)
            campaign_info(connection)
        yield connection


def initialize_campaign(data_root: Path, name: str, kp_name: str) -> Path:
    """在按不可变团 ID 命名的新目录中，原子创建一份团数据。"""
    name = name.strip()
    kp_name = kp_name.strip()
    if not name or not kp_name:
        raise ValueError("团名和 KP 名称不能为空。")

    campaign_id, session_id, lobby_id, member_id, actor_id = (
        str(uuid4()) for _ in range(5)
    )
    directory = data_root.resolve() / campaign_id
    directory.mkdir(parents=True)
    path = directory / DATABASE_NAME
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with closing(_connect(path, create=True)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                migrate(connection)
                connection.execute(
                    "INSERT INTO campaigns (id, name, ruleset, created_at) VALUES (?, ?, ?, ?)",
                    (campaign_id, name, "coc7", created_at),
                )
                connection.execute(
                    """INSERT INTO play_sessions
                       (id, campaign_id, name, status, started_at)
                       VALUES (?, ?, ?, 'active', ?)""",
                    (session_id, campaign_id, "测试场次", created_at),
                )
                connection.execute(
                    """INSERT INTO channels (id, session_id, name, kind, status, created_at)
                       VALUES (?, ?, ?, 'lobby', 'active', ?)""",
                    (lobby_id, session_id, "大厅", created_at),
                )
                connection.execute(
                    """INSERT INTO members (id, campaign_id, name, role, created_at)
                       VALUES (?, ?, ?, 'kp', ?)""",
                    (member_id, campaign_id, kp_name, created_at),
                )
                connection.execute(
                    """INSERT INTO actors (id, member_id, name, kind, created_at)
                       VALUES (?, ?, ?, 'kp', ?)""",
                    (actor_id, member_id, kp_name, created_at),
                )
    except Exception:
        # 目录由本次 UUID 初始化独占创建，失败后清理空数据库。
        with suppress(OSError):
            path.unlink(missing_ok=True)
            directory.rmdir()
        raise
    return directory
