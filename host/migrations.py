"""团数据库的顺序迁移，调用方负责事务。"""

import sqlite3

APPLICATION_ID = 0x53545259  # STRY
SCHEMA_VERSION = 4


def _unique_character_names(connection: sqlite3.Connection) -> None:
    duplicates = connection.execute(
        """SELECT id, name FROM (
               SELECT id, name, row_number() OVER (
                   PARTITION BY name COLLATE NOCASE ORDER BY kind = 'kp' DESC, rowid
               ) AS position
               FROM actors WHERE kind IN ('kp', 'pc')
           ) WHERE position > 1"""
    ).fetchall()
    for actor_id, name in duplicates:
        number = 2
        while True:
            suffix = f"（{number}）"
            candidate = name[:80 - len(suffix)] + suffix
            exists = connection.execute(
                "SELECT 1 FROM actors WHERE kind IN ('kp', 'pc') AND name = ? COLLATE NOCASE",
                (candidate,),
            ).fetchone()
            if exists is None:
                break
            number += 1
        connection.execute("UPDATE actors SET name = ? WHERE id = ?", (candidate, actor_id))

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
    (
        """CREATE TABLE credentials (
            member_id TEXT PRIMARY KEY NOT NULL REFERENCES members(id),
            password_hash TEXT NOT NULL,
            must_change_password INTEGER NOT NULL CHECK (must_change_password IN (0, 1)),
            initial_expires_at REAL,
            CHECK ((must_change_password = 1 AND initial_expires_at IS NOT NULL)
                OR (must_change_password = 0 AND initial_expires_at IS NULL))
        )""",
        """CREATE TABLE auth_sessions (
            token_hash TEXT PRIMARY KEY NOT NULL,
            member_id TEXT NOT NULL UNIQUE REFERENCES credentials(member_id),
            expires_at REAL NOT NULL
        )""",
    ),
    (
        "ALTER TABLE campaigns ADD COLUMN description TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE auth_sessions RENAME TO legacy_auth_sessions",
        "ALTER TABLE credentials RENAME TO legacy_credentials",
        """CREATE TABLE credentials (
            member_id TEXT PRIMARY KEY NOT NULL REFERENCES members(id),
            password TEXT,
            password_hash TEXT,
            CHECK ((password IS NULL) != (password_hash IS NULL))
        )""",
        """INSERT INTO credentials (member_id, password_hash)
           SELECT member_id, password_hash FROM legacy_credentials WHERE must_change_password = 0""",
        """CREATE TABLE auth_sessions (
            token_hash TEXT PRIMARY KEY NOT NULL,
            member_id TEXT NOT NULL UNIQUE REFERENCES members(id),
            expires_at REAL NOT NULL
        )""",
        """INSERT INTO auth_sessions
           SELECT s.token_hash, s.member_id, s.expires_at FROM legacy_auth_sessions AS s
           JOIN credentials AS c ON c.member_id = s.member_id
           JOIN members AS m ON m.id = s.member_id AND m.role = 'pl'""",
        "DROP TABLE legacy_auth_sessions",
        "DROP TABLE legacy_credentials",
        _unique_character_names,
        """CREATE UNIQUE INDEX unique_character_name ON actors(name COLLATE NOCASE)
           WHERE kind IN ('kp', 'pc')""",
    ),
    (
        """CREATE TABLE messages (
            message_id TEXT PRIMARY KEY NOT NULL,
            channel_id TEXT NOT NULL REFERENCES channels(id),
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            member_id TEXT NOT NULL REFERENCES members(id),
            actor_id TEXT NOT NULL REFERENCES actors(id),
            nickname TEXT NOT NULL,
            request_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (channel_id, sequence),
            UNIQUE (member_id, request_id)
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
        for step in MIGRATIONS[index]:
            if isinstance(step, str):
                connection.execute(step)
            else:
                step(connection)
        connection.execute(f"PRAGMA user_version = {index + 1}")
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
