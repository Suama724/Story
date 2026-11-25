"""团频道的授权历史查询、消息写入与幂等确认。"""

import sqlite3
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from chat_protocol import CHAT_MESSAGE, HISTORY_PAGE_SIZE, SEND_ERRORS, chat_text_error, valid_request_id
from dice_component import DiceError, parse_command, roll

from .auth import session_for_token
from .channels import get_channel_access
from .dice import check_input, read_dice_config
from .migrations import DICE_SCHEMA_VERSION
from .storage import campaign_info, open_campaign


def lobby_history(directory: Path, token: str, mode: str, cursor: int,
                  channel_id: str | None = None) -> dict[str, object]:
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN")
        session = session_for_token(connection, token)
        channel_id = channel_id or campaign_info(connection)["lobby_id"]
        access = get_channel_access(connection, session.member_id, channel_id)
        if not access.can_read:
            raise ChatSendError("channel_read_forbidden")
        return _channel_page(connection, channel_id, mode, cursor, access.history_ranges)


def local_lobby_history(directory: Path, *, before: int | None = None,
                        channel_id: str | None = None) -> dict[str, object]:
    """组织者工作台直接读取本地团库，监听关闭时也可翻页。"""
    with open_campaign(directory) as connection:
        campaign = campaign_info(connection)
        channel_id = channel_id or campaign["lobby_id"]
        mode = "latest" if before is None else "before"
        if campaign["schema_version"] < DICE_SCHEMA_VERSION:
            return _channel_page(connection, channel_id, mode, before or 0, ((1, None),))
        rows = connection.execute(
            """WITH timeline AS (
                SELECT message_id, channel_id, COALESCE(local_sequence, sequence) AS sequence,
                       sequence AS public_sequence, member_id, actor_id, nickname, kind, text, created_at
                FROM messages WHERE channel_id = ?
                UNION ALL
                SELECT message_id, channel_id, local_sequence, NULL, member_id, actor_id, nickname, kind, text, created_at
                FROM private_dice_messages WHERE channel_id = ? AND member_id = ?
            )
            SELECT * FROM timeline WHERE (? IS NULL OR sequence < ?)
            ORDER BY sequence DESC LIMIT ?""",
            (channel_id, channel_id, campaign["kp_member_id"], before, before, HISTORY_PAGE_SIZE + 1),
        ).fetchall()
        return {"messages": [{"type": CHAT_MESSAGE, **dict(row)} for row in reversed(rows[:HISTORY_PAGE_SIZE])],
                "has_more": len(rows) > HISTORY_PAGE_SIZE}


def _channel_page(connection: sqlite3.Connection, channel_id: str, mode: str, cursor: int,
                  ranges: tuple[tuple[int, int | None], ...]) -> dict[str, object]:
    clauses = []
    bounds = []
    for start, end in ranges:
        clauses.append("(sequence >= ? AND sequence <= ?)")
        bounds.extend((start, end if end is not None else 2**63 - 1))
    visible = " OR ".join(clauses) or "0"
    if mode == "after":
        selection, values = "AND sequence > ? ORDER BY sequence", (cursor,)
    elif mode == "before":
        selection, values = "AND sequence < ? ORDER BY sequence DESC", (cursor,)
    else:
        selection, values = "ORDER BY sequence DESC", ()
    rows = connection.execute(
        f"""SELECT message_id, channel_id, sequence, member_id, actor_id, nickname, kind, text, created_at
            FROM messages WHERE channel_id = ? AND ({visible}) {selection} LIMIT ?""",
        (channel_id, *bounds, *values, HISTORY_PAGE_SIZE + 1),
    ).fetchall()
    page = rows[:HISTORY_PAGE_SIZE]
    if mode != "after":
        page.reverse()
    return {"messages": [{"type": CHAT_MESSAGE, **dict(row)} for row in page],
            "has_more": len(rows) > HISTORY_PAGE_SIZE}


class ChatSendError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(SEND_ERRORS[code])
        self.code = code


@dataclass(frozen=True)
class ChatSubmission:
    message: dict
    created: bool
    result: dict | None = None
    private: bool = False


def save_lobby_message(
    directory: Path, request_id: object, text: object, *, token: str | None, channel_id: str | None = None,
) -> tuple[dict[str, object], bool]:
    """返回已提交的消息及其是否为首次写入。无令牌表示本地主持操作。"""
    submission = submit_lobby_message(directory, request_id, text, token=token, channel_id=channel_id, enable_dice=False)
    return submission.message, submission.created


def submit_lobby_message(
    directory: Path, request_id: object, text: object, *, token: str | None,
    channel_id: str | None = None, enable_dice: bool = True,
) -> ChatSubmission:
    if not valid_request_id(request_id):
        raise ChatSendError("invalid_request_id")
    error = chat_text_error(text)
    if error is not None:
        raise ChatSendError(error)

    try:
        with open_campaign(directory) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            campaign = campaign_info(connection)
            if token is None:
                member_id = campaign["kp_member_id"]
                actor_id = campaign["kp_actor_id"]
                nickname = connection.execute("SELECT name FROM actors WHERE id = ?", (actor_id,)).fetchone()[0]
                role = "kp"
            else:
                session = session_for_token(connection, token)
                member_id, actor_id, nickname = session.member_id, session.actor_id, session.actor_name
                role = session.role

            channel_id = channel_id or campaign["lobby_id"]
            access = get_channel_access(connection, member_id, channel_id)
            dice_ready = campaign["schema_version"] >= DICE_SCHEMA_VERSION
            if dice_ready:
                previous = _replay_dice(connection, member_id, request_id, channel_id, text, access, role)
                if previous is not None:
                    return ChatSubmission(*previous)
            existing = connection.execute(
                "SELECT * FROM messages WHERE member_id = ? AND request_id = ?", (member_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["text"] != text or existing["channel_id"] != channel_id:
                    raise ChatSendError("request_id_conflict")
                if not access.includes(existing["sequence"]):
                    raise ChatSendError("channel_forbidden")
                return ChatSubmission(dict(existing), False)

            if not access.can_send:
                raise ChatSendError("attributes_required" if access.attributes_required else "channel_forbidden")

            command = parse_command(text) if enable_dice and dice_ready and campaign["ruleset"] == "coc7" else None
            if command is None:
                message = _insert_public(connection, channel_id, member_id, actor_id, nickname, request_id, text,
                                         local_order=dice_ready)
                return ChatSubmission(message, True)
            if command.secret and role != "kp":
                raise ChatSendError("dice_secret_forbidden")
            if role not in ("kp", "pl"):
                raise ChatSendError("dice_role_forbidden")
            config = read_dice_config(connection)
            check = check_input(connection, command, actor_id=actor_id, actor_name=nickname,
                                organizer=role == "kp") if command.check else None
            outcome = roll(command, config, check)
            bot_actor_id = "dice-" + campaign["campaign_id"]
            bot_owner = campaign["kp_member_id"]
            connection.execute(
                "INSERT OR IGNORE INTO actors (id, member_id, name, kind, created_at) VALUES (?, ?, '骰子', 'npc', ?)",
                (bot_actor_id, bot_owner, _now()),
            )
            result_text = outcome.text if check else f"{nickname}掷骰：{outcome.text}"
            if command.secret:
                message = _insert_private(connection, channel_id, member_id, actor_id, nickname, text, "dice_command")
                result = _insert_private(connection, channel_id, member_id, bot_actor_id, "骰子", result_text, "dice_result")
            else:
                message = _insert_public(connection, channel_id, member_id, actor_id, nickname, request_id, text,
                                         local_order=True)
                result = _insert_public(connection, channel_id, bot_owner, bot_actor_id, "骰子",
                                        "dice-" + uuid4().hex, result_text, local_order=True, kind="dice_result")
            connection.execute(
                """INSERT INTO dice_invocations VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (member_id, request_id, channel_id, text, int(command.secret), message["message_id"],
                 result["message_id"], json.dumps({"roll": outcome.details, "config": asdict(config)}, ensure_ascii=False)),
            )
            return ChatSubmission(message, True, result, command.secret)
    except DiceError as error:
        raise ChatSendError(error.code) from error
    except sqlite3.Error as error:
        raise ChatSendError("storage_error") from error


def _replay_dice(connection, member_id, request_id, channel_id, text, access, role):
    row = connection.execute(
        "SELECT * FROM dice_invocations WHERE member_id = ? AND request_id = ?", (member_id, request_id),
    ).fetchone()
    if row is None:
        return None
    if row["command_text"] != text or row["channel_id"] != channel_id:
        raise ChatSendError("request_id_conflict")
    private = bool(row["private"])
    if private:
        if role != "kp" or not access.can_read:
            raise ChatSendError("channel_forbidden")
        query = "SELECT * FROM private_dice_messages WHERE message_id = ? AND member_id = ?"
        args = (member_id,)
    else:
        query = "SELECT * FROM messages WHERE message_id = ?"
        args = ()
    message = connection.execute(query, (row["input_id"], *args)).fetchone()
    result = connection.execute(query, (row["result_id"], *args)).fetchone()
    if message is None or result is None:
        raise ChatSendError("storage_error")
    if not private and not (access.includes(message["sequence"]) and access.includes(result["sequence"])):
        raise ChatSendError("channel_forbidden")
    return dict(message), False, dict(result), private


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_local_sequence(connection: sqlite3.Connection, channel_id: str) -> int:
    return connection.execute(
        """SELECT MAX(COALESCE((SELECT MAX(local_sequence) FROM messages WHERE channel_id = ?), 0),
                      COALESCE((SELECT MAX(local_sequence) FROM private_dice_messages WHERE channel_id = ?), 0)) + 1""",
        (channel_id, channel_id),
    ).fetchone()[0]


def _insert_public(connection, channel_id, member_id, actor_id, nickname, request_id, text, *,
                   local_order, kind="text") -> dict:
    columns = ", local_sequence" if local_order else ""
    placeholder = ", ?" if local_order else ""
    local_sequence = (_next_local_sequence(connection, channel_id),) if local_order else ()
    row = connection.execute(
        f"""INSERT INTO messages
            (message_id, channel_id, sequence, member_id, actor_id, nickname, request_id, kind, text, created_at{columns})
            VALUES (?, ?, (SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE channel_id = ?),
                    ?, ?, ?, ?, ?, ?, ?{placeholder}) RETURNING *""",
        (str(uuid4()), channel_id, channel_id, member_id, actor_id, nickname, request_id, kind, text,
         _now(), *local_sequence),
    ).fetchone()
    return dict(row)


def _insert_private(connection, channel_id, member_id, actor_id, nickname, text, kind) -> dict:
    return dict(connection.execute(
        """INSERT INTO private_dice_messages
           (message_id, channel_id, local_sequence, member_id, actor_id, nickname, kind, text, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *""",
        (str(uuid4()), channel_id, _next_local_sequence(connection, channel_id), member_id, actor_id,
         nickname, kind, "【暗骰】" + text, _now()),
    ).fetchone())


def chat_event(record: dict, *, local: bool = False) -> dict:
    event = {"type": CHAT_MESSAGE, **record}
    event.pop("request_id", None)
    local_sequence = event.pop("local_sequence", None)
    if local and local_sequence is not None and local_sequence != event.get("sequence"):
        event["public_sequence"] = event.get("sequence")
        event["sequence"] = local_sequence
    return event
