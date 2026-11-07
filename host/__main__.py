"""用 python -m host 初始化、查看和迁移本地团。"""

import argparse
import json
import sqlite3
from pathlib import Path

from .storage import campaign_info, initialize_campaign, open_campaign


def main() -> int:
    parser = argparse.ArgumentParser(description="Story 主机团数据管理")
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="在数据根目录下创建一个新团")
    initialize.add_argument("data_root", type=Path, help="存放各团 ID 目录的数据根目录")
    initialize.add_argument("--name", required=True, help="团名")
    initialize.add_argument("--kp-name", default="KP", help="KP 显示名称，默认 KP")

    for command, help_text in (
        ("show", "打开已有团，显示稳定 ID 和基础信息"),
        ("migrate", "将已有团数据库迁移到当前版本"),
    ):
        action = commands.add_parser(command, help=help_text)
        action.add_argument("directory", type=Path, help="包含 story.sqlite3 的团目录")

    arguments = parser.parse_args()
    try:
        if arguments.command == "init":
            directory = initialize_campaign(
                arguments.data_root, arguments.name, arguments.kp_name
            )
        else:
            directory = arguments.directory
        with open_campaign(directory) as connection:
            info = campaign_info(connection)
        print(json.dumps({"directory": str(directory.resolve()), **info}, ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"操作失败，{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
