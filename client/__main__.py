import sys

from PySide6.QtWidgets import QApplication

from .echo_window import EchoWindow


def main() -> int:
    app = QApplication(sys.argv)
    window = EchoWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
