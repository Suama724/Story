"""属性独立于登录身份，由参与者首次导入、由本地主持重置。"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from coc7_attributes import effective_attributes, parse_attributes
from .auth import AuthError, session_for_token
from .storage import open_campaign


def has_attributes(connection: sqlite3.Connection, member_id: str) -> bool:
    return connection.execute(
        """SELECT 1 FROM actor_attributes AS attrs
           JOIN actors AS a ON a.id = attrs.actor_id
           JOIN members AS m ON m.id = a.member_id
           JOIN campaigns AS c ON c.id = m.campaign_id AND c.ruleset = attrs.ruleset
           WHERE m.id = ? AND m.role = 'pl' AND a.kind = 'pc'""", (member_id,),
    ).fetchone() is not None


def _read(connection: sqlite3.Connection, actor_id: str, ruleset: str) -> dict | None:
    row = connection.execute(
        "SELECT values_json FROM actor_attributes WHERE actor_id = ? AND ruleset = ?", (actor_id, ruleset),
    ).fetchone()
    return effective_attributes(json.loads(row["values_json"])) if row else None


def player_attributes(directory: Path, token: str, *, text: str | None = None) -> dict:
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE" if text is not None else "BEGIN")
        session = session_for_token(connection, token)
        if session.role != "pl":
            raise AuthError("attributes_forbidden", "只有 PL 可以导入或查看自己的角色属性。", 403)
        ruleset = connection.execute(
            "SELECT ruleset FROM campaigns WHERE id = ?", (session.campaign_id,),
        ).fetchone()[0]
        if ruleset != "coc7":
            raise AuthError("attributes_ruleset", "当前只支持 CoC7 属性导入。", 400)
        values = _read(connection, session.actor_id, ruleset)
        if text is not None:
            if values is not None:
                raise AuthError("attributes_exist", "角色已有属性。如需重新导入，请由 KP 重置。", 409)
            try:
                imported = parse_attributes(text)
            except ValueError as error:
                raise AuthError("invalid_attributes", str(error), 400) from error
            connection.execute(
                "INSERT INTO actor_attributes(actor_id, ruleset, values_json, imported_at) VALUES (?, ?, ?, ?)",
                (session.actor_id, ruleset, json.dumps(imported, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()),
            )
            values = effective_attributes(imported)
        return {"actor_name": session.actor_name, "attributes": values}


def _local_actor(connection: sqlite3.Connection, member_id: str) -> sqlite3.Row:
    row = connection.execute(
        """SELECT a.id, a.name, c.ruleset FROM actors AS a
           JOIN members AS m ON m.id = a.member_id AND m.role = 'pl'
           JOIN campaigns AS c ON c.id = m.campaign_id
           WHERE m.id = ? AND a.kind = 'pc'""", (member_id,),
    ).fetchone()
    if row is None:
        raise ValueError("找不到该玩家角色。")
    return row


def local_attributes(directory: Path, member_id: str) -> dict:
    with open_campaign(directory) as connection:
        actor = _local_actor(connection, member_id)
        return {"actor_name": actor["name"], "attributes": _read(connection, actor["id"], actor["ruleset"])}


def reset_local_attributes(directory: Path, member_id: str) -> None:
    with open_campaign(directory) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        actor = _local_actor(connection, member_id)
        connection.execute("DELETE FROM actor_attributes WHERE actor_id = ?", (actor["id"],))
