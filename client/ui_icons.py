"""Load the bundled Phosphor SVGs in the colors used by Qt widgets."""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .ui_style import DISABLED, ICON_ROOT, MUTED


DEFAULT_AVATAR_SIZE = 24
DEFAULT_AVATAR = b"""
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
  <circle cx="12" cy="12" r="12" fill="#dde2e7"/>
  <circle cx="12" cy="9" r="3" fill="currentColor"/>
  <path d="M5.8 18a6.2 5 0 0 1 12.4 0Z" fill="currentColor"/>
</svg>
"""


def default_avatar_icon() -> QIcon:
    return svg_icon(DEFAULT_AVATAR, "#55616e", size=DEFAULT_AVATAR_SIZE)


def phosphor_icon(file: str, color: str = MUTED, disabled: str = DISABLED, *, size: int = 20) -> QIcon:
    return svg_icon((ICON_ROOT / file).read_bytes(), color, disabled, size=size)


def svg_icon(source: bytes, color: str = MUTED, disabled: str = DISABLED, *, size: int = 20) -> QIcon:
    icon = QIcon()
    for mode, tone in ((QIcon.Mode.Normal, color), (QIcon.Mode.Disabled, disabled)):
        renderer = QSvgRenderer(QByteArray(source.replace(b"currentColor", tone.encode("ascii"))))
        for scale in (1, 2):
            pixmap = QPixmap(size * scale, size * scale)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            pixmap.setDevicePixelRatio(scale)
            icon.addPixmap(pixmap, mode)
    return icon
