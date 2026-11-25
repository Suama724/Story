"""Common widgets for the channel workspace."""

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import (
    QLabel, QPushButton, QSplitter, QStyle, QStyleOptionButton, QStylePainter, QToolTip, QWidget,
)


class ElidedLabel(QLabel):
    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    def event(self, event) -> bool:
        if (event.type() == QEvent.Type.ToolTip and not self.toolTip()
                and self.fontMetrics().horizontalAdvance(self.text()) > self.contentsRect().width()):
            QToolTip.showText(event.globalPos(), self.text(), self)
            return True
        return super().event(event)


class ElidedButton(QPushButton):
    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event) -> None:
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        content = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        option.text = option.fontMetrics.elidedText(
            option.text, Qt.TextElideMode.ElideRight, content.width(), Qt.TextFlag.TextShowMnemonic.value,
        )
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


def split_channel_view(sidebar: QWidget, chat: QWidget) -> QSplitter:
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.setObjectName("channelSplitter")
    splitter.setChildrenCollapsible(False)
    splitter.setOpaqueResize(True)
    splitter.setHandleWidth(5)
    sidebar.setMinimumWidth(152)
    chat.setMinimumWidth(316)
    splitter.addWidget(sidebar)
    splitter.addWidget(chat)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([240, 640])
    splitter.handle(1).setAccessibleName("调整频道栏宽度")
    return splitter
