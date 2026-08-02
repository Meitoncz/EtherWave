"""
EtherWave Server (Windows) - PySide6 GUI

Lets the user pick a surround channel layout, start/stop VB-Cable default-
device switching + UDP stream + LAN discovery beacon, and watch live
per-channel VU meters and a status log. All audio/network work happens on
QThreads (audio_engine.py, discovery.py); this module only ever touches Qt
signals from the main thread.

Mirrors server/gui.py -- only the sink_manager class and AudioCaptureThread
construction differ (VB-Cable default-device switching instead of a PipeWire
null-sink); VU meters, gain spinboxes, QSettings, tray icon, About dialog,
and stats display are unchanged, since none of that is Linux-specific.
"""

import socket
import sys
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, QDateTime, QSettings, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QComboBox, QPushButton, QSpinBox, QProgressBar, QPlainTextEdit,
    QFormLayout, QSizePolicy, QSystemTrayIcon, QMenu, QStyle, QApplication,
    QMessageBox, QCheckBox,
)

import autostart
from audio_engine import (
    AUDIO_PORT, AudioCaptureThread, SubscriberRegistry,
    MIN_CHANNEL_GAIN_DB, MAX_CHANNEL_GAIN_DB,
)
from default_device import DefaultDeviceManager
from discovery import DiscoveryBroadcaster


def _find_asset_path(filename: str) -> str:
    """Locates a file under assets/ whether running from source or frozen
    via PyInstaller.

    A source checkout has server_windows/ and assets/ side by side;
    PyInstaller's _MEIPASS bundles them together. No other deployment path
    exists yet for this port (see docs/WINDOWS_PORT.md's packaging section).
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
    .github/workflows/release.yml at release time -- see CLAUDE.md."""
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
    the panel's theme rather than a fixed-color badge (unlike the window
    icon, which stays the full-color brand mark). Defaults to the white
    variant when the platform can't report a scheme."""
    variant = "black" if scheme == Qt.ColorScheme.Light else "white"
    path = _find_asset_path(f"icon_tray_{variant}.png")
    return path or _find_icon_path()


def _format_data_size(num_bytes) -> str:
    """Formats a byte count as MB, switching to GB past 1000 MB -- kept in
    sync by hand with server/gui.py's and client/gui.py's copies of this
    same function (see CLAUDE.md): no shared module exists between these
    independently-deployed apps."""
    mb = num_bytes / (1024 * 1024)
    if mb >= 1000:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.2f} MB"


def _build_stats_row(specs, column_width: int = 150):
    """Builds a caption-over-value grid of *equal-width* columns, centered
    by the caller (Qt.AlignHCenter). Kept in sync by hand with server/gui.py's
    and client/gui.py's copies of this same function.

    specs: list of (key, caption) tuples.
    Returns (container_widget, {key: value_QLabel}).
    """
    container = QWidget()
    grid = QGridLayout(container)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(24)
    grid.setVerticalSpacing(2)
    values = {}
    for col, (key, caption) in enumerate(specs):
        cap_label = QLabel(caption)
        cap_label.setAlignment(Qt.AlignCenter)
        cap_label.setFixedWidth(column_width)
        cap_label.setStyleSheet("color: #666; font-size: 10px; font-weight: bold;")
        grid.addWidget(cap_label, 0, col)

        val_label = QLabel("--")
        val_label.setAlignment(Qt.AlignCenter)
        val_label.setFixedWidth(column_width)
        val_label.setWordWrap(True)
        val_label.setStyleSheet("color: #888;")
        grid.addWidget(val_label, 1, col)
        values[key] = val_label
    return container, values


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
SETTINGS_APP = "ServerWindows"
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
    """A row of [VU meter + per-channel dB gain spinbox] columns, rebuilt
    (and gain reset to 0 dB) whenever the channel count changes -- e.g. on
    every layout change, since channel count/meaning changes with it."""

    CHANNEL_LABELS = {
        2: ["L", "R"],
        3: ["L", "R", "LFE"],
        4: ["L", "R", "RL", "RR"],
        6: ["L", "R", "C", "LFE", "RL", "RR"],
        8: ["L", "R", "C", "LFE", "RL", "RR", "SL", "SR"],
    }

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
            if item.widget():
                item.widget().deleteLater()
        self._meters = []
        self._gain_spins = []
        labels = self.CHANNEL_LABELS.get(channels, [str(i + 1) for i in range(channels)])
        for i, label in enumerate(labels):
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
            gain_spin.setToolTip(f"Channel {label} volume trim (applied before sending)")
            gain_spin.setMaximumWidth(88)
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


class ServerMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EtherWave Server")
        self.resize(560, 480)

        app_icon_path = _find_icon_path()
        if app_icon_path:
            self.setWindowIcon(QIcon(app_icon_path))

        self.settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                                   SETTINGS_ORG, SETTINGS_APP)

        self.sink_manager = DefaultDeviceManager()
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

        # Listens for clients announcing themselves so the audio stream can
        # be unicast straight to them. Started at launch, not on Start
        # Streaming, so subscriptions are already known by the time
        # streaming begins. Falls back to broadcast when nobody is
        # subscribed -- see SubscriberRegistry.
        self.subscribers = SubscriberRegistry()
        self.subscribers.subscribers_changed.connect(self._on_subscribers_changed)
        self.subscribers.start()
        self.channel_combo.currentIndexChanged.connect(self._on_channel_layout_changed)
        self.channel_combo.currentIndexChanged.connect(self._save_settings)
        self.blocksize_spin.valueChanged.connect(self._save_settings)
        self.vu_panel.gain_changed.connect(self._on_channel_gain_changed)
        self.autostart_check.toggled.connect(self._on_autostart_toggled)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet(
            "QGroupBox { font-size: 13px; font-weight: 600; margin-top: 10px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
        )
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(24)

        config_box = QGroupBox("Stream Configuration")
        form = QFormLayout(config_box)
        form.setContentsMargins(14, 22, 14, 14)
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(12)

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

        self.autostart_check = QCheckBox("Start with Windows (minimized to tray)")
        if not autostart.is_frozen():
            self.autostart_check.setToolTip(
                "Running from source: this will launch via the current Python "
                "interpreter rather than a packaged EtherWave Server.exe."
            )
        form.addRow("", self.autostart_check)

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
        meter_layout.setContentsMargins(14, 22, 14, 14)
        self.vu_panel = VUMeterPanel()
        meter_layout.addWidget(self.vu_panel)
        root.addWidget(meter_box, stretch=1)

        stats_widget, self.stat_labels = _build_stats_row([
            ("latency", "LATENCY"),
            ("packets", "PACKETS SENT"),
            ("sent", "DATA SENT"),
        ], column_width=150)
        root.addWidget(stats_widget, alignment=Qt.AlignHCenter)

        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(14, 22, 14, 14)
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

        # Reflects the registry directly rather than a persisted QSettings
        # value -- the registry Run key is also what Windows' own Task
        # Manager "Startup apps" tab shows/lets the user disable, so it's
        # the actual source of truth, not whatever we last wrote here.
        self.autostart_check.setChecked(autostart.is_enabled())

    def _save_settings(self):
        self.settings.setValue("stream/channels", self.channel_combo.currentData())
        self.settings.setValue("stream/packet_size", self.blocksize_spin.value())
        self.settings.sync()

    def _on_channel_layout_changed(self):
        self.broadcaster.update_stream_info(
            audio_port=AUDIO_PORT, channels=self.channel_combo.currentData()
        )

    def _on_autostart_toggled(self, enabled: bool):
        try:
            autostart.set_enabled(enabled)
            self._log(f"Start with Windows {'enabled' if enabled else 'disabled'}")
        except OSError as exc:
            self._log(f"ERROR: failed to update Start with Windows setting: {exc}")
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(not enabled)
            self.autostart_check.blockSignals(False)

    def _on_channel_gain_changed(self, channel_index: int, db: float):
        if self.capture_thread is not None:
            self.capture_thread.set_channel_gain_db(channel_index, db)
        gains = self.vu_panel.gains_db()
        self.settings.setValue(f"stream/channel_gains_{len(gains)}ch",
                                ",".join(str(g) for g in gains))
        self.settings.sync()

    def _load_channel_gains(self, channels: int):
        saved = self.settings.value(f"stream/channel_gains_{channels}ch", "", type=str)
        if not saved:
            return
        try:
            gains = [int(x) for x in saved.split(",")]
        except ValueError:
            return
        if len(gains) == channels:
            self.vu_panel.set_gains_db(gains)

    def _update_latency_estimate(self):
        frames = self.blocksize_spin.value()
        ms = frames / 48000.0 * 1000.0
        self.latency_label.setText(f"~{ms:.1f} ms per packet")

    def _on_subscribers_changed(self, count: int):
        if count:
            message = f"{count} client(s) subscribed — streaming directly to them"
        else:
            message = "No subscribed clients — falling back to LAN broadcast"
        self._log(message)
        print(message, flush=True)

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
        box = QMessageBox(self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setWindowTitle("About EtherWave Server")
        box.setText(
            "<h3>EtherWave Server (Windows)</h3>"
            f"<p>Version {APP_VERSION}</p>"
            "<p>Low-latency multichannel audio streaming from a "
            "Windows 11/VB-Cable server to a LAN client.</p>"
            "<p>License: GPLv3</p>"
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
            self._log(
                f"Switched Windows default output to VB-Cable, capturing "
                f"'{self.sink_manager.capture_device_name}' ({channels}ch)"
            )
        except Exception as exc:
            self._log(f"ERROR: failed to switch default audio device: {exc}")
            self.status_label.setText("Error switching default device")
            self.status_label.setStyleSheet("color: #e74c3c;")
            return

        self.vu_panel.set_channels(channels)

        self.capture_thread = AudioCaptureThread(
            subscribers=self.subscribers,
            channels=channels,
            device_index=self.sink_manager.capture_device_index,
            blocksize=blocksize,
        )
        self.capture_thread.levels_changed.connect(self.vu_panel.update_levels)
        self.capture_thread.status_changed.connect(self._log)
        self.capture_thread.error_occurred.connect(self._on_capture_error)
        self.capture_thread.stats_updated.connect(self._on_stats_updated)
        self.capture_thread.start()
        self._load_channel_gains(channels)

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
            self._log("Previous default output device restored, streaming stopped")
            status_text, status_color = "Idle", "#888"
        except RuntimeError as exc:
            self._log(f"ERROR: {exc}")
            status_text, status_color = "Error restoring default device (see log)", "#e74c3c"

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
        latency_ms = self.blocksize_spin.value() / 48000.0 * 1000.0
        self.stat_labels["latency"].setText(f"~{latency_ms:.1f} ms")
        self.stat_labels["packets"].setText(str(packets_sent))
        self.stat_labels["sent"].setText(_format_data_size(bytes_sent))

    def closeEvent(self, event):
        if not self._quitting:
            # Closing the window just minimizes to tray; use the tray
            # menu's "Close" to actually quit and stop streaming.
            event.ignore()
            self.hide()
            return

        self._stop_streaming()
        self.broadcaster.stop()
        self.subscribers.stop()
        self.subscribers.wait(2000)
        self.broadcaster.wait(2000)
        self.tray_icon.hide()
        super().closeEvent(event)
