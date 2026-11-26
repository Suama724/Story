"""Story title bar and resize border for the main desktop windows."""

import sys

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from .ui_icons import svg_icon
from .ui_style import BACKGROUND, BORDER, FOCUS, HOVER, MUTED, SELECTION, TEXT
from .ui_widgets import ElidedLabel


BAR_HEIGHT = 36
BUTTON_WIDTH = 44
FRAME_WIDTH = 1
FRAME_RADIUS = 8
FRAME_COLOR = "#b6b6b6"
RESIZE_BORDER = 5
RESIZE_CORNER = 12
STYLE = f"""
QFrame#storyTitleBar {{ background: {BACKGROUND}; }}
QLabel#windowCaption {{ color: {TEXT}; font-size: 13px; font-weight: 600; }}
QLabel#windowIdentity {{ color: {MUTED}; font-size: 13px; }}
QToolButton#windowControl {{
    background: transparent; border: 1px solid transparent; border-radius: 0; padding: 0;
}}
QToolButton#windowControl:hover {{ background: {HOVER}; }}
QToolButton#windowControl:pressed {{ background: {SELECTION}; }}
QToolButton#windowControl:focus {{ border-color: {FOCUS}; }}
"""
DIVIDER_STYLE = f"QFrame#storyTitleBar {{ border-bottom: 1px solid {BORDER}; }}"
LEFT, RIGHT, TOP, BOTTOM = Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge
RESIZE_CURSORS = {
    LEFT: Qt.CursorShape.SizeHorCursor, RIGHT: Qt.CursorShape.SizeHorCursor,
    TOP: Qt.CursorShape.SizeVerCursor, BOTTOM: Qt.CursorShape.SizeVerCursor,
    TOP | LEFT: Qt.CursorShape.SizeFDiagCursor, BOTTOM | RIGHT: Qt.CursorShape.SizeFDiagCursor,
    TOP | RIGHT: Qt.CursorShape.SizeBDiagCursor, BOTTOM | LEFT: Qt.CursorShape.SizeBDiagCursor,
}


def _caption_icon(shape: str, size: int = 13):
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="none" '
        'stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round">'
        + shape + "</svg>"
    )
    return svg_icon(source.encode("ascii"), TEXT, size=size)


class _ResizeBorder(QWidget):
    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.setMouseTracking(True)
        self._drag = None

    def sync(self) -> None:
        window = self.window()
        if window.isMaximized() or window.isFullScreen() or window.isMinimized():
            self._drag = None
            self.hide()
            return
        self.setGeometry(window.rect())
        rect = self.rect()
        self.setMask(QRegion(rect) - QRegion(rect.adjusted(
            RESIZE_BORDER, RESIZE_BORDER, -RESIZE_BORDER, -RESIZE_BORDER,
        )))
        self.raise_()
        self.show()

    def _edges_at(self, position: QPoint) -> Qt.Edges:
        edges = Qt.Edge(0)
        if position.x() < RESIZE_CORNER:
            edges |= LEFT
        elif position.x() >= self.width() - RESIZE_CORNER:
            edges |= RIGHT
        if position.y() < RESIZE_CORNER:
            edges |= TOP
        elif position.y() >= self.height() - RESIZE_CORNER:
            edges |= BOTTOM
        return edges

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        window = self.window()
        edges = self._edges_at(event.position().toPoint())
        handle = window.windowHandle()
        # A frameless Windows window has no sizing frame for the system's resize command.
        if sys.platform != "win32" and handle is not None and handle.startSystemResize(edges):
            return
        self._drag = (edges, event.globalPosition().toPoint(), window.geometry())
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            edges = self._edges_at(event.position().toPoint())
            self.setCursor(RESIZE_CURSORS.get(edges, Qt.CursorShape.ArrowCursor))
            return
        window = self.window()
        edges, start, rectangle = self._drag
        offset = event.globalPosition().toPoint() - start
        left, top, width, height = rectangle.getRect()
        if edges & (LEFT | RIGHT):
            width = min(window.maximumWidth(), max(window.minimumWidth(), width + offset.x() * (-1 if edges & LEFT else 1)))
        if edges & (TOP | BOTTOM):
            height = min(window.maximumHeight(), max(window.minimumHeight(), height + offset.y() * (-1 if edges & TOP else 1)))
        if edges & LEFT:
            left += rectangle.width() - width
        if edges & TOP:
            top += rectangle.height() - height
        window.setGeometry(left, top, width, height)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = None


class _WindowSurface(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._rounded = True
        self.content = QWidget(self)
        self.content.installEventFilter(self)
        self._frame_layout = QVBoxLayout(self)
        self._frame_layout.setContentsMargins(FRAME_WIDTH, FRAME_WIDTH, FRAME_WIDTH, FRAME_WIDTH)
        self._frame_layout.addWidget(self.content)

    def set_rounded(self, rounded: bool) -> None:
        if self._rounded != rounded:
            self._rounded = rounded
            margin = FRAME_WIDTH if rounded else 0
            self._frame_layout.setContentsMargins(margin, margin, margin, margin)
            self._clip_content()
            self.update()

    def _clip_content(self) -> None:
        if self._rounded:
            # Child widgets stay inside the frame. The outer edge keeps its per-pixel alpha.
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(self.content.rect()), FRAME_RADIUS, FRAME_RADIUS)
            self.content.setMask(QRegion(clip.toFillPolygon().toPolygon()))
        else:
            self.content.clearMask()

    def eventFilter(self, watched, event) -> bool:
        if watched == getattr(self, "content", None) and event.type() == QEvent.Type.Resize:
            self._clip_content()
        return super().eventFilter(watched, event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        if not self._rounded:
            painter.fillRect(self.rect(), QColor(BACKGROUND))
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(BACKGROUND))
        painter.setPen(QPen(QColor(FRAME_COLOR), FRAME_WIDTH))
        inset = FRAME_WIDTH / 2
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        painter.drawRoundedRect(rect, FRAME_RADIUS - inset, FRAME_RADIUS - inset)


class WindowTitleBar(QFrame):
    def __init__(self, window: QWidget, caption: str, surface: _WindowSurface) -> None:
        super().__init__()
        self._window = window
        self._surface = surface
        self._press = None
        self._drag_offset = None
        self.setObjectName("storyTitleBar")
        self.set_divider_visible(True)
        self.setFixedHeight(BAR_HEIGHT)
        window.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize_border = _ResizeBorder(window)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 1)
        row.setSpacing(0)
        row.addSpacing(3 * BUTTON_WIDTH)

        self.title_group = QWidget()
        self.title_group.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        center = QHBoxLayout(self.title_group)
        center.setContentsMargins(8, 0, 8, 0)
        center.setSpacing(8)
        self.caption = ElidedLabel()
        self.caption.setObjectName("windowCaption")
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        center.addWidget(self.caption)
        self.separator = QLabel()
        self.separator.setPixmap(_caption_icon('<circle cx="8" cy="8" r="7" fill="currentColor" stroke="none"/>', 5)
                                 .pixmap(QSize(5, 5), 2))
        center.addWidget(self.separator)
        self.identity = ElidedLabel()
        self.identity.setObjectName("windowIdentity")
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        center.addWidget(self.identity)
        row.addWidget(self.title_group, 1, Qt.AlignmentFlag.AlignCenter)

        self._maximize_icon = _caption_icon('<rect x="2.5" y="2.5" width="11" height="11"/>')
        self._restore_icon = _caption_icon('<rect x="2.5" y="5.5" width="8" height="8"/><path d="M5.5 5.5V2.5h8v8h-3"/>')
        self.minimize_button = self._button("最小化", _caption_icon('<path d="M2.5 8h11"/>'), window.showMinimized)
        self.maximize_button = self._button("最大化", self._maximize_icon, self._toggle_maximized)
        self.close_button = self._button("关闭", _caption_icon('<path d="m3.25 3.25 9.5 9.5m0-9.5-9.5 9.5"/>'), window.close)
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            row.addWidget(button)

        self.set_caption(caption)
        window.installEventFilter(self)

    def set_caption(self, caption: str, identity: str = "") -> None:
        self.caption.setText(caption)
        self.identity.setText(identity)
        self.separator.setVisible(bool(identity))
        self.identity.setVisible(bool(identity))
        self._window.setWindowTitle(f"{caption} · {identity}" if identity else caption)

    def set_divider_visible(self, visible: bool) -> None:
        self.setStyleSheet(STYLE + (DIVIDER_STYLE if visible else ""))

    def _toggle_maximized(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()

    def eventFilter(self, watched, event) -> bool:
        if watched == getattr(self, "_window", None):
            if event.type() in (QEvent.Type.Show, QEvent.Type.Resize, QEvent.Type.WindowStateChange):
                self.resize_border.sync()
            if event.type() == QEvent.Type.WindowStateChange:
                maximized = self._window.isMaximized()
                self._surface.set_rounded(not (maximized or self._window.isFullScreen()))
                label = "还原" if maximized else "最大化"
                self.maximize_button.setIcon(self._restore_icon if maximized else self._maximize_icon)
                self.maximize_button.setToolTip(label)
                self.maximize_button.setAccessibleName(label)
        return super().eventFilter(watched, event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = self._drag_offset = None
            self._toggle_maximized()
            event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition().toPoint()
            self._drag_offset = None
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        position = event.globalPosition().toPoint()
        if self._drag_offset is None:
            if (position - self._press).manhattanLength() < QApplication.startDragDistance():
                return
            window = self._window
            handle = window.windowHandle()
            if handle is not None and handle.startSystemMove():
                self._press = None
                return
            offset = window.mapFromGlobal(self._press)
            if window.isMaximized():
                fraction = offset.x() / max(1, window.width())
                window.showNormal()
                offset.setX(round(window.width() * fraction))
            self._drag_offset = offset
        self._window.move(position - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = self._drag_offset = None

    @staticmethod
    def _button(label, icon, action) -> QToolButton:
        button = QToolButton()
        button.setObjectName("windowControl")
        button.setFixedSize(BUTTON_WIDTH, BAR_HEIGHT - 1)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setIconSize(QSize(13, 13))
        button.setIcon(icon)
        button.setAccessibleName(label)
        button.setToolTip(label)
        button.clicked.connect(action)
        return button


def window_layout(window: QWidget, container: QWidget, caption: str) -> tuple[WindowTitleBar, QVBoxLayout]:
    shell = QVBoxLayout(container)
    shell.setContentsMargins(0, 0, 0, 0)
    surface = _WindowSurface()
    shell.addWidget(surface)
    layout = QVBoxLayout(surface.content)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    title_bar = WindowTitleBar(window, caption, surface)
    layout.addWidget(title_bar, alignment=Qt.AlignmentFlag.AlignTop)
    body = QVBoxLayout()
    layout.addLayout(body, 1)
    return title_bar, body
