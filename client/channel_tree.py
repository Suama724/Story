"""组织方与参与者共用的频道组导航。"""

from PySide6.QtCore import QModelIndex, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QStyle, QStyledItemDelegate, QToolTip,
    QTreeWidget, QTreeWidgetItem, QWidget,
)

from .ui_icons import phosphor_icon
from .ui_style import DISABLED, FOCUS, HOVER, SELECTION, TEXT
from .ui_widgets import ElidedLabel


DOT_GUTTER = 10
ICON_INSET = 6
ICON_SIZE = 18
NAME_INSET = DOT_GUTTER + ICON_INSET + ICON_SIZE + 6
UNREAD_ROLE = Qt.ItemDataRole.UserRole.value + 1


class _ChannelRows(QStyledItemDelegate):
    def __init__(self, owner: QTreeWidget):
        super().__init__(owner)
        self._hash = phosphor_icon("bold/hash-bold.svg", size=ICON_SIZE)

    @staticmethod
    def _name_rect(rect: QRect) -> QRect:
        return rect.adjusted(NAME_INSET, 0, -8, 0)

    def paint(self, painter, option, index) -> None:
        states = option.state
        selected = bool(states & QStyle.StateFlag.State_Selected)
        hovered = bool(states & QStyle.StateFlag.State_MouseOver)
        focused = bool(states & QStyle.StateFlag.State_HasFocus)
        enabled = bool(states & QStyle.StateFlag.State_Enabled)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if selected or hovered or focused:
            background = QRectF(option.rect).adjusted(DOT_GUTTER + 0.5, 2.5, -1.5, -2.5)
            painter.setPen(QPen(QColor(FOCUS)) if focused else Qt.PenStyle.NoPen)
            painter.setBrush(QColor(SELECTION) if selected else QColor(HOVER) if hovered else Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(background, 5, 5)
        if index.parent().isValid():
            center = option.rect.y() + option.rect.height() / 2
            color = QColor(TEXT if enabled else DISABLED)
            if index.data(UNREAD_ROLE) and not selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawEllipse(QRectF(option.rect.x() + 1, center - 2.5, 5, 5))
            icon_rect = QRect(option.rect.x() + DOT_GUTTER + ICON_INSET, round(center - ICON_SIZE / 2),
                              ICON_SIZE, ICON_SIZE)
            self._hash.paint(painter, icon_rect, mode=QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled)
            painter.setFont(option.font)
            painter.setPen(color)
            rect = self._name_rect(option.rect)
            name = option.fontMetrics.elidedText(index.data(), Qt.TextElideMode.ElideRight, rect.width())
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        painter.restore()

    def helpEvent(self, event, view, option, index) -> bool:
        if index.parent().isValid():
            name = index.data()
            if option.fontMetrics.horizontalAdvance(name) > self._name_rect(option.rect).width():
                QToolTip.showText(event.globalPos(), name, view)
                return True
        return super().helpEvent(event, view, option, index)


class ChannelTree(QTreeWidget):
    currentIndexChanged = Signal(int)
    createRequested = Signal(str)

    def __init__(self, *, managed=True):
        super().__init__()
        self.setObjectName("channelTree")
        self.setAccessibleName("频道列表")
        self.setHeaderHidden(True)
        self.setIndentation(0)
        self.setRootIsDecorated(False)
        self.setExpandsOnDoubleClick(False)
        self.setMouseTracking(True)
        self.setItemDelegate(_ChannelRows(self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.managed = managed
        self._items = {}
        self._carets = {
            expanded: phosphor_icon(f"bold/caret-{direction}-bold.svg", size=ICON_SIZE).pixmap(
                QSize(ICON_SIZE, ICON_SIZE), 2,
            )
            for expanded, direction in ((False, "right"), (True, "down"))
        }
        self.currentItemChanged.connect(self._changed)
        self.itemClicked.connect(self._toggle_group)
        self.itemExpanded.connect(self._update_group_caret)
        self.itemCollapsed.connect(self._update_group_caret)

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

    def _toggle_group(self, item, _column):
        if item.parent() is None and item.childCount():
            item.setExpanded(not item.isExpanded())

    def _update_group_caret(self, item):
        row = self.itemWidget(item, 0)
        if row is not None:
            caret = row.findChild(QLabel, "groupCaret")
            if item.childCount():
                caret.setPixmap(self._carets[item.isExpanded()])
            else:
                caret.clear()

    def set_unread(self, channel_id: str, unread: bool) -> None:
        item = self._items.get(channel_id)
        if item is not None:
            item.setData(0, UNREAD_ROLE, unread)
            item.setData(0, Qt.ItemDataRole.AccessibleDescriptionRole, "有未读消息" if unread else "")

    def set_channels(self, groups, channels, selected):
        expanded = {self.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole): self.topLevelItem(i).isExpanded()
                    for i in range(self.topLevelItemCount())}
        self.clear()
        self._items.clear()
        for group in groups:
            root = QTreeWidgetItem(self, [group["name"]])
            root.setData(0, Qt.ItemDataRole.UserRole, group["group_id"])
            root.setFlags(root.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            root.setSizeHint(0, QSize(0, 40))
            row = QWidget()
            row.setMouseTracking(True)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(DOT_GUTTER + ICON_INSET, 0, 6, 0)
            layout.setSpacing(6)
            caret = QLabel()
            caret.setObjectName("groupCaret")
            caret.setFixedWidth(ICON_SIZE)
            caret.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(caret)
            title = ElidedLabel(group["name"])
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
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
                item = QTreeWidgetItem(root, [channel["name"] + suffix])
                channel_id = channel["channel_id"]
                item.setData(0, Qt.ItemDataRole.UserRole, channel_id)
                item.setSizeHint(0, QSize(0, 36))
                self._items[channel_id] = item
                self.set_unread(channel_id, bool(channel.get("unread_count")))
            root.setExpanded(expanded.get(group["group_id"], True))
            self._update_group_caret(root)
        if selected in self._items:
            self.setCurrentItem(self._items[selected])
