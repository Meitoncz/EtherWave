"""
EtherWave Client - Discovery Listener

Listens for UDP broadcast beacons from EtherWave servers on the LAN and
maintains a live list of currently-active servers, pruning any that haven't
been heard from recently. Runs in its own QThread.

DISCOVERY_PORT and the JSON schema must match server/discovery.py.
"""

import json
import socket
import time

from PySide6.QtCore import QThread, Signal

DISCOVERY_PORT = 51234
SERVICE_ID = "EtherWave"
SERVER_TIMEOUT_SECONDS = 6.0


class DiscoveryListener(QThread):
    """Emits the current dict of {ip: server_info} whenever it changes or a
    beacon is (not) received, so the GUI can keep its server list fresh."""

    servers_updated = Signal(dict)
    error_occurred = Signal(str)

    def __init__(self, timeout: float = SERVER_TIMEOUT_SECONDS, parent=None):
        super().__init__(parent)
        self.timeout = timeout
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", DISCOVERY_PORT))
            sock.settimeout(1.0)
        except OSError as exc:
            self.error_occurred.emit(f"Failed to bind discovery socket: {exc}")
            return

        servers = {}
        try:
            while not self._stop_flag:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    pass
                else:
                    try:
                        info = json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        info = None
                    if info and info.get("service") == SERVICE_ID:
                        info["ip"] = addr[0]
                        info["last_seen"] = time.time()
                        servers[addr[0]] = info

                now = time.time()
                stale_ips = [ip for ip, info in servers.items()
                             if now - info["last_seen"] > self.timeout]
                changed = bool(stale_ips)
                for ip in stale_ips:
                    del servers[ip]

                if changed or not self._stop_flag:
                    self.servers_updated.emit(dict(servers))
        finally:
            sock.close()
