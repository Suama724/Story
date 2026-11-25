"""Window controls and border interactions for Story's custom title bar."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from client.window_title_bar import FRAME_RADIUS, FRAME_WIDTH, window_layout


class WindowTitleBarTests(unittest.TestCase):
    def test_caption_centering_window_buttons_and_border_resize(self):
        app = QApplication.instance() or QApplication([])
        window = QWidget()
        self.addCleanup(window.close)
        window.setMinimumSize(360, 246)
        window.resize(680, 400)
        bar, body = window_layout(window, window, "选择身份")
        body.addWidget(QWidget(), 1)
        bar.set_caption("调查团", "KP")
        window.show()
        app.processEvents()

        self.assertEqual(bar.mapTo(window, QPoint()).y(), FRAME_WIDTH)
        self.assertLessEqual(abs(bar.title_group.geometry().center().x() - bar.rect().center().x()), 1)
        self.assertTrue(bar.separator.isVisible())
        self.assertFalse(bar.separator.pixmap().isNull())
        frame = window.grab().toImage()
        radius = round(FRAME_RADIUS * frame.devicePixelRatioF())
        self.assertTrue(window.mask().isEmpty())
        self.assertEqual(frame.pixelColor(0, 0).alpha(), 0)
        self.assertTrue(any(0 < frame.pixelColor(x, y).alpha() < 255
                            for x in range(radius) for y in range(radius)))

        bar.maximize_button.click()
        self.assertTrue(window.isMaximized())
        self.assertEqual(bar.maximize_button.accessibleName(), "还原")
        self.assertFalse(bar.resize_border.isVisible())
        app.processEvents()
        self.assertEqual(bar.mapTo(window, QPoint()).y(), 0)
        bar.maximize_button.click()
        app.processEvents()
        self.assertFalse(window.isMaximized())
        self.assertTrue(bar.resize_border.isVisible())
        self.assertEqual(bar.mapTo(window, QPoint()).y(), FRAME_WIDTH)
        self.assertEqual(bar.maximize_button.accessibleName(), "最大化")

        previous_size = window.size()
        border = bar.resize_border
        pos = border.rect().bottomRight() - QPoint(2, 2)
        QTest.mousePress(border, Qt.MouseButton.LeftButton, pos=pos)
        QTest.mouseMove(border, pos + QPoint(35, 20))
        QTest.mouseRelease(border, Qt.MouseButton.LeftButton)
        self.assertEqual(window.width(), previous_size.width() + 35)
        self.assertEqual(window.height(), previous_size.height() + 20)

        QTest.mouseDClick(bar, Qt.MouseButton.LeftButton, pos=QPoint(50, 20))
        self.assertTrue(window.isMaximized())
        bar.minimize_button.click()
        self.assertTrue(window.isMinimized())
        window.showNormal()
        bar.close_button.click()
        self.assertFalse(window.isVisible())
