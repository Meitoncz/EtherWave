"""
EtherWave Server - PySide6 GUI

Lets the user pick a surround channel layout, start/stop the virtual sink +
UDP stream + LAN discovery beacon, and watch live per-channel VU meters and a
status log. All audio/network work happens on QThreads (audio_engine.py,
discovery.py); this module only ever touches Qt signals from the main thread.
"""

import socket
import sys
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, QDateTime, QSettings
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QComboBox, QPushButton, QSpinBox, QProgressBar, QPlainTextEdit,
    QFormLayout, QSizePolicy, QSystemTrayIcon, QMenu, QStyle, QApplication,
    QMessageBox,
)

from audio_engine import AUDIO_PORT, AudioCaptureThread, PipeWireSinkManager
from discovery import DiscoveryBroadcaster


def _find_asset_path(filename: str) -> str:
    """Locates a file under assets/ whether running from source, frozen via
    PyInstaller, or installed as a system package (Arch PKGBUILD).

    Every real deployment puts assets/ as a sibling of this file's own
    directory: a source checkout has server/ and assets/ side by side,
    PyInstaller's _MEIPASS bundles them together, and PKGBUILD's
    `cp -r assets ...` installs them next to server/ under
    /usr/share/etherwave-server/. No other system path is ever actually
    used by this project's packaging, so there's nothing else to guess.
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


def _tray_icon_path_for_scheme(scheme) -> str:
    """Picks the monochrome tray icon variant for the current system color
    scheme: tray icons are conventionally simple silhouettes that adapt to
    the panel's theme rather than a fixed-color badge (unlike the window/
    dock icon, which stays the full-color brand mark). Defaults to the
    white variant when the platform can't report a scheme (Qt.ColorScheme.
    Unknown, e.g. some window managers/offscreen) since dark panels are the
    more common default across Linux desktop environments."""
    variant = "black" if scheme == Qt.ColorScheme.Light else "white"
    path = _find_asset_path(f"icon_tray_{variant}.png")
    return path or _find_icon_path()

CHANNEL_LAYOUTS = OrderedDict([
    (2, "Stereo (2.0)"),
    (3, "2.1 Surround (3ch)"),
    (4, "4.0 Quad (4ch)"),
    (6, "5.1 Surround (6ch)"),
    (8, "7.1 Surround (8ch)"),
])

# Persisted across runs via QSettings (ini file). Add more keys here as new
# settings are introduced -- no other scaffolding is needed.
SETTINGS_ORG = "EtherWave"
SETTINGS_APP = "Server"
DEFAULT_CHANNELS = 6
DEFAULT_PACKET_SIZE = 240


class VUMeter(QProgressBar):
    """A single-channel level meter, 0-100 mapped from a 0.0-1.0 peak value."""

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
    """A row of VUMeter widgets, rebuilt whenever the channel count changes."""

    CHANNEL_LABELS = {
        2: ["L", "R"],
        3: ["L", "R", "LFE"],
        4: ["L", "R", "RL", "RR"],
        6: ["L", "R", "C", "LFE", "RL", "RR"],
        8: ["L", "R", "C", "LFE", "RL", "RR", "SL", "SR"],
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._meters = []
        self.set_channels(2)

    def set_channels(self, channels: int):
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._meters = []
        labels = self.CHANNEL_LABELS.get(channels, [str(i + 1) for i in range(channels)])
        for label in labels:
            meter = VUMeter(label)
            self._meters.append(meter)
            self._layout.addWidget(meter)

    def update_levels(self, levels):
        for meter, peak in zip(self._meters, levels):
            meter.set_peak(float(peak))


class ServerMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EtherWave Server")
        self.resize(560, 480)

        # Without an explicit window icon, Wayland compositors (e.g. KDE
        # Plasma) have nothing to show in the taskbar/dock for this window
        # and fall back to a generic placeholder icon -- this, together
        # with QApplication.setDesktopFileName() in main.py, is what lets
        # the compositor resolve the real app icon there.
        app_icon_path = _find_icon_path()
        if app_icon_path:
            self.setWindowIcon(QIcon(app_icon_path))

        self.settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                                   SETTINGS_ORG, SETTINGS_APP)

        self.sink_manager = PipeWireSinkManager()
        self.capture_thread = None

        self._build_ui()
        self._load_settings()
        self._quitting = False
        self._setup_tray_icon()
        self._set_running_state(False)

        # The discovery beacon broadcasts from app launch, not just while
        # streaming, so clients can find and pre-connect to this server (see
        # ClientMainWindow's auto-connect toggle) before any audio flows.
        self.broadcaster = DiscoveryBroadcaster(
            server_name=socket.gethostname(),
            audio_port=AUDIO_PORT,
            channels=self.channel_combo.currentData(),
            streaming=False,
        )
        self.broadcaster.status_changed.connect(self._log)
        self.broadcaster.error_occurred.connect(self._log)
        self.broadcaster.start()
        self.channel_combo.currentIndexChanged.connect(self._on_channel_layout_changed)
        self.channel_combo.currentIndexChanged.connect(self._save_settings)
        self.blocksize_spin.valueChanged.connect(self._save_settings)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        config_box = QGroupBox("Stream Configuration")
        form = QFormLayout(config_box)

        self.channel_combo = QComboBox()
        for channels, label in CHANNEL_LAYOUTS.items():
            self.channel_combo.addItem(label, channels)
        self.channel_combo.setCurrentIndex(3)  # default 5.1
        form.addRow("Channel Layout:", self.channel_combo)

        self.blocksize_spin = QSpinBox()
        self.blocksize_spin.setRange(64, 1920)
        self.blocksize_spin.setSingleStep(16)
        self.blocksize_spin.setValue(240)
        self.blocksize_spin.setSuffix(" frames")
        form.addRow("Packet Size:", self.blocksize_spin)

        self.latency_label = QLabel()
        form.addRow("Approx. Latency:", self.latency_label)
        self.blocksize_spin.valueChanged.connect(self._update_latency_estimate)
        self._update_latency_estimate()

        root.addWidget(config_box)

        control_row = QHBoxLayout()
        self.start_stop_button = QPushButton("Start Streaming")
        self.start_stop_button.clicked.connect(self._toggle_streaming)
        control_row.addWidget(self.start_stop_button)
        self.status_label = QLabel("Idle")
        self.status_label.setStyleSheet("color: #888;")
        control_row.addWidget(self.status_label, stretch=1)
        root.addLayout(control_row)

        meter_box = QGroupBox("Levels")
        meter_layout = QVBoxLayout(meter_box)
        self.vu_panel = VUMeterPanel()
        meter_layout.addWidget(self.vu_panel)
        root.addWidget(meter_box, stretch=1)

        self.stats_label = QLabel("Packets sent: 0    Data sent: 0.0 MB")
        self.stats_label.setStyleSheet("color: #888;")
        root.addWidget(self.stats_label)

        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        log_layout.addWidget(self.log_view)
        root.addWidget(log_box, stretch=1)

    def _load_settings(self):
        channels = self.settings.value("stream/channels", DEFAULT_CHANNELS, type=int)
        index = self.channel_combo.findData(channels)
        if index >= 0:
            self.channel_combo.setCurrentIndex(index)

        packet_size = self.settings.value("stream/packet_size", DEFAULT_PACKET_SIZE, type=int)
        self.blocksize_spin.setValue(packet_size)

    def _save_settings(self):
        self.settings.setValue("stream/channels", self.channel_combo.currentData())
        self.settings.setValue("stream/packet_size", self.blocksize_spin.value())
        self.settings.sync()

    def _on_channel_layout_changed(self):
        self.broadcaster.update_stream_info(
            audio_port=AUDIO_PORT, channels=self.channel_combo.currentData()
        )

    def _update_latency_estimate(self):
        frames = self.blocksize_spin.value()
        ms = frames / 48000.0 * 1000.0
        self.latency_label.setText(f"~{ms:.1f} ms per packet")

    def _log(self, message: str):
        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss")
        self.log_view.appendPlainText(f"[{timestamp}] {message}")

    def _set_running_state(self, running: bool):
        self.channel_combo.setEnabled(not running)
        self.blocksize_spin.setEnabled(not running)
        self.start_stop_button.setText("Stop Streaming" if running else "Start Streaming")
        self.tray_pause_action.setText("Pause stream" if running else "Resume stream")
        self.tray_pause_action.setEnabled(True)

    def _setup_tray_icon(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setToolTip("EtherWave Server")
        self._update_tray_icon()

        style_hints = QApplication.instance().styleHints()
        style_hints.colorSchemeChanged.connect(self._update_tray_icon)

        menu = QMenu()
        open_action = menu.addAction("Open EtherWave")
        open_action.triggered.connect(self._show_from_tray)

        # Label reflects state and toggles the same start/stop logic as the
        # main window's button; kept in sync in _set_running_state().
        self.tray_pause_action = menu.addAction("Pause stream")
        self.tray_pause_action.triggered.connect(self._toggle_streaming)
        self.tray_pause_action.setEnabled(False)  # enabled once _set_running_state runs

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
        # History: QMessageBox.about()'s modal exec() segfaulted on this
        # KDE/Wayland compositor (SEGV_ACCERR in the destructor, right where
        # exec() releases its modal grab). Switching to a non-modal .show()
        # fixed the crash but re-showing the SAME previously-closed instance
        # on a second click silently failed to reappear -- KWin apparently
        # doesn't reliably remap a toplevel that was already hidden once.
        # Building a fresh QMessageBox every time sidesteps that: each
        # "About" click gets a brand-new toplevel, and WA_DeleteOnClose lets
        # Qt clean it up itself once the user closes it, instead of us
        # having to track and reuse an instance.
        box = QMessageBox(self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setWindowTitle("About EtherWave Server")
        box.setText(
            "<h3>EtherWave Server</h3>"
            f"<p>Version {APP_VERSION}</p>"
            "<p>Ultra-low-latency multichannel audio streaming from a "
            "CachyOS/PipeWire server to a LAN client.</p>"
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

    def _toggle_streaming(self):
        if self.capture_thread is not None:
            self._stop_streaming()
        else:
            self._start_streaming()

    def _start_streaming(self):
        channels = self.channel_combo.currentData()
        blocksize = self.blocksize_spin.value()

        try:
            self.sink_manager.create_sink(channels)
            self._log(f"Created PipeWire sink '{self.sink_manager.SINK_NAME}' ({channels}ch)")
        except Exception as exc:
            self._log(f"ERROR: failed to create virtual sink: {exc}")
            self.status_label.setText("Error creating sink")
            self.status_label.setStyleSheet("color: #e74c3c;")
            return

        self.vu_panel.set_channels(channels)

        self.capture_thread = AudioCaptureThread(
            channels=channels,
            sink_name=self.sink_manager.SINK_NAME,
            blocksize=blocksize,
        )
        self.capture_thread.levels_changed.connect(self.vu_panel.update_levels)
        self.capture_thread.status_changed.connect(self._log)
        self.capture_thread.error_occurred.connect(self._on_capture_error)
        self.capture_thread.stats_updated.connect(self._on_stats_updated)
        self.capture_thread.start()

        self.broadcaster.update_stream_info(audio_port=AUDIO_PORT, channels=channels)
        self.broadcaster.set_streaming(True)

        server_name = socket.gethostname()
        self.status_label.setText(f"Streaming as '{server_name}'")
        self.status_label.setStyleSheet("color: #2ecc71;")
        self._set_running_state(True)

    def _stop_streaming(self):
        if self.capture_thread is not None:
            self.capture_thread.stop()
            self.capture_thread.wait(2000)
            self.capture_thread = None

        # Keep broadcasting presence (just with streaming=False) rather than
        # stopping the thread, so the server stays discoverable/auto-
        # connectable between streaming sessions.
        self.broadcaster.set_streaming(False)

        try:
            self.sink_manager.remove_sink()
            self._log("Virtual sink removed, streaming stopped")
            status_text, status_color = "Idle", "#888"
        except RuntimeError as exc:
            self._log(f"ERROR: {exc}")
            status_text, status_color = "Error removing sink (see log)", "#e74c3c"

        self.vu_panel.update_levels([0.0] * len(self.vu_panel._meters))
        self.status_label.setText(status_text)
        self.status_label.setStyleSheet(f"color: {status_color};")
        self._set_running_state(False)

    def _on_capture_error(self, message: str):
        self._log(f"ERROR: {message}")
        self.status_label.setText("Error")
        self.status_label.setStyleSheet("color: #e74c3c;")
        self._stop_streaming()

    def _on_stats_updated(self, packets_sent: int, bytes_sent: int):
        mb = bytes_sent / (1024 * 1024)
        self.stats_label.setText(f"Packets sent: {packets_sent}    Data sent: {mb:.2f} MB")

    def closeEvent(self, event):
        if not self._quitting:
            # Closing the window just minimizes to tray; use the tray
            # menu's "Close" to actually quit and stop streaming.
            event.ignore()
            self.hide()
            return

        self._stop_streaming()
        self.broadcaster.stop()
        self.broadcaster.wait(2000)
        self.tray_icon.hide()
        super().closeEvent(event)
