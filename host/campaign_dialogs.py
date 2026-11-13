"""团管理及带存档删除选择的确认框。"""

import sqlite3

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from client.player_window import identity_list, label
from .campaign_library import CampaignLibrary


def add_campaign_card(widget: QListWidget, campaign: dict, *, show_hidden: bool = False) -> None:
    players = sum(member["role"] == "pl" for member in campaign["members"])
    observers = sum(member["role"] == "ob" for member in campaign["members"])
    suffix = "（未显示）" if show_hidden and campaign.get("hidden") else ""
    item = QListWidgetItem(f"{campaign['name']}{suffix}\nPL: {players} 人   OB: {observers} 人")
    item.setData(Qt.ItemDataRole.UserRole, campaign["directory"])
    item.setData(Qt.ItemDataRole.AccessibleTextRole, campaign["name"] + suffix)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    item.setSizeHint(QSize(180, 76))
    widget.addItem(item)


class DeleteCampaignDialog(QDialog):
    def __init__(self, parent: QWidget, campaign: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle("删除团")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(label(f"删除「{campaign['name']}」？"))
        path = QLineEdit(str(campaign["directory"]))
        path.setReadOnly(True)
        path.setToolTip(str(campaign["directory"]))
        layout.addWidget(path)
        self.delete_archive = QCheckBox("删除本地存档（不可恢复）")
        layout.addWidget(self.delete_archive)
        layout.addWidget(label("未勾选时只从选团页面移除，保留本地存档。"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("删除")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class CampaignManagementDialog(QDialog):
    def __init__(self, parent: QWidget, library: CampaignLibrary, campaigns: list[dict]) -> None:
        super().__init__(parent)
        self.setWindowTitle("团管理")
        self.resize(560, 460)
        self.library = library
        self.campaigns = {campaign["directory"]: campaign for campaign in campaigns}
        self.changed = False
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.root_field = QLineEdit(str(library.root))
        self.root_field.setReadOnly(True)
        self.root_field.setToolTip(str(library.root))
        form.addRow("数据目录", self.root_field)
        self.archive_field = QLineEdit()
        self.archive_field.setReadOnly(True)
        form.addRow("存档位置", self.archive_field)
        layout.addLayout(form)
        self.campaign_list = identity_list()
        for campaign in campaigns:
            add_campaign_card(self.campaign_list, campaign, show_hidden=True)
        layout.addWidget(self.campaign_list, 1)
        self.feedback = label()
        layout.addWidget(self.feedback)
        buttons = QHBoxLayout()
        self.delete_button = QPushButton("删除")
        self.delete_button.clicked.connect(self._delete_campaign)
        self.import_button = QPushButton("导入团")
        self.import_button.setEnabled(False)
        self.import_button.setToolTip("导入团将在后续实现。")
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        for button in (self.delete_button, self.import_button, close):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.campaign_list.currentItemChanged.connect(self._selection_changed)
        if campaigns:
            self.campaign_list.setCurrentRow(0)
        self._selection_changed()

    def _selection_changed(self, *_args: object) -> None:
        item = self.campaign_list.currentItem()
        self.delete_button.setEnabled(item is not None)
        self.archive_field.setText(str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else "")
        self.archive_field.setToolTip(self.archive_field.text())
        self.archive_field.setCursorPosition(0)

    def _delete_campaign(self) -> None:
        item = self.campaign_list.currentItem()
        if item is None:
            return
        directory = item.data(Qt.ItemDataRole.UserRole)
        campaign = self.campaigns[directory]
        confirmation = DeleteCampaignDialog(self, campaign)
        accepted = confirmation.exec() == QDialog.DialogCode.Accepted
        delete_archive = confirmation.delete_archive.isChecked()
        confirmation.deleteLater()
        if not accepted:
            return
        try:
            self.library.remove(directory, delete_archive=delete_archive)
        except (OSError, sqlite3.Error, ValueError) as error:
            self.feedback.setText(str(error))
            return
        self.changed = True
        if delete_archive:
            self.campaign_list.takeItem(self.campaign_list.row(item))
            self.campaigns.pop(directory)
            self.feedback.setText("本地存档已删除。")
        else:
            campaign["hidden"] = True
            players = sum(member["role"] == "pl" for member in campaign["members"])
            observers = sum(member["role"] == "ob" for member in campaign["members"])
            item.setText(f"{campaign['name']}（未显示）\nPL: {players} 人   OB: {observers} 人")
            self.feedback.setText("已从选团页面移除，本地存档已保留。")
        self._selection_changed()
