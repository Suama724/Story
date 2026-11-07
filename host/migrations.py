"""团数据库的顺序迁移，调用方负责事务。"""

import sqlite3

APPLICATION_ID = 0x53545259  # STRY
SCHEMA_VERSION = 1

MIGRATIONS = (
    (
        """CREATE TABLE campaigns (
            id TEXT PRIMARY KEY NOT NULL,
            singleton INTEGER NOT NULL UNIQUE DEFAULT 1 CHECK (singleton = 1),
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            ruleset TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE play_sessions (
            id TEXT PRIMARY KEY NOT NULL,
            campaign_id TEXT NOT NULL REFERENCES campaigns(id),
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'ended')),
            started_at TEXT NOT NULL,
            ended_at TEXT,
            CHECK ((status = 'active' AND ended_at IS NULL)
                OR (status = 'ended' AND ended_at IS NOT NULL))
        )""",
        """CREATE TABLE channels (
            id TEXT PRIMARY KEY NOT NULL,
            session_id TEXT NOT NULL REFERENCES play_sessions(id),
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('lobby', 'scene')),
            status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
            created_at TEXT NOT NULL
        )""",
        """CREATE UNIQUE INDEX one_lobby_per_session
            ON channels(session_id) WHERE kind = 'lobby'""",
        """CREATE TABLE members (
            id TEXT PRIMARY KEY NOT NULL,
            campaign_id TEXT NOT NULL REFERENCES campaigns(id),
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            role TEXT NOT NULL CHECK (role IN ('kp', 'pl')),
            created_at TEXT NOT NULL
        )""",
        """CREATE UNIQUE INDEX one_kp_per_campaign
            ON members(campaign_id) WHERE role = 'kp'""",
        """CREATE TABLE actors (
            id TEXT PRIMARY KEY NOT NULL,
            member_id TEXT NOT NULL REFERENCES members(id),
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            kind TEXT NOT NULL CHECK (kind IN ('kp', 'pc', 'npc')),
            created_at TEXT NOT NULL
        )""",
    ),
)


def migrate(connection: sqlite3.Connection) -> None:
    """拒绝其他应用及较新版本的数据库，在现有事务内逐版升级。"""
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if application_id != APPLICATION_ID:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if application_id != 0 or version != 0 or tables:
            raise ValueError("该文件不是 Story 团数据库。")
    if version > SCHEMA_VERSION:
        raise ValueError("数据库版本较新，请使用更新的 Story 主机。")

    for index in range(version, SCHEMA_VERSION):
        # executescript 会隐式提交，因此逐条执行以保留调用方事务。
        for statement in MIGRATIONS[index]:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {index + 1}")
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
