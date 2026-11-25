"""团数据库的顺序迁移，调用方负责事务。"""

import sqlite3

APPLICATION_ID = 0x53545259  # STRY
DICE_SCHEMA_VERSION = 10
SCHEMA_VERSION = DICE_SCHEMA_VERSION


def _preserve_identity_triggers(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TEMP TABLE identity_triggers_v7 AS SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name IN ('members', 'actors')")


def _finish_identity_rebuild(connection: sqlite3.Connection) -> None:
    for row in connection.execute("SELECT sql FROM identity_triggers_v7").fetchall():
        connection.execute(row[0])
    connection.execute("DROP TABLE identity_triggers_v7")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise sqlite3.IntegrityError("身份迁移后的外键检查失败。")
    # SQLite 的延迟 DROP 检查仍记录原表的删除；完整检查通过后恢复即时约束。
    connection.execute("PRAGMA defer_foreign_keys = OFF")


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
    (
        "ALTER TABLE campaigns ADD COLUMN visibility_mode TEXT NOT NULL DEFAULT 'secret' CHECK (visibility_mode IN ('secret', 'broad'))",
        "ALTER TABLE campaigns ADD COLUMN channel_revision INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE channel_memberships (
            id INTEGER PRIMARY KEY,
            member_id TEXT NOT NULL REFERENCES members(id),
            channel_id TEXT NOT NULL REFERENCES channels(id),
            start_sequence INTEGER NOT NULL CHECK (start_sequence > 0),
            end_sequence INTEGER CHECK (end_sequence >= 0 AND end_sequence >= start_sequence - 1)
        )""",
        """CREATE UNIQUE INDEX one_active_scene_per_member ON channel_memberships(member_id)
           WHERE end_sequence IS NULL""",
        "CREATE INDEX channel_participants ON channel_memberships(channel_id, member_id)",
    ),
    (
        """CREATE TABLE channel_read_positions (
            member_id TEXT NOT NULL REFERENCES members(id),
            channel_id TEXT NOT NULL REFERENCES channels(id),
            sequence INTEGER NOT NULL CHECK (sequence >= 0),
            PRIMARY KEY (member_id, channel_id)
        )""",
        """CREATE TABLE channel_operations (
            request_id TEXT PRIMARY KEY NOT NULL,
            payload TEXT NOT NULL
        )""",
    ),
    (
        # 延迟检查引用，重建 CHECK 约束时保留所有既有 ID 和外键。
        _preserve_identity_triggers,
        "PRAGMA defer_foreign_keys = ON",
        """CREATE TABLE members_v7 (
            id TEXT PRIMARY KEY NOT NULL,
            campaign_id TEXT NOT NULL REFERENCES campaigns(id),
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            role TEXT NOT NULL CHECK (role IN ('kp', 'pl', 'ob')),
            created_at TEXT NOT NULL
        )""",
        "INSERT INTO members_v7 SELECT * FROM members",
        "DROP TABLE members",
        "ALTER TABLE members_v7 RENAME TO members",
        "CREATE UNIQUE INDEX one_kp_per_campaign ON members(campaign_id) WHERE role = 'kp'",
        """CREATE TABLE actors_v7 (
            id TEXT PRIMARY KEY NOT NULL,
            member_id TEXT NOT NULL REFERENCES members(id),
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            kind TEXT NOT NULL CHECK (kind IN ('kp', 'pc', 'npc', 'ob')),
            created_at TEXT NOT NULL
        )""",
        "INSERT INTO actors_v7 SELECT * FROM actors",
        "DROP TABLE actors",
        "ALTER TABLE actors_v7 RENAME TO actors",
        """CREATE UNIQUE INDEX unique_character_name ON actors(name COLLATE NOCASE)
           WHERE kind IN ('kp', 'pc', 'ob')""",
        _finish_identity_rebuild,
    ),
    (
        "DROP INDEX one_active_scene_per_member",
        """CREATE UNIQUE INDEX one_active_membership_per_channel
           ON channel_memberships(member_id, channel_id) WHERE end_sequence IS NULL""",
        """CREATE TABLE channel_groups (
            id TEXT PRIMARY KEY NOT NULL,
            session_id TEXT NOT NULL REFERENCES play_sessions(id),
            name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
            position INTEGER NOT NULL DEFAULT 0
        )""",
        "ALTER TABLE channels ADD COLUMN group_id TEXT REFERENCES channel_groups(id)",
        "ALTER TABLE channels ADD COLUMN description TEXT NOT NULL DEFAULT ''",
        """INSERT INTO channel_groups(id, session_id, name)
           SELECT 'default-' || id, id, '频道' FROM play_sessions""",
        "UPDATE channels SET group_id = 'default-' || session_id",
    ),
    (
        """CREATE TABLE actor_attributes (
            actor_id TEXT PRIMARY KEY NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
            ruleset TEXT NOT NULL,
            values_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )""",
    ),
    (
        "ALTER TABLE campaigns ADD COLUMN dice_config TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE messages ADD COLUMN local_sequence INTEGER",
        "UPDATE messages SET local_sequence = sequence",
        "CREATE UNIQUE INDEX messages_local_order ON messages(channel_id, local_sequence)",
        """CREATE TABLE private_dice_messages (
            message_id TEXT PRIMARY KEY NOT NULL,
            channel_id TEXT NOT NULL REFERENCES channels(id),
            local_sequence INTEGER NOT NULL,
            member_id TEXT NOT NULL REFERENCES members(id),
            actor_id TEXT NOT NULL REFERENCES actors(id),
            nickname TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (channel_id, local_sequence)
        )""",
        """CREATE TABLE dice_invocations (
            member_id TEXT NOT NULL REFERENCES members(id),
            request_id TEXT NOT NULL,
            channel_id TEXT NOT NULL REFERENCES channels(id),
            command_text TEXT NOT NULL,
            private INTEGER NOT NULL CHECK (private IN (0, 1)),
            input_id TEXT NOT NULL,
            result_id TEXT NOT NULL,
            roll_data TEXT NOT NULL,
            PRIMARY KEY (member_id, request_id)
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
