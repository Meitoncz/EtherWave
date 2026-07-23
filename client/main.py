"""EtherWave Client - entry point."""

import sys

from PySide6.QtWidgets import QApplication

from gui import ClientMainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("EtherWave Client")
    app.setQuitOnLastWindowClosed(False)  # keep running in the tray when the window is hidden
    window = ClientMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
