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

# Fields whose value actually matters to a listener. Everything else in a
# beacon (its timestamp, and the last_seen we stamp on arrival) changes on
# every single broadcast, so comparing whole beacons would report a change
# twice a second forever and defeat the point of comparing at all.
SIGNIFICANT_FIELDS = ("name", "audio_port", "channels", "sample_rate", "streaming")


def _significant(info):
    return tuple(info.get(field) for field in SIGNIFICANT_FIELDS)


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
                changed = False
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
                        previous = servers.get(addr[0])
                        if previous is None or _significant(previous) != _significant(info):
                            changed = True
                        servers[addr[0]] = info

                now = time.time()
                stale_ips = [ip for ip, info in servers.items()
                             if now - info["last_seen"] > self.timeout]
                if stale_ips:
                    changed = True
                for ip in stale_ips:
                    del servers[ip]

                # Emit only when the picture actually changed. The condition
                # here used to be `changed or not self._stop_flag`, and the
                # second half is unconditionally true inside this loop, so it
                # emitted on every iteration -- at least once a second, and
                # again on every beacon -- making the `changed` bookkeeping
                # dead code. Harmless only because the GUI happened to
                # re-check for itself before touching any widgets.
                if changed:
                    self.servers_updated.emit(dict(servers))
        finally:
            sock.close()
