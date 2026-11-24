"""团频道的授权历史查询、消息写入与幂等确认。"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from chat_protocol import CHAT_MESSAGE, HISTORY_PAGE_SIZE, SEND_ERRORS, chat_text_error, valid_request_id

from .auth import session_for_token
from .channels import get_channel_access
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
        channel_id = channel_id or campaign_info(connection)["lobby_id"]
        return _channel_page(connection, channel_id, "latest" if before is None else "before", before or 0,
                             ((1, None),))


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


def save_lobby_message(
    directory: Path, request_id: object, text: object, *, token: str | None, channel_id: str | None = None,
) -> tuple[dict[str, object], bool]:
    """返回已提交的消息及其是否为首次写入。无令牌表示本地主持操作。"""
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
            else:
                session = session_for_token(connection, token)
                member_id, actor_id, nickname = session.member_id, session.actor_id, session.actor_name

            channel_id = channel_id or campaign["lobby_id"]
            access = get_channel_access(connection, member_id, channel_id)
            existing = connection.execute(
                "SELECT * FROM messages WHERE member_id = ? AND request_id = ?", (member_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["text"] != text or existing["channel_id"] != channel_id:
                    raise ChatSendError("request_id_conflict")
                if not access.includes(existing["sequence"]):
                    raise ChatSendError("channel_forbidden")
                return dict(existing), False

            if not access.can_send:
                raise ChatSendError("attributes_required" if access.attributes_required else "channel_forbidden")

            row = connection.execute(
                """INSERT INTO messages
                   (message_id, channel_id, sequence, member_id, actor_id, nickname, request_id, kind, text, created_at)
                   VALUES (?, ?, (SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE channel_id = ?),
                           ?, ?, ?, ?, 'text', ?, ?)
                   RETURNING *""",
                (str(uuid4()), channel_id, channel_id, member_id, actor_id, nickname, request_id, text,
                 datetime.now(timezone.utc).isoformat()),
            ).fetchone()
            return dict(row), True
    except sqlite3.Error as error:
        raise ChatSendError("storage_error") from error
