"""组织方与参与者共用的频道组导航。"""

from PySide6.QtCore import QModelIndex, QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QWidget


def text_label(text):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class ChannelTree(QTreeWidget):
    currentIndexChanged = Signal(int)
    createRequested = Signal(str)

    def __init__(self, *, managed=True):
        super().__init__()
        self.setObjectName("channelTree")
        self.setAccessibleName("频道列表")
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setRootIsDecorated(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.managed = managed
        self._items = {}
        self.currentItemChanged.connect(self._changed)

    def _changed(self, item, _previous):
        if item is not None and item.data(0, Qt.ItemDataRole.UserRole) in self._items:
            self.currentIndexChanged.emit(self.findData(item.data(0, Qt.ItemDataRole.UserRole)))

    def currentData(self):
        item = self.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None

    def count(self):
        return len(self._items)

    def findData(self, channel_id):
        return next((index for index, key in enumerate(self._items) if key == channel_id), -1)

    def setCurrentIndex(self, index):
        if isinstance(index, QModelIndex):
            return super().setCurrentIndex(index)
        items = list(self._items.values())
        self.setCurrentItem(items[index] if 0 <= index < len(items) else None)

    def set_channels(self, groups, channels, selected):
        expanded = {self.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole): self.topLevelItem(i).isExpanded()
                    for i in range(self.topLevelItemCount())}
        self.clear()
        self._items.clear()
        for group in groups:
            root = QTreeWidgetItem(self)
            root.setData(0, Qt.ItemDataRole.UserRole, group["group_id"])
            root.setFlags(root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            root.setSizeHint(0, QSize(0, 40))
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(3, 0, 4, 0)
            title = text_label(group["name"])
            title.setObjectName("muted")
            layout.addWidget(title, 1)
            if self.managed:
                create = QPushButton("+")
                create.setObjectName("groupAdd")
                create.setAccessibleName(f"在{group['name']}中新建频道")
                create.setFixedSize(28, 28)
                create.clicked.connect(lambda _checked=False, key=group["group_id"]: self.createRequested.emit(key))
                layout.addWidget(create)
            self.setItemWidget(root, 0, row)
            for channel in channels:
                if channel.get("group_id") != group["group_id"]:
                    continue
                suffix = " · 归档" if channel["status"] == "archived" else ""
                if channel.get("unread_count"):
                    suffix += f" · {channel['unread_count']}"
                item = QTreeWidgetItem(root, [f"# {channel['name']}{suffix}"])
                item.setData(0, Qt.ItemDataRole.UserRole, channel["channel_id"])
                item.setToolTip(0, channel["name"] + suffix)
                item.setSizeHint(0, QSize(0, 36))
                self._items[channel["channel_id"]] = item
            root.setExpanded(expanded.get(group["group_id"], True))
        if selected in self._items:
            self.setCurrentItem(self._items[selected])
