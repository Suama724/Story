"""频道组、多频道参与关系，以及独立的阅读与发言权限。"""

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from chat_protocol import valid_request_id

from .attributes import has_attributes
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
    attributes_required: bool = False

    def includes(self, sequence: int) -> bool:
        return any(start <= sequence and (end is None or sequence <= end)
                   for start, end in self.history_ranges)


def current_channel(connection: sqlite3.Connection, member_id: str) -> str:
    row = connection.execute(
        """SELECT ch.id FROM channel_memberships AS p JOIN channels AS ch ON ch.id = p.channel_id
           WHERE p.member_id = ? AND p.end_sequence IS NULL AND ch.status = 'active'
           ORDER BY p.id DESC LIMIT 1""", (member_id,),
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
        # 旁观资格自动覆盖本团所有场景（含归档），不随成员列表取得场景发言权。
        return ChannelAccess(True, active and channel["kind"] == "lobby", ((1, None),))
    # 只有本版本的团库含属性表。迁移旧库时，既有消息按旧版本写入规则保留。
    attributes_ready = (
        connection.execute("PRAGMA user_version").fetchone()[0] < 9 or has_attributes(connection, member_id)
    )
    if channel["kind"] == "lobby":
        return ChannelAccess(True, active and attributes_ready, ((1, None),), active and not attributes_ready)
    memberships = connection.execute(
        "SELECT * FROM channel_memberships WHERE member_id = ? AND channel_id = ? ORDER BY id",
        (member_id, channel_id),
    ).fetchall()
    if policy is None:
        mode = connection.execute("SELECT visibility_mode FROM campaigns").fetchone()[0]
        policy = READ_POLICIES[mode]
    ranges = policy.history_ranges(memberships)
    joined = any(row["end_sequence"] is None for row in memberships)
    return ChannelAccess(bool(ranges), active and joined and attributes_ready, ranges,
                         active and joined and not attributes_ready)


def channel_state(connection: sqlite3.Connection, member_id: str) -> dict:
    campaign = campaign_info(connection)
    settings = connection.execute("SELECT visibility_mode, channel_revision FROM campaigns").fetchone()
    is_kp = member_id == campaign["kp_member_id"]
    channels = []
    for row in connection.execute(
        "SELECT id AS channel_id, name, kind, status, group_id, description FROM channels WHERE session_id = ? ORDER BY rowid",
        (campaign["session_id"],),
    ).fetchall():
        access = get_channel_access(connection, member_id, row["channel_id"])
        if access.can_read:
            channel = {**dict(row), **asdict(access)}
            if not is_kp:
                channel["members"] = [dict(item) for item in connection.execute(
                    """SELECT m.role, a.name AS actor_name FROM members m JOIN actors a ON a.member_id = m.id
                       WHERE a.kind IN ('kp', 'pc', 'ob') AND
                       (m.role IN ('kp', 'ob') OR ? = 'lobby' OR EXISTS (
                           SELECT 1 FROM channel_memberships p WHERE p.member_id = m.id AND p.channel_id = ?
                           AND (? = 'archived' OR p.end_sequence IS NULL)))
                       ORDER BY m.role = 'kp' DESC, m.created_at, m.id""",
                    (row["kind"], row["channel_id"], row["status"]),
                )]
            if is_kp:
                channel["member_ids"] = [item[0] for item in connection.execute(
                    """SELECT DISTINCT member_id FROM channel_memberships WHERE channel_id = ?
                       AND (? = 'archived' OR end_sequence IS NULL)""", (row["channel_id"], row["status"]),
                )]
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
    visible_groups = {item["group_id"] for item in channels}
    groups = [dict(row) for row in connection.execute(
        "SELECT id AS group_id, name FROM channel_groups WHERE session_id = ? ORDER BY position, rowid",
        (campaign["session_id"],),
    ) if is_kp or row["group_id"] in visible_groups]
    role = connection.execute("SELECT role FROM members WHERE id = ?", (member_id,)).fetchone()[0]
    default_channel = campaign["lobby_id"] if role == "ob" else current_channel(connection, member_id)
    state = {"channels": channels, "current_channel_id": default_channel,
             "membership_mode": "multiple", "groups": groups,
             "visibility_mode": settings["visibility_mode"], "revision": settings["channel_revision"]}
    if role == "pl":
        state["attributes_imported"] = has_attributes(connection, member_id)
    if is_kp:
        state["members"] = []
        for row in connection.execute("SELECT id FROM members WHERE role IN ('pl', 'ob')").fetchall():
            assigned = current_channel(connection, row["id"])
            name = connection.execute("SELECT name FROM channels WHERE id = ?", (assigned,)).fetchone()[0]
            joined = [item[0] for item in connection.execute(
                """SELECT p.channel_id FROM channel_memberships p JOIN channels ch ON ch.id = p.channel_id
                   WHERE p.member_id = ? AND p.end_sequence IS NULL AND ch.status = 'active' ORDER BY p.id""",
                (row["id"],),
            )]
            state["members"].append({"member_id": row["id"], "channel_id": assigned,
                                     "channel_name": name, "channel_ids": joined})
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


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("名称无效。")
    name = name.strip()
    if not 1 <= len(name) <= 80 or len(name.splitlines()) != 1:
        raise ValueError("名称须为 1–80 个字符的单行文本。")
    return name


def _validate_description(description: str) -> None:
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("频道简介最多 4,000 个字符。")


def _resolve_group(connection: sqlite3.Connection, session_id: str, group_id: str | None) -> str:
    if group_id is None:
        row = connection.execute(
            "SELECT id FROM channel_groups WHERE session_id = ? ORDER BY position, rowid LIMIT 1", (session_id,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT id FROM channel_groups WHERE id = ? AND session_id = ?", (group_id, session_id),
        ).fetchone()
    if row is None:
        raise ValueError("频道组不存在。")
    return row[0]


def create_channel_group(directory: Path, name: str, *, group_id: str | None = None) -> str:
    name = _validate_name(name)
    group_id = group_id or str(uuid4())
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        session_id = campaign_info(connection)["session_id"]
        existing = connection.execute("SELECT name, session_id FROM channel_groups WHERE id = ?", (group_id,)).fetchone()
        if existing is not None:
            if existing["name"] != name or existing["session_id"] != session_id:
                raise ValueError("频道组创建请求冲突。")
            return group_id
        connection.execute(
            """INSERT INTO channel_groups(id, session_id, name, position)
               VALUES (?, ?, ?, (SELECT coalesce(max(position), -1) + 1 FROM channel_groups WHERE session_id = ?))""",
            (group_id, session_id, name, session_id),
        )
        connection.execute("UPDATE campaigns SET channel_revision = channel_revision + 1")
    return group_id


def update_channel(directory: Path, channel_id: str, name: str, group_id: str, description: str,
                   *, expected_revision: int | None = None) -> None:
    name = _validate_name(name)
    _validate_description(description)
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        campaign = campaign_info(connection)
        revision = connection.execute("SELECT channel_revision FROM campaigns").fetchone()[0]
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != revision):
            raise ValueError("频道信息已变更，请重新打开后编辑。")
        group_id = _resolve_group(connection, campaign["session_id"], group_id)
        if connection.execute("SELECT 1 FROM channels WHERE id = ? AND session_id = ?",
                              (channel_id, campaign["session_id"])).fetchone() is None:
            raise ValueError("频道不存在。")
        connection.execute("UPDATE channels SET name = ?, group_id = ?, description = ? WHERE id = ?",
                           (name, group_id, description, channel_id))
        connection.execute("UPDATE campaigns SET channel_revision = channel_revision + 1")


def execute_channel_operation(directory: Path, operation: str, values: dict) -> dict:
    """监听进程与离线界面共用相同的本地管理操作。"""
    if operation == "scene.create":
        create_scene(directory, values.get("name", ""), values.get("member_ids", []),
                     channel_id=values.get("channel_id"), group_id=values.get("group_id"),
                     description=values.get("description", ""))
    elif operation == "scene.archive":
        archive_scene(directory, values.get("channel_id"))
    elif operation == "group.create":
        create_channel_group(directory, values.get("name", ""), group_id=values.get("group_id"))
    elif operation == "channel.update":
        update_channel(directory, values.get("channel_id"), values.get("name", ""), values.get("group_id"),
                       values.get("description", ""), expected_revision=values.get("expected_revision"))
    elif operation == "visibility.set":
        set_visibility_mode(directory, values.get("mode"))
    elif operation == "members.move":
        move_members(directory, values.get("member_ids"), values.get("channel_id"),
                     request_id=values.get("request_id"), expected_revision=values.get("expected_revision"))
    elif operation == "channel.read":
        mark_channel_read(directory, values.get("channel_id"), values.get("sequence"))
    elif operation != "channels":
        raise ValueError("未知频道操作。")
    return local_channel_state(directory)


def create_scene(directory: Path, name: str, member_ids: list[str], *, channel_id: str | None = None,
                 group_id: str | None = None, description: str = "") -> str:
    name = _validate_name(name)
    name.encode("utf-8")
    if not isinstance(member_ids, list) or any(not isinstance(item, str) for item in member_ids):
        raise ValueError("请选择有效的玩家角色。")
    channel_id = channel_id or str(uuid4())
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        campaign = campaign_info(connection)
        group_id = _resolve_group(connection, campaign["session_id"], group_id)
        _validate_description(description)
        existing = connection.execute("SELECT name, kind, group_id, description FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if existing is not None:
            participants = {row[0] for row in connection.execute(
                "SELECT member_id FROM channel_memberships WHERE channel_id = ?", (channel_id,),
            )}
            if (existing["kind"] != "scene" or existing["name"] != name or participants != set(member_ids)
                    or existing["group_id"] != group_id or existing["description"] != description):
                raise ValueError("场景创建请求冲突。")
            return channel_id
        for member_id in set(member_ids):
            member = connection.execute(
                "SELECT 1 FROM members WHERE id = ? AND role IN ('pl', 'ob') AND campaign_id = ?",
                (member_id, campaign["campaign_id"]),
            ).fetchone()
            if member is None:
                raise ValueError("所选玩家角色不存在。")
        connection.execute(
            """INSERT INTO channels (id, session_id, name, kind, status, created_at, group_id, description)
               VALUES (?, ?, ?, 'scene', 'active', ?, ?, ?)""",
            (channel_id, campaign["session_id"], name, datetime.now(timezone.utc).isoformat(), group_id, description),
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
