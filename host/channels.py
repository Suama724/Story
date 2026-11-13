"""场景参与关系，以及可替换的阅读策略和独立发言权限。"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from chat_protocol import valid_request_id

from .auth import session_for_token
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
    if member["role"] == "ob":
        # 旁观资格自动覆盖本团所有场景（含归档），不参与 PL 的独占分组。
        return ChannelAccess(True, active and channel["kind"] == "lobby", ((1, None),))
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
    is_kp = member_id == campaign["kp_member_id"]
    channels = []
    for row in connection.execute(
        "SELECT id AS channel_id, name, kind, status FROM channels WHERE session_id = ? ORDER BY rowid",
        (campaign["session_id"],),
    ).fetchall():
        access = get_channel_access(connection, member_id, row["channel_id"])
        if access.can_read:
            channel = {**dict(row), **asdict(access)}
            if is_kp:
                read = connection.execute(
                    "SELECT sequence FROM channel_read_positions WHERE member_id = ? AND channel_id = ?",
                    (member_id, row["channel_id"]),
                ).fetchone()
                channel["read_sequence"] = read[0] if read is not None else 0
                channel["unread_count"] = connection.execute(
                    """SELECT count(*) FROM messages
                       WHERE channel_id = ? AND sequence > ? AND member_id != ?""",
                    (row["channel_id"], channel["read_sequence"], member_id),
                ).fetchone()[0]
            channels.append(channel)
    state = {"channels": channels, "current_channel_id": current_channel(connection, member_id),
             "visibility_mode": settings["visibility_mode"], "revision": settings["channel_revision"]}
    if is_kp:
        state["members"] = []
        for row in connection.execute("SELECT id FROM members WHERE role = 'pl'").fetchall():
            assigned = current_channel(connection, row["id"])
            name = connection.execute("SELECT name FROM channels WHERE id = ?", (assigned,)).fetchone()[0]
            state["members"].append({"member_id": row["id"], "channel_id": assigned, "channel_name": name})
    return state


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


def move_members(
    directory: Path, member_ids: list[str], channel_id: str, *, request_id: str | None = None,
    expected_revision: int | None = None,
) -> None:
    """事务内结束旧参与区间并开启新区间。同一请求重试不会再次移动角色。"""
    if (not isinstance(member_ids, list) or not member_ids
            or any(not isinstance(item, str) for item in member_ids)):
        raise ValueError("请至少选择一名有效的玩家角色。")
    if not isinstance(channel_id, str):
        raise ValueError("请选择目标频道。")
    request_id = str(uuid4()) if request_id is None else request_id
    if not valid_request_id(request_id):
        raise ValueError("分组请求 ID 无效。")
    members = sorted(set(member_ids))
    payload = json.dumps({"member_ids": members, "channel_id": channel_id}, sort_keys=True)
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT payload FROM channel_operations WHERE request_id = ?", (request_id,),
        ).fetchone()
        if existing is not None:
            if existing[0] != payload:
                raise ValueError("分组请求 ID 已用于另一项操作。")
            return
        campaign = campaign_info(connection)
        revision = connection.execute("SELECT channel_revision FROM campaigns").fetchone()[0]
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != revision):
            raise ValueError("分组状态已变更，请刷新后重新调整。")
        target = connection.execute(
            "SELECT kind FROM channels WHERE id = ? AND session_id = ? AND status = 'active'",
            (channel_id, campaign["session_id"]),
        ).fetchone()
        if target is None:
            raise ValueError("目标频道不存在或已归档。")
        for member_id in members:
            if connection.execute(
                "SELECT 1 FROM members WHERE id = ? AND campaign_id = ? AND role = 'pl'",
                (member_id, campaign["campaign_id"]),
            ).fetchone() is None:
                raise ValueError("所选玩家角色不存在。")
        next_sequence = connection.execute(
            "SELECT coalesce(max(sequence), 0) + 1 FROM messages WHERE channel_id = ?", (channel_id,),
        ).fetchone()[0]
        changed = False
        for member_id in members:
            if current_channel(connection, member_id) == channel_id:
                continue
            connection.execute(
                """UPDATE channel_memberships SET end_sequence =
                   (SELECT coalesce(max(sequence), 0) FROM messages WHERE channel_id = channel_memberships.channel_id)
                   WHERE member_id = ? AND end_sequence IS NULL""", (member_id,),
            )
            if target["kind"] == "scene":
                connection.execute(
                    "INSERT INTO channel_memberships(member_id, channel_id, start_sequence) VALUES (?, ?, ?)",
                    (member_id, channel_id, next_sequence),
                )
            changed = True
        if changed:
            connection.execute("UPDATE campaigns SET channel_revision = channel_revision + 1")
        connection.execute("INSERT INTO channel_operations VALUES (?, ?)", (request_id, payload))


def mark_channel_read(directory: Path, channel_id: str, sequence: int) -> None:
    """记录 KP 已实际显示的消息位置，迟到的标记只能向前推进。"""
    if type(sequence) is not int or not 0 <= sequence < 2**63:
        raise ValueError("已读位置无效。")
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        member_id = campaign_info(connection)["kp_member_id"]
        if not get_channel_access(connection, member_id, channel_id).can_read:
            raise ValueError("频道不存在。")
        latest = connection.execute(
            "SELECT coalesce(max(sequence), 0) FROM messages WHERE channel_id = ?", (channel_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO channel_read_positions VALUES (?, ?, ?)
               ON CONFLICT(member_id, channel_id) DO UPDATE SET sequence = max(sequence, excluded.sequence)""",
            (member_id, channel_id, min(sequence, latest)),
        )


def set_visibility_mode(directory: Path, mode: str) -> None:
    if mode not in READ_POLICIES:
        raise ValueError("阅读模式无效。")
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE campaigns SET visibility_mode = ?, channel_revision = channel_revision + 1
               WHERE visibility_mode != ?""", (mode, mode),
        )
