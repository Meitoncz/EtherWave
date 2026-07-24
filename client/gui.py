"""
EtherWave Client - PySide6 GUI

Shows auto-discovered EtherWave servers on the LAN, an output device picker
(sounddevice/CoreAudio devices), a jitter buffer slider (5-50ms), and live VU
meters for the received/remapped audio. Networking and audio I/O run on
QThreads / sounddevice's own callback thread (discovery.py, audio_player.py);
this module only ever touches Qt signals from the main thread.
"""

import sys
from pathlib import Path

import sounddevice as sd
from PySide6.QtCore import Qt, QDateTime, QSettings, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QComboBox, QPushButton, QSlider, QProgressBar, QPlainTextEdit,
    QFormLayout, QListWidget, QListWidgetItem, QSizePolicy, QCheckBox,
    QSystemTrayIcon, QMenu, QStyle, QApplication, QMessageBox, QSpinBox,
)

from audio_player import (
    JitterBuffer, NetworkReceiveThread, AudioOutputStream,
    DEFAULT_JITTER_MS, MIN_JITTER_MS, MAX_JITTER_MS,
    MIN_CHANNEL_GAIN_DB, MAX_CHANNEL_GAIN_DB,
)
from discovery import DiscoveryListener

# Persisted across runs via QSettings (ini file). Add more keys here as new
# settings are introduced -- no other scaffolding is needed.
SETTINGS_ORG = "EtherWave"
SETTINGS_APP = "Client"

def _find_asset_path(filename: str) -> str:
    """Locates a file under assets/ whether running from source or frozen
    via PyInstaller (macOS .app bundle).

    Every real deployment puts assets/ as a sibling of this file's own
    directory: a source checkout has client/ and assets/ side by side, and
    PyInstaller's _MEIPASS bundles them together (see
    packaging/macos/EtherWaveClient.spec's datas=). No other path is ever
    actually used by this project's packaging, so there's nothing else to
    guess.
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / filename)
    here = Path(__file__).resolve().parent
    candidates.append(here.parent / "assets" / filename)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def _find_icon_path() -> str:
    return _find_asset_path("icon.png")


def _read_app_version() -> str:
    """Reads assets/VERSION, the single source of truth patched by
    .github/workflows/release.yml at release time (alongside PKGBUILD's
    pkgver and the macOS .app's CFBundleShortVersionString) so this,
    packaging/arch/PKGBUILD, and packaging/macos/EtherWaveClient.spec never
    drift out of sync with each other or with the git tag."""
    path = _find_asset_path("VERSION")
    if not path:
        return "dev"
    try:
        return Path(path).read_text().strip() or "dev"
    except OSError:
        return "dev"


APP_VERSION = _read_app_version()


def _format_data_size(num_bytes) -> str:
    """Formats a byte count as MB, switching to GB past 1000 MB -- kept in
    sync by hand with server/gui.py's copy of this same function, the same
    way the wire protocol constants are (see CLAUDE.md): no shared module
    exists between the two independently-deployed apps."""
    mb = num_bytes / (1024 * 1024)
    if mb >= 1000:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.2f} MB"


def _tray_icon_path_for_scheme(scheme) -> str:
    """Picks the monochrome tray icon variant for the current system color
    scheme: tray/menu-bar icons are conventionally simple silhouettes that
    adapt to the panel's theme rather than a fixed-color badge (unlike the
    window/dock icon, which stays the full-color brand mark). Defaults to
    the white variant when the platform can't report a scheme
    (Qt.ColorScheme.Unknown)."""
    variant = "black" if scheme == Qt.ColorScheme.Light else "white"
    path = _find_asset_path(f"icon_tray_{variant}.png")
    return path or _find_icon_path()


class VUMeter(QProgressBar):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setRange(0, 100)
        self.setValue(0)
        self.setTextVisible(True)
        self.setFormat(label)
        self.setOrientation(Qt.Vertical)
        self.setMinimumHeight(140)
        self.setMinimumWidth(28)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setStyleSheet(
            "QProgressBar { background-color: #1a1a1a; border: 1px solid #333; "
            "border-radius: 3px; color: #ccc; text-align: center; }"
            "QProgressBar::chunk { background-color: qlineargradient("
            "x1:0, y1:1, x2:0, y2:0, stop:0 #2ecc71, stop:0.7 #f1c40f, stop:1 #e74c3c); "
            "border-radius: 2px; }"
        )

    def set_peak(self, peak: float):
        self.setValue(max(0, min(100, int(peak * 100))))


class VUMeterPanel(QWidget):
    """A row of [VU meter + per-channel dB gain spinbox] columns, rebuilt
    (and gain reset to 0 dB) whenever the channel count changes -- e.g. on
    every new connection, since channel count/meaning depends on which
    server/output device is in play."""

    gain_changed = Signal(int, float)  # channel_index, db

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._meters = []
        self._gain_spins = []
        self.set_channels(2)

    def set_channels(self, channels: int):
        while self._layout.count():
            item = self._layout.takeAt(0)
            item.widget().deleteLater()
        self._meters = []
        self._gain_spins = []
        for i in range(channels):
            label = str(i + 1)
            # A plain QWidget (not a bare QVBoxLayout) so Qt's parent/child
            # ownership cascades deleteLater() to the meter and spinbox
            # automatically on the next set_channels() call, instead of
            # needing to walk into each column by hand to clear it.
            column = QWidget()
            column_layout = QVBoxLayout(column)
            column_layout.setContentsMargins(0, 0, 0, 0)

            meter = VUMeter(label)
            self._meters.append(meter)
            column_layout.addWidget(meter)

            gain_spin = QSpinBox()
            gain_spin.setRange(MIN_CHANNEL_GAIN_DB, MAX_CHANNEL_GAIN_DB)
            gain_spin.setValue(0)
            gain_spin.setSuffix(" dB")
            gain_spin.setToolTip(f"Channel {label} volume trim")
            gain_spin.setMaximumWidth(70)
            gain_spin.setAlignment(Qt.AlignCenter)
            gain_spin.valueChanged.connect(
                lambda db, idx=i: self.gain_changed.emit(idx, float(db))
            )
            self._gain_spins.append(gain_spin)
            column_layout.addWidget(gain_spin, alignment=Qt.AlignHCenter)

            self._layout.addWidget(column)

    def gains_db(self) -> list:
        return [spin.value() for spin in self._gain_spins]

    def set_gains_db(self, gains):
        for spin, db in zip(self._gain_spins, gains):
            spin.setValue(int(db))

    def update_levels(self, levels):
        for meter, peak in zip(self._meters, levels):
            meter.set_peak(float(peak))


def list_output_devices():
    """Returns [(device_index, display_name, max_output_channels), ...]."""
    devices = []
    try:
        for index, info in enumerate(sd.query_devices()):
            if info.get("max_output_channels", 0) > 0:
                devices.append((index, info["name"], info["max_output_channels"]))
    except Exception:
        pass
    return devices


class ClientMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EtherWave Client")
        self.resize(600, 560)

        # Without an explicit window icon, Wayland compositors have nothing
        # to show in the taskbar/dock for this window and fall back to a
        # generic placeholder (on macOS the .app bundle's .icns already
        # covers the Dock icon, so this mainly matters when running from
        # source on Linux).
        app_icon_path = _find_icon_path()
        if app_icon_path:
            self.setWindowIcon(QIcon(app_icon_path))

        self.settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                                   SETTINGS_ORG, SETTINGS_APP)

        self.servers = {}
        self.connected_ip = None
        self._connected_output_channels = None
        self._connected_source_channels = None
        self.jitter_buffer = None
        self.receive_thread = None
        self.output_stream = None
        self._auto_connecting = False

        self._build_ui()
        self._refresh_output_devices()
        self._load_settings()

        self.jitter_slider.valueChanged.connect(self._save_settings)
        self.auto_connect_checkbox.toggled.connect(self._save_settings)
        self.device_combo.currentIndexChanged.connect(self._save_settings)

        self._quitting = False
        self._setup_tray_icon()

        self.discovery_listener = DiscoveryListener()
        self.discovery_listener.servers_updated.connect(self._on_servers_updated)
        self.discovery_listener.error_occurred.connect(self._log)
        self.discovery_listener.start()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        server_box = QGroupBox("Discovered EtherWave Servers")
        server_layout = QVBoxLayout(server_box)
        self.server_list = QListWidget()
        server_layout.addWidget(self.server_list)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._toggle_connection)
        server_layout.addWidget(self.connect_button)
        self.auto_connect_checkbox = QCheckBox(
            "Auto-connect to first detected server (even before it starts streaming)"
        )
        self.auto_connect_checkbox.toggled.connect(self._on_auto_connect_toggled)
        server_layout.addWidget(self.auto_connect_checkbox)
        root.addWidget(server_box)

        config_box = QGroupBox("Playback Configuration")
        form = QFormLayout(config_box)

        self.device_combo = QComboBox()
        form.addRow("Output Device:", self.device_combo)

        self.jitter_slider = QSlider(Qt.Horizontal)
        self.jitter_slider.setRange(MIN_JITTER_MS, MAX_JITTER_MS)
        self.jitter_slider.setValue(DEFAULT_JITTER_MS)
        self.jitter_slider.valueChanged.connect(self._on_jitter_changed)
        self.jitter_label = QLabel(f"{DEFAULT_JITTER_MS} ms")
        jitter_row = QHBoxLayout()
        jitter_row.addWidget(self.jitter_slider)
        jitter_row.addWidget(self.jitter_label)
        form.addRow("Jitter Buffer:", jitter_row)

        root.addWidget(config_box)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("color: #888;")
        status_row.addWidget(self.status_label, stretch=1)
        root.addLayout(status_row)

        meter_box = QGroupBox("Levels")
        meter_layout = QVBoxLayout(meter_box)
        self.vu_panel = VUMeterPanel()
        self.vu_panel.gain_changed.connect(self._on_channel_gain_changed)
        meter_layout.addWidget(self.vu_panel)
        root.addWidget(meter_box, stretch=1)

        self.stats_label = QLabel("Latency: -- ms    Packets: 0    Underruns: 0    Resyncs: 0")
        self.stats_label.setStyleSheet("color: #888;")
        root.addWidget(self.stats_label)

        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        log_layout.addWidget(self.log_view)
        root.addWidget(log_box, stretch=1)

    def _refresh_output_devices(self):
        self.device_combo.clear()
        for index, name, max_channels in list_output_devices():
            self.device_combo.addItem(f"{name} (max {max_channels}ch)", (index, max_channels, name))
        if self.device_combo.count() == 0:
            self.device_combo.addItem("No output devices found", None)

    def _log(self, message: str):
        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss")
        self.log_view.appendPlainText(f"[{timestamp}] {message}")

    def _load_settings(self):
        jitter_ms = self.settings.value("playback/jitter_ms", DEFAULT_JITTER_MS, type=int)
        jitter_ms = max(MIN_JITTER_MS, min(MAX_JITTER_MS, jitter_ms))
        self.jitter_slider.setValue(jitter_ms)

        auto_connect = self.settings.value("discovery/auto_connect", False, type=bool)
        self.auto_connect_checkbox.setChecked(auto_connect)

        device_name = self.settings.value("playback/output_device", "", type=str)
        if device_name:
            for i in range(self.device_combo.count()):
                data = self.device_combo.itemData(i)
                if data is not None and data[2] == device_name:
                    self.device_combo.setCurrentIndex(i)
                    break
            else:
                self._log(f"Saved output device '{device_name}' not found, using default")

    def _save_settings(self):
        self.settings.setValue("playback/jitter_ms", self.jitter_slider.value())
        self.settings.setValue("discovery/auto_connect", self.auto_connect_checkbox.isChecked())
        device_data = self.device_combo.currentData()
        if device_data is not None:
            self.settings.setValue("playback/output_device", device_data[2])
        self.settings.sync()

    def _on_channel_gain_changed(self, channel_index: int, db: float):
        if self.output_stream is not None:
            self.output_stream.set_channel_gain_db(channel_index, db)
        gains = self.vu_panel.gains_db()
        self.settings.setValue(f"playback/channel_gains_{len(gains)}ch",
                                ",".join(str(g) for g in gains))
        self.settings.sync()

    def _load_channel_gains(self, channels: int):
        saved = self.settings.value(f"playback/channel_gains_{channels}ch", "", type=str)
        if not saved:
            return
        try:
            gains = [int(x) for x in saved.split(",")]
        except ValueError:
            return
        if len(gains) == channels:
            self.vu_panel.set_gains_db(gains)

    def _on_servers_updated(self, servers: dict):
        previous_selection = None
        current_item = self.server_list.currentItem()
        if current_item is not None:
            previous_selection = current_item.data(Qt.UserRole)

        new_ips = set(servers.keys())
        old_ips = set(self.servers.keys())
        unchanged = new_ips == old_ips and all(
            servers[ip].get("channels") == self.servers[ip].get("channels")
            and servers[ip].get("streaming") == self.servers[ip].get("streaming")
            for ip in new_ips
        )
        if unchanged:
            self.servers = servers
            return

        self.servers = servers
        self.server_list.clear()
        for ip, info in sorted(servers.items(), key=lambda kv: kv[1].get("name", "")):
            state = "streaming" if info.get("streaming") else "idle"
            label = (
                f"{info.get('name', 'Unknown')} — {ip} — "
                f"{info.get('channels')}ch @ {info.get('sample_rate')}Hz — {state}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, ip)
            self.server_list.addItem(item)
            if ip == previous_selection:
                self.server_list.setCurrentItem(item)

        if self.connected_ip and self.connected_ip not in servers:
            self._log(f"Server {self.connected_ip} disappeared from the network")

        if self.connected_ip in servers:
            new_channels = int(servers[self.connected_ip].get("channels", 0))
            if new_channels != self._connected_source_channels:
                # The server's channel layout changed after we connected --
                # e.g. auto-connected while idle, then the layout was
                # changed before streaming started. The jitter buffer and
                # output stream are sized for the old count, so a clean
                # reconnect is needed to pick up the new one.
                self._log(
                    f"Server {self.connected_ip} channel count changed "
                    f"({self._connected_source_channels} -> {new_channels}ch), reconnecting"
                )
                ip_to_rejoin = self.connected_ip
                self._disconnect()
                self._connect(ip=ip_to_rejoin)
            else:
                self._update_connection_status()

        self._maybe_auto_connect()

    def _on_jitter_changed(self, value: int):
        self.jitter_label.setText(f"{value} ms")
        if self.jitter_buffer is not None:
            self.jitter_buffer.set_jitter_ms(value)

    def _toggle_connection(self):
        if self.connected_ip is not None:
            self._disconnect()
        else:
            self._connect()

    def _setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setToolTip("EtherWave Client")
        self._update_tray_icon()

        style_hints = QApplication.instance().styleHints()
        style_hints.colorSchemeChanged.connect(self._update_tray_icon)

        menu = QMenu()
        open_action = menu.addAction("Open EtherWave")
        open_action.triggered.connect(self._show_from_tray)

        # Label reflects connection state and toggles the same
        # connect/disconnect logic as the main window's button; kept in
        # sync in _connect()/_disconnect().
        self.tray_pause_action = menu.addAction("Resume stream")
        self.tray_pause_action.triggered.connect(self._toggle_connection)

        menu.addSeparator()
        about_action = menu.addAction("About")
        about_action.triggered.connect(self._show_about)
        close_action = menu.addAction("Close")
        close_action.triggered.connect(self._quit_from_tray)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _update_tray_icon(self):
        scheme = QApplication.instance().styleHints().colorScheme()
        icon_path = _tray_icon_path_for_scheme(scheme)
        if icon_path:
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            self.tray_icon.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume))

    def _show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_about(self):
        # A fresh QMessageBox on every click, with WA_DeleteOnClose to let
        # Qt clean it up itself -- see server/gui.py's _show_about for the
        # full history (modal exec() segfaulted on KDE/Wayland; a reused
        # non-modal instance stopped crashing but silently failed to
        # reappear on a second click). Applying the same pattern here for
        # consistency, since both windows share the identical tray-menu
        # About code path.
        box = QMessageBox(self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setWindowTitle("About EtherWave Client")
        box.setText(
            "<h3>EtherWave Client</h3>"
            f"<p>Version {APP_VERSION}</p>"
            "<p>Ultra-low-latency multichannel audio streaming from a "
            "CachyOS/PipeWire server to this Mac.</p>"
            "<p>License: MIT</p>"
            '<p><a href="https://github.com/Meitoncz/EtherWave">'
            "github.com/Meitoncz/EtherWave</a></p>"
        )
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.show()
        box.raise_()
        box.activateWindow()

    def _quit_from_tray(self):
        self._quitting = True
        self.close()
        QApplication.instance().quit()

    def _on_auto_connect_toggled(self, checked: bool):
        if checked:
            self._maybe_auto_connect()

    def _maybe_auto_connect(self):
        if not self.auto_connect_checkbox.isChecked():
            return
        if self.connected_ip is not None or self._auto_connecting:
            return
        if not self.servers:
            return
        first_ip = next(iter(self.servers))
        self._auto_connecting = True
        try:
            self._connect(ip=first_ip)
        finally:
            self._auto_connecting = False

    def _connect(self, ip=None):
        if ip is None:
            item = self.server_list.currentItem()
            if item is None:
                self._log("Select a server from the list first")
                return
            ip = item.data(Qt.UserRole)
        info = self.servers.get(ip)
        if info is None:
            self._log("Selected server is no longer available")
            return

        for i in range(self.server_list.count()):
            item = self.server_list.item(i)
            if item.data(Qt.UserRole) == ip:
                self.server_list.setCurrentItem(item)
                break

        device_data = self.device_combo.currentData()
        if device_data is None:
            self._log("No output device available")
            return
        device_index, device_max_channels, _device_name = device_data

        server_channels = int(info.get("channels", 2))
        output_channels = min(server_channels, device_max_channels)

        self.jitter_buffer = JitterBuffer(
            channels=server_channels,
            jitter_ms=self.jitter_slider.value(),
        )

        self.receive_thread = NetworkReceiveThread(server_ip=ip, jitter_buffer=self.jitter_buffer)
        self.receive_thread.stats_updated.connect(self._on_stats_updated)
        self.receive_thread.error_occurred.connect(self._log)
        self.receive_thread.start()

        self.output_stream = AudioOutputStream(
            device_index=device_index,
            output_channels=output_channels,
            jitter_buffer=self.jitter_buffer,
        )
        self.output_stream.levels_changed.connect(self.vu_panel.update_levels)
        self.output_stream.error_occurred.connect(self._log)
        self.vu_panel.set_channels(output_channels)
        self._load_channel_gains(output_channels)
        self.output_stream.start()

        self.connected_ip = ip
        self._connected_output_channels = output_channels
        self._connected_source_channels = server_channels
        self._update_connection_status()
        self.connect_button.setText("Disconnect")
        self.tray_pause_action.setText("Pause stream")
        self.device_combo.setEnabled(False)
        self._log(f"Connected to {ip}, streaming {server_channels}ch -> {output_channels}ch output")

    def _update_connection_status(self):
        info = self.servers.get(self.connected_ip)
        if info is None:
            return
        if info.get("streaming"):
            text = (
                f"Connected to {info.get('name')} ({self.connected_ip}) — "
                f"output {self._connected_output_channels}ch"
            )
        else:
            text = f"Connected to {info.get('name')} ({self.connected_ip}) — waiting for stream to start"
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #2ecc71;")

    def _disconnect(self):
        if self.receive_thread is not None:
            self.receive_thread.stop()
            self.receive_thread.wait(2000)
            self.receive_thread = None
        if self.output_stream is not None:
            self.output_stream.stop()
            self.output_stream = None
        self.jitter_buffer = None

        self.connected_ip = None
        self._connected_source_channels = None
        self.status_label.setText("Not connected")
        self.status_label.setStyleSheet("color: #888;")
        self.connect_button.setText("Connect")
        self.tray_pause_action.setText("Resume stream")
        self.device_combo.setEnabled(True)
        self.vu_panel.update_levels([0.0] * len(self.vu_panel._meters))
        self._log("Disconnected")

    def _on_stats_updated(self, buffered_ms: float, packets_received: int, bytes_received: int):
        underruns = self.jitter_buffer.underruns if self.jitter_buffer else 0
        resyncs = self.jitter_buffer.resyncs if self.jitter_buffer else 0
        self.stats_label.setText(
            f"Buffered: {buffered_ms:.1f} ms    Packets: {packets_received}    "
            f"Received: {_format_data_size(bytes_received)}    "
            f"Underruns: {underruns}    Resyncs: {resyncs}"
        )

    def closeEvent(self, event):
        if not self._quitting:
            # Closing the window just minimizes to tray; use the tray
            # menu's "Close" to actually quit and disconnect.
            event.ignore()
            self.hide()
            return

        if self.connected_ip is not None:
            self._disconnect()
        self.discovery_listener.stop()
        self.discovery_listener.wait(2000)
        self.tray_icon.hide()
        super().closeEvent(event)
