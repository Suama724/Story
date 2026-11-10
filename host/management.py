"""主机本地团和人物管理，监听服务关闭时也可使用。"""

import sqlite3
from pathlib import Path

from .auth import DEFAULT_PASSWORD
from .storage import DATABASE_NAME, campaign_info, create_member, member_name, open_campaign


def list_members(directory: Path) -> list[dict[str, object]]:
    with open_campaign(directory) as connection:
        rows = connection.execute(
            """SELECT m.id AS member_id, m.role, a.name AS actor_name,
                      CASE WHEN m.role = 'pl' AND c.password_hash IS NULL
                           THEN coalesce(c.password, ?) END AS password
               FROM members AS m
               JOIN actors AS a ON a.member_id = m.id AND a.kind IN ('kp', 'pc')
               LEFT JOIN credentials AS c ON c.member_id = m.id
               ORDER BY m.role = 'kp' DESC, m.created_at, m.id""",
            (DEFAULT_PASSWORD,),
        ).fetchall()
    return [dict(row) for row in rows]


def campaign_details(directory: Path) -> dict[str, object]:
    with open_campaign(directory) as connection:
        info = campaign_info(connection)
    return {"directory": directory.resolve(), **info, "members": list_members(directory)}


def discover_campaigns(root: Path) -> tuple[list[dict[str, object]], list[str]]:
    campaigns = []
    errors = []
    for database in sorted(root.glob(f"*/{DATABASE_NAME}")):
        try:
            campaigns.append(campaign_details(database.parent))
        except (OSError, sqlite3.Error, ValueError) as error:
            errors.append(f"{database.parent.name}，{error}")
    return campaigns, errors


def _check_name(connection: sqlite3.Connection, name: str, member_id: str = "") -> None:
    duplicate = connection.execute(
        """SELECT 1 FROM actors WHERE kind IN ('kp', 'pc')
           AND name = ? COLLATE NOCASE AND member_id != ?""",
        (name, member_id),
    ).fetchone()
    if duplicate is not None:
        raise ValueError("这个团已有同名角色，请换一个角色名。")


def add_member(directory: Path, actor_name: str) -> str:
    actor_name = member_name(actor_name)
    with open_campaign(directory) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            _check_name(connection, actor_name)
            campaign_id = connection.execute("SELECT id FROM campaigns").fetchone()[0]
            return create_member(connection, campaign_id, actor_name, actor_name)


def edit_member(directory: Path, member_id: str, actor_name: str) -> None:
    actor_name = member_name(actor_name)
    with open_campaign(directory) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            member = connection.execute("SELECT 1 FROM members WHERE id = ? AND role = 'pl'", (member_id,)).fetchone()
            if member is None:
                raise ValueError("成员不存在。")
            _check_name(connection, actor_name, member_id)
            connection.execute("UPDATE members SET name = ? WHERE id = ?", (actor_name, member_id))
            connection.execute(
                "UPDATE actors SET name = ? WHERE member_id = ? AND kind = 'pc'",
                (actor_name, member_id),
            )
