"""EtherWave Client - entry point."""

import sys

from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from gui import ClientMainWindow

# QLocalServer/QLocalSocket (stock PySide6 QtNetwork, no extra dependency)
# double as a single-instance guard -- see server/main.py's copy of this
# same mechanism for the full rationale (identical pattern, kept in sync
# by hand like everything else shared between the two apps; see CLAUDE.md).
SINGLE_INSTANCE_KEY = "EtherWaveClientSingleInstance"


def _another_instance_is_running() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(SINGLE_INSTANCE_KEY)
    if socket.waitForConnected(200):
        socket.disconnectFromServer()
        return True
    return False


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("EtherWave Client")
    app.setQuitOnLastWindowClosed(False)  # keep running in the tray when the window is hidden

    if _another_instance_is_running():
        return

    window = ClientMainWindow()

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
