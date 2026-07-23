"""EtherWave Server - entry point."""

import sys

from PySide6.QtWidgets import QApplication

from gui import ServerMainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("EtherWave Server")
    app.setQuitOnLastWindowClosed(False)  # keep running in the tray when the window is hidden
    window = ServerMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
