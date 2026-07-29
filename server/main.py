"""EtherWave Server - entry point."""

import sys

from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from gui import ServerMainWindow

# QLocalServer/QLocalSocket (stock PySide6 QtNetwork, no extra dependency)
# double as a single-instance guard: launching the app while an instance is
# already running (e.g. clicking the taskbar/dock icon while the window is
# hidden in the tray -- closing to tray, not quitting, means the OS has no
# running window to raise, so it launches a brand new process instead of
# restoring the existing one) connects to this name instead of starting a
# second GUI.
SINGLE_INSTANCE_KEY = "EtherWaveServerSingleInstance"


def _another_instance_is_running() -> bool:
    """A live connection attempt is a direct liveness check -- unlike a
    lockfile/pidfile, there's nothing to go stale after a crash: if nothing
    answers, nothing is running, full stop."""
    socket = QLocalSocket()
    socket.connectToServer(SINGLE_INSTANCE_KEY)
    if socket.waitForConnected(200):
        socket.disconnectFromServer()
        return True
    return False


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("EtherWave Server")
    # Matches packaging/arch/etherwave-server.desktop's basename -- lets
    # Wayland compositors (KDE Plasma etc.) resolve this running app back
    # to its .desktop entry (and therefore its Icon=) for the taskbar/dock,
    # instead of falling back to a generic placeholder icon.
    app.setDesktopFileName("etherwave-server")
    app.setQuitOnLastWindowClosed(False)  # keep running in the tray when the window is hidden

    if _another_instance_is_running():
        return

    window = ServerMainWindow()

    # A stale socket file can be left behind if a previous instance was
    # killed without a clean shutdown (SIGKILL, crash) -- removeServer()
    # clears it first so listen() doesn't fail against a dead socket that
    # _another_instance_is_running() above already proved has no listener.
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    local_server = QLocalServer()
    local_server.listen(SINGLE_INSTANCE_KEY)

    def _on_activation_request():
        socket = local_server.nextPendingConnection()
        if socket is not None:
            socket.disconnectFromServer()
        window._show_from_tray()

    local_server.newConnection.connect(_on_activation_request)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
