"""
EtherWave Server (Windows) - Discovery Broadcaster

Periodically announces this server's presence on the LAN via UDP broadcast so
clients can auto-discover it without any manual IP entry. Runs in its own
QThread so it never blocks audio streaming or the GUI event loop.

Pure socket/JSON code, no OS-specific calls -- identical to
server/discovery.py (confirmed portable as-is when this port was planned;
see docs/WINDOWS_PORT.md). DISCOVERY_PORT and the JSON schema below must
match client/discovery.py.
"""

import json
import socket
import threading
import time

from PySide6.QtCore import QThread, Signal

DISCOVERY_PORT = 51234
BROADCAST_ADDRESS = "255.255.255.255"
SERVICE_ID = "EtherWave"
BROADCAST_INTERVAL_SECONDS = 2.0
PROTOCOL_VERSION = 1


class DiscoveryBroadcaster(QThread):
    """Broadcasts a small JSON beacon advertising this server every few seconds."""

    status_changed = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, server_name: str, audio_port: int, channels: int,
                 sample_rate: int = 48000, streaming: bool = False,
                 interval: float = BROADCAST_INTERVAL_SECONDS, parent=None):
        super().__init__(parent)
        self.server_name = server_name
        self.interval = interval
        self._lock = threading.Lock()
        self._audio_port = audio_port
        self._channels = channels
        self._sample_rate = sample_rate
        self._streaming = streaming
        self._stop_flag = False

    def update_stream_info(self, audio_port: int, channels: int, sample_rate: int = 48000):
        with self._lock:
            self._audio_port = audio_port
            self._channels = channels
            self._sample_rate = sample_rate

    def set_streaming(self, streaming: bool):
        with self._lock:
            self._streaming = streaming

    def stop(self):
        self._stop_flag = True

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError as exc:
            self.error_occurred.emit(f"Failed to open discovery socket: {exc}")
            return

        self.status_changed.emit("Broadcasting presence on LAN")
        try:
            while not self._stop_flag:
                with self._lock:
                    payload = json.dumps({
                        "service": SERVICE_ID,
                        "version": PROTOCOL_VERSION,
                        "name": self.server_name,
                        "audio_port": self._audio_port,
                        "channels": self._channels,
                        "sample_rate": self._sample_rate,
                        "streaming": self._streaming,
                        "timestamp": time.time(),
                    }).encode("utf-8")
                try:
                    sock.sendto(payload, (BROADCAST_ADDRESS, DISCOVERY_PORT))
                except OSError as exc:
                    self.error_occurred.emit(f"Discovery broadcast failed: {exc}")

                slept = 0.0
                while slept < self.interval and not self._stop_flag:
                    self.msleep(100)
                    slept += 0.1
        finally:
            sock.close()
            self.status_changed.emit("Discovery broadcast stopped")
