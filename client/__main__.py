import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from client.lobby_window import LobbyWindow
from client.player_window import PlayerWindow
from client.role_window import RoleWindow


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--serve"]:
        from host.__main__ import main as host_main
        from host.worker_stdio import restore_worker_stdio

        restore_worker_stdio()
        return host_main(["serve", *argv[1:]])

    parser = argparse.ArgumentParser(description="Story 桌面应用")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--legacy", action="store_true", help="打开 M1 临时昵称测试大厅")
    mode.add_argument("--player", action="store_true", help="直接打开玩家登录页")
    parser.add_argument("--data-root", type=Path, help="组织者的团数据目录")
    arguments = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Story")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    if arguments.legacy:
        window = LobbyWindow()
    elif arguments.player:
        window = PlayerWindow()
    else:
        base = Path(__file__).resolve().parents[1]
        if getattr(sys, "frozen", False):
            base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
        app.setQuitOnLastWindowClosed(False)
        window = RoleWindow(arguments.data_root or base / "data")
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
