# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the "EtherWave Client.app" macOS bundle.

Build via packaging/macos/build.sh (handles icon generation/conversion and
invokes PyInstaller with this spec) rather than running pyinstaller
directly, unless you've already got assets/icon.icns in place.
"""

from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH).resolve().parent.parent
CLIENT_DIR = PROJECT_ROOT / "client"
ASSETS_DIR = PROJECT_ROOT / "assets"

# Single source of truth for the version, patched by
# .github/workflows/release.yml at release time (alongside PKGBUILD's
# pkgver) so this .app's version metadata and client/gui.py's About dialog
# (which reads the same bundled file at runtime) never drift out of sync.
APP_VERSION = (ASSETS_DIR / "VERSION").read_text().strip()

# EtherWave imports QtCore/QtGui/QtWidgets/QtNetwork (see client/gui.py,
# client/main.py's single-instance guard). requirements.txt installs
# PySide6-Essentials rather than the full PySide6 meta-package for the same
# reason, but PyInstaller's PySide6 hook can still go looking for these
# unused modules/plugins on disk and bundle them if present -- exclude them
# explicitly so a build machine that happens to have the full PySide6
# installed doesn't balloon the app size regardless.
UNUSED_QT_MODULES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNfc", "PySide6.QtBluetooth", "PySide6.QtSerialPort",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtRemoteObjects",
    "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSpatialAudio", "PySide6.QtStateMachine", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
]

a = Analysis(
    [str(CLIENT_DIR / "main.py")],
    pathex=[str(CLIENT_DIR)],
    binaries=[],
    datas=[
        (str(ASSETS_DIR / "icon.png"), "."),
        (str(ASSETS_DIR / "icon_tray_white.png"), "."),
        (str(ASSETS_DIR / "icon_tray_black.png"), "."),
        (str(ASSETS_DIR / "VERSION"), "."),
    ],
    hiddenimports=["sounddevice", "numpy"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=UNUSED_QT_MODULES,
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EtherWave Client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="EtherWave Client",
)

app = BUNDLE(
    coll,
    name="EtherWave Client.app",
    icon=str(ASSETS_DIR / "icon.icns"),
    bundle_identifier="com.etherwave.client",
    info_plist={
        "CFBundleName": "EtherWave Client",
        "CFBundleDisplayName": "EtherWave Client",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "NSLocalNetworkUsageDescription": (
            "EtherWave discovers and streams audio from EtherWave servers "
            "on your local network."
        ),
    },
)
