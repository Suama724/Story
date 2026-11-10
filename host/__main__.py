"""用 python -m host 管理本地团和角色。"""

import argparse
import json
import sqlite3
from pathlib import Path

from .management import add_member
from .storage import campaign_info, initialize_campaign, open_campaign


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Story 主机团数据管理")
    commands = parser.add_subparsers(dest="command")

    gui = commands.add_parser("gui", help="打开主机管理窗口")
    gui.add_argument("--data-root", type=Path, default=Path(__file__).resolve().parents[1] / "data")

    serve = commands.add_parser("serve", help="为管理窗口启动独立监听进程")
    serve.add_argument("directory", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    initialize = commands.add_parser("init", help="在数据根目录下创建一个新团")
    initialize.add_argument("data_root", type=Path, help="存放各团 ID 目录的数据根目录")
    initialize.add_argument("--name", required=True, help="团名")
    initialize.add_argument("--description", default="", help="团本简介")

    character = commands.add_parser("add-character", help="添加角色")
    character.add_argument("directory", type=Path, help="包含 story.sqlite3 的团目录")
    character.add_argument("--name", required=True, help="角色名")

    for command, help_text in (
        ("show", "打开已有团，显示稳定 ID 和基础信息"),
        ("migrate", "将已有团数据库迁移到当前版本"),
    ):
        action = commands.add_parser(command, help=help_text)
        action.add_argument("directory", type=Path, help="包含 story.sqlite3 的团目录")

    arguments = parser.parse_args(argv)
    if arguments.command in (None, "gui"):
        from .host_window import run_gui

        root = getattr(arguments, "data_root", Path(__file__).resolve().parents[1] / "data")
        return run_gui(root)
    if arguments.command == "serve":
        import asyncio

        from .server_process import run_server

        return asyncio.run(run_server(arguments.directory, arguments.host, arguments.port))
    try:
        if arguments.command == "init":
            directory = initialize_campaign(
                arguments.data_root, arguments.name, description=arguments.description,
            )
        else:
            directory = arguments.directory
        if arguments.command == "add-character":
            info = {"member_id": add_member(directory, arguments.name), "actor_name": arguments.name.strip()}
        else:
            with open_campaign(directory) as connection:
                info = campaign_info(connection)
        print(json.dumps({"directory": str(directory.resolve()), **info}, ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"操作失败，{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
