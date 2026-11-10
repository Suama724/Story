"""覆盖跨进程重开、事务失败和数据库版本边界。"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from host import migrations
from host.storage import DATABASE_NAME, campaign_info, initialize_campaign, open_campaign

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CampaignStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "团数据 #1"

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "host", *arguments],
            cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            encoding="utf-8",
            check=False,
        )

    def test_cli_initializes_and_reopens_with_stable_ids(self) -> None:
        result = self.run_cli("init", str(self.root), "--name", "地下室 🎲", "--description", "测试简介")
        self.assertEqual(result.returncode, 0, result.stderr)
        original = json.loads(result.stdout)
        directory = Path(original["directory"])
        self.assertEqual(directory.name, original["campaign_id"])
        self.assertEqual(original["name"], "地下室 🎲")
        self.assertEqual(original["kp_name"], "KP")
        self.assertEqual(original["description"], "测试简介")
        self.assertEqual(original["schema_version"], migrations.SCHEMA_VERSION)
        for command in ("show", "migrate", "show"):
            reopened = self.run_cli(command, str(directory))
            self.assertEqual(reopened.returncode, 0, reopened.stderr)
            self.assertEqual(json.loads(reopened.stdout), original)
        with open_campaign(directory) as connection:
            for table in ("campaigns", "play_sessions", "channels", "members", "actors"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE actors SET member_id = 'missing'")

    def test_different_campaigns_have_independent_ids(self) -> None:
        first = initialize_campaign(self.root, "同名团")
        second = initialize_campaign(self.root, "同名团")
        self.assertNotEqual(first, second)
        with open_campaign(first) as connection:
            first_info = campaign_info(connection)
        with open_campaign(second) as connection:
            second_info = campaign_info(connection)
        for field in ("campaign_id", "session_id", "lobby_id", "kp_member_id", "kp_actor_id"):
            self.assertNotEqual(first_info[field], second_info[field])

    def test_missing_database_is_not_created(self) -> None:
        result = self.run_cli("show", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.root.exists())
        self.assertNotIn("Traceback", result.stderr)

    def test_blank_names_do_not_create_directories(self) -> None:
        with self.assertRaises(ValueError):
            initialize_campaign(self.root, "  ")
        self.assertFalse(self.root.exists())

    def test_initialization_failure_rolls_back_and_cleans_new_directory(self) -> None:
        # 在建表后、写完初始化数据前模拟数据库失败。
        broken = (*migrations.MIGRATIONS[0],
                  "CREATE TRIGGER reject_actor BEFORE INSERT ON actors BEGIN SELECT RAISE(ABORT, 'test'); END")
        with patch.object(migrations, "MIGRATIONS", (broken, *migrations.MIGRATIONS[1:])):
            with self.assertRaises(sqlite3.IntegrityError):
                initialize_campaign(self.root, "测试团")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_upgrade_preserves_data_and_version(self) -> None:
        directory = initialize_campaign(self.root, "团")
        with open_campaign(directory) as connection:
            original = campaign_info(connection)
        upgrade = ("ALTER TABLE campaigns ADD COLUMN extra TEXT", "INVALID SQL")
        with patch.object(migrations, "SCHEMA_VERSION", migrations.SCHEMA_VERSION + 1), patch.object(
            migrations, "MIGRATIONS", (*migrations.MIGRATIONS, upgrade)
        ):
            with self.assertRaises(sqlite3.OperationalError):
                with open_campaign(directory):
                    pass
        with open_campaign(directory) as connection:
            self.assertEqual(campaign_info(connection), original)
            columns = connection.execute("PRAGMA table_info(campaigns)").fetchall()
            self.assertNotIn("extra", [column["name"] for column in columns])

    def test_unknown_and_newer_databases_are_rejected(self) -> None:
        self.root.mkdir()
        with closing(sqlite3.connect(self.root / DATABASE_NAME)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        with self.assertRaisesRegex(ValueError, "不是 Story"):
            with open_campaign(self.root):
                pass
        directory = initialize_campaign(self.root, "团")
        with closing(sqlite3.connect(directory / DATABASE_NAME)) as connection:
            connection.execute("PRAGMA user_version = 999")
        result = self.run_cli("migrate", str(directory))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("版本较新", result.stderr)
        with closing(sqlite3.connect(directory / DATABASE_NAME)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_concurrent_upgrade_applies_migration_once(self) -> None:
        directory = initialize_campaign(self.root, "团")
        upgrade = ("ALTER TABLE campaigns ADD COLUMN extra TEXT",)

        def reopen() -> int:
            with open_campaign(directory) as connection:
                return campaign_info(connection)["schema_version"]

        version = migrations.SCHEMA_VERSION + 1
        with patch.object(migrations, "SCHEMA_VERSION", version), patch.object(
            migrations, "MIGRATIONS", (*migrations.MIGRATIONS, upgrade)
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(reopen) for _ in range(2)]
                self.assertEqual([future.result() for future in futures], [version, version])


if __name__ == "__main__":
    unittest.main()
