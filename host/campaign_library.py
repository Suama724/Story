"""组织者的本地团列表：同名复用、隐藏记录与显式删除存档。"""

import shutil
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .management import discover_campaigns
from .storage import initialize_campaign, open_campaign


class CampaignLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @contextmanager
    def _preferences(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.root / ".campaign-library.sqlite3", timeout=10)) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS hidden_campaigns (directory TEXT PRIMARY KEY)")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection

    def entries(self) -> tuple[list[dict], list[str]]:
        with self._preferences() as connection:
            hidden = {row[0] for row in connection.execute("SELECT directory FROM hidden_campaigns")}
            campaigns, errors = discover_campaigns(self.root)
        return [{**campaign, "hidden": campaign["directory"].name in hidden} for campaign in campaigns], errors

    def enter(self, name: str, description: str = "") -> Path:
        name = name.strip()
        if not name:
            raise ValueError("团名不能为空。")
        with self._preferences() as connection:
            # 持有目录级事务，多个窗口同时输入同名团也只创建一份。
            campaigns, errors = discover_campaigns(self.root)
            matches = [campaign for campaign in campaigns if campaign["name"] == name]
            if matches:
                directory = matches[0]["directory"]
                connection.execute("DELETE FROM hidden_campaigns WHERE directory = ?", (directory.name,))
                return directory
            if errors:
                raise ValueError("部分团无法读取，无法确认团名是否重复：" + "；".join(errors))
            return initialize_campaign(self.root, name, description=description)

    def remove(self, directory: Path, *, delete_archive: bool = False) -> None:
        source = directory.absolute()
        # 只允许处理数据根目录的直接子目录，拒绝指向别处的链接和联接点。
        if source.parent != self.root or source.is_symlink() or source.is_junction():
            raise ValueError("存档位置不在当前团数据目录内。")
        target = source.resolve(strict=True)
        if target.parent != self.root or target == self.root:
            raise ValueError("存档位置不在当前团数据目录内。")
        with self._preferences() as connection:
            with open_campaign(target):
                pass
            if delete_archive:
                shutil.rmtree(target)
                connection.execute("DELETE FROM hidden_campaigns WHERE directory = ?", (target.name,))
            else:
                connection.execute("INSERT OR IGNORE INTO hidden_campaigns VALUES (?)", (target.name,))
