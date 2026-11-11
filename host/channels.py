"""场景参与关系，以及可替换的阅读策略和独立发言权限。"""

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .auth import AuthError, session_for_token
from .storage import campaign_info, open_campaign

HistoryRange = tuple[int, int | None]


class ReadPolicy(Protocol):
    def history_ranges(self, memberships: list[sqlite3.Row]) -> tuple[HistoryRange, ...]: ...


class SecretReadPolicy:
    def history_ranges(self, memberships: list[sqlite3.Row]) -> tuple[HistoryRange, ...]:
        return tuple((row["start_sequence"], row["end_sequence"]) for row in memberships)


class BroadReadPolicy:
    def history_ranges(self, memberships: list[sqlite3.Row]) -> tuple[HistoryRange, ...]:
        return ((1, None),)


READ_POLICIES: dict[str, ReadPolicy] = {"secret": SecretReadPolicy(), "broad": BroadReadPolicy()}


@dataclass(frozen=True)
class ChannelAccess:
    can_read: bool
    can_send: bool
    history_ranges: tuple[HistoryRange, ...]

    def includes(self, sequence: int) -> bool:
        return any(start <= sequence and (end is None or sequence <= end)
                   for start, end in self.history_ranges)


def current_channel(connection: sqlite3.Connection, member_id: str) -> str:
    row = connection.execute(
        """SELECT ch.id FROM channel_memberships AS p JOIN channels AS ch ON ch.id = p.channel_id
           WHERE p.member_id = ? AND p.end_sequence IS NULL AND ch.status = 'active'""", (member_id,),
    ).fetchone()
    return row[0] if row is not None else campaign_info(connection)["lobby_id"]


def get_channel_access(
    connection: sqlite3.Connection, member_id: str, channel_id: str, *, policy: ReadPolicy | None = None,
) -> ChannelAccess:
    channel = connection.execute(
        """SELECT ch.*, s.campaign_id FROM channels AS ch
           JOIN play_sessions AS s ON s.id = ch.session_id WHERE ch.id = ?""", (channel_id,),
    ).fetchone()
    member = connection.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if channel is None or member is None or member["campaign_id"] != channel["campaign_id"]:
        return ChannelAccess(False, False, ())
    active = channel["status"] == "active"
    if member["role"] == "kp":
        return ChannelAccess(True, active, ((1, None),))
    assigned = current_channel(connection, member_id)
    if channel["kind"] == "lobby":
        return ChannelAccess(True, active and assigned == channel_id, ((1, None),))
    memberships = connection.execute(
        "SELECT * FROM channel_memberships WHERE member_id = ? AND channel_id = ? ORDER BY id",
        (member_id, channel_id),
    ).fetchall()
    if policy is None:
        mode = connection.execute("SELECT visibility_mode FROM campaigns").fetchone()[0]
        policy = READ_POLICIES[mode]
    ranges = policy.history_ranges(memberships)
    return ChannelAccess(bool(ranges), active and assigned == channel_id, ranges)


def channel_state(connection: sqlite3.Connection, member_id: str) -> dict:
    campaign = campaign_info(connection)
    settings = connection.execute("SELECT visibility_mode, channel_revision FROM campaigns").fetchone()
    channels = []
    for row in connection.execute(
        "SELECT id AS channel_id, name, kind, status FROM channels WHERE session_id = ? ORDER BY rowid",
        (campaign["session_id"],),
    ).fetchall():
        access = get_channel_access(connection, member_id, row["channel_id"])
        if access.can_read:
            channels.append({**dict(row), **asdict(access)})
    return {"channels": channels, "current_channel_id": current_channel(connection, member_id),
            "visibility_mode": settings["visibility_mode"], "revision": settings["channel_revision"]}


def player_channel_state(directory: Path, token: str) -> dict:
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN")
        session = session_for_token(connection, token)
        return channel_state(connection, session.member_id)


def local_channel_state(directory: Path) -> dict:
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN")
        return channel_state(connection, campaign_info(connection)["kp_member_id"])


def create_scene(directory: Path, name: str, member_ids: list[str], *, channel_id: str | None = None) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 80 or len(name.splitlines()) != 1:
        raise ValueError("场景名须为 1–80 个字符的单行文本。")
    name.encode("utf-8")
    if not isinstance(member_ids, list) or any(not isinstance(item, str) for item in member_ids):
        raise ValueError("请选择有效的玩家角色。")
    channel_id = channel_id or str(uuid4())
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        campaign = campaign_info(connection)
        existing = connection.execute("SELECT name, kind FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if existing is not None:
            participants = {row[0] for row in connection.execute(
                "SELECT member_id FROM channel_memberships WHERE channel_id = ?", (channel_id,),
            )}
            if existing["kind"] != "scene" or existing["name"] != name or participants != set(member_ids):
                raise ValueError("场景创建请求冲突。")
            return channel_id
        for member_id in set(member_ids):
            member = connection.execute(
                "SELECT 1 FROM members WHERE id = ? AND role = 'pl' AND campaign_id = ?",
                (member_id, campaign["campaign_id"]),
            ).fetchone()
            if member is None:
                raise ValueError("所选玩家角色不存在。")
            if current_channel(connection, member_id) != campaign["lobby_id"]:
                raise ValueError("所选角色已在另一场景，请先结束该场景。")
        connection.execute(
            "INSERT INTO channels (id, session_id, name, kind, status, created_at) VALUES (?, ?, ?, 'scene', 'active', ?)",
            (channel_id, campaign["session_id"], name, datetime.now(timezone.utc).isoformat()),
        )
        connection.executemany(
            "INSERT INTO channel_memberships (member_id, channel_id, start_sequence) VALUES (?, ?, 1)",
            ((member_id, channel_id) for member_id in set(member_ids)),
        )
        connection.execute("UPDATE campaigns SET channel_revision = channel_revision + 1")
    return channel_id


def archive_scene(directory: Path, channel_id: str) -> None:
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        channel = connection.execute("SELECT kind, status FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if channel is None or channel["kind"] != "scene":
            raise ValueError("场景不存在。")
        if channel["status"] == "archived":
            return
        connection.execute(
            """UPDATE channel_memberships SET end_sequence =
               (SELECT coalesce(max(sequence), 0) FROM messages WHERE channel_id = ?)
               WHERE channel_id = ? AND end_sequence IS NULL""", (channel_id, channel_id),
        )
        connection.execute("UPDATE channels SET status = 'archived' WHERE id = ?", (channel_id,))
        connection.execute("UPDATE campaigns SET channel_revision = channel_revision + 1")


def set_visibility_mode(directory: Path, mode: str) -> None:
    if mode not in READ_POLICIES:
        raise ValueError("阅读模式无效。")
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE campaigns SET visibility_mode = ?, channel_revision = channel_revision + 1
               WHERE visibility_mode != ?""", (mode, mode),
        )
