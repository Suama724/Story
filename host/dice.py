"""将已授权的人物数据和团设置提供给独立骰子部件。"""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from coc7_attributes import ATTRIBUTES, RESOURCES, attribute_key
from dice_component import CheckInput, DiceCommand, DiceError, RuleConfig
from .attributes import read_actor_attributes
from .storage import open_campaign


_ATTRIBUTE_NAMES = dict((*ATTRIBUTES, *RESOURCES))


def read_dice_config(connection: sqlite3.Connection) -> RuleConfig:
    raw = connection.execute("SELECT dice_config FROM campaigns").fetchone()[0]
    settings = json.loads(raw)
    # 旧团档可能保留已经移除的难度限制选项。
    settings.pop("require_success", None)
    settings.pop("require_failure", None)
    return RuleConfig(**settings)


def local_dice_config(directory: Path) -> RuleConfig:
    with open_campaign(directory) as connection:
        return read_dice_config(connection)


def save_local_dice_config(directory: Path, config: RuleConfig) -> None:
    config.validate()
    with open_campaign(directory) as connection, connection:
        connection.execute("UPDATE campaigns SET dice_config = ?", (json.dumps(asdict(config)),))


def check_input(connection: sqlite3.Connection, command: DiceCommand, *,
                actor_id: str, actor_name: str, organizer: bool) -> CheckInput:
    if command.check.isascii() and command.check.isdecimal():
        if not organizer:
            raise DiceError("dice_delegate_forbidden")
        if len(command.check) > 4:
            raise DiceError("dice_limit")
        return CheckInput(command.subject or actor_name, "临时数值", int(command.check))
    if command.subject:
        if not organizer:
            raise DiceError("dice_delegate_forbidden")
        actor = connection.execute(
            "SELECT id, name FROM actors WHERE name = ? COLLATE NOCASE AND kind = 'pc'", (command.subject,),
        ).fetchone()
        if actor is None:
            raise DiceError("dice_actor_missing")
        actor_id, actor_name = actor
    key = attribute_key(command.check)
    card = read_actor_attributes(connection, actor_id, "coc7")
    if card is None:
        raise DiceError("dice_card_missing")
    if key not in card:
        raise DiceError("dice_attribute_missing")
    return CheckInput(actor_name, _ATTRIBUTE_NAMES.get(key, key), card[key])
