"""EtherWave Server - entry point."""

import sys

from PySide6.QtWidgets import QApplication

from gui import ServerMainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("EtherWave Server")
    # Matches packaging/arch/etherwave-server.desktop's basename -- lets
    # Wayland compositors (KDE Plasma etc.) resolve this running app back
    # to its .desktop entry (and therefore its Icon=) for the taskbar/dock,
    # instead of falling back to a generic placeholder icon.
    app.setDesktopFileName("etherwave-server")
    app.setQuitOnLastWindowClosed(False)  # keep running in the tray when the window is hidden
    window = ServerMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
