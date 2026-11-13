"""团列表隐藏、同名创建及存档删除边界。"""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from host.campaign_library import CampaignLibrary
from host.management import campaign_details


class CampaignLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "团数据"
        self.library = CampaignLibrary(self.root)

    def test_same_name_reuses_ids_description_and_hidden_archive(self):
        first = self.library.enter("调查团", "原简介")
        original = campaign_details(first)
        self.assertEqual(self.library.enter(" 调查团 ", "新简介"), first)
        self.assertEqual(campaign_details(first), original)
        self.library.remove(first)
        self.assertTrue(first.exists())
        reopened = CampaignLibrary(self.root)
        entries, errors = reopened.entries()
        self.assertFalse(errors)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["hidden"])
        self.assertEqual(reopened.enter("调查团"), first)
        self.assertFalse(reopened.entries()[0][0]["hidden"])

    def test_concurrent_same_name_creates_one_campaign(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            paths = list(executor.map(lambda _: CampaignLibrary(self.root).enter("同名团"), range(2)))
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(len(self.library.entries()[0]), 1)

    def test_explicit_delete_removes_only_selected_archive(self):
        first = self.library.enter("待删除")
        second = self.library.enter("保留")
        (first / "附件.txt").write_text("同团文件", encoding="utf-8")
        before = campaign_details(second)
        self.library.remove(first, delete_archive=True)
        self.assertFalse(first.exists())
        self.assertEqual(campaign_details(second), before)
        self.assertEqual([entry["name"] for entry in self.library.entries()[0]], ["保留"])

    def test_invalid_root_external_and_unrecognized_directories_are_preserved(self):
        self.library.enter("保留")
        outside = CampaignLibrary(Path(self.temp.name) / "其他目录").enter("外部团")
        unrelated = self.root / "普通文件夹"
        unrelated.mkdir()
        marker = unrelated / "保留.txt"
        marker.write_text("保留", encoding="utf-8")
        for directory in (self.root, outside, unrelated):
            with self.subTest(path=str(directory)), self.assertRaises((ValueError, OSError, sqlite3.Error)):
                self.library.remove(directory, delete_archive=True)
            self.assertTrue(directory.exists())
        self.assertTrue(marker.exists())

    def test_failed_delete_does_not_hide_the_archive(self):
        directory = self.library.enter("未删除")
        with patch("host.campaign_library.shutil.rmtree", side_effect=PermissionError("文件被占用")):
            with self.assertRaises(PermissionError):
                self.library.remove(directory, delete_archive=True)
        self.assertTrue(directory.exists())
        self.assertFalse(self.library.entries()[0][0]["hidden"])
