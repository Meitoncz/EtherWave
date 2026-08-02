# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for "EtherWave Server.exe" (Windows).

Build via packaging/windows/build.ps1 (handles icon conversion and invokes
PyInstaller with this spec) rather than running pyinstaller directly, unless
you've already got assets/icon.ico in place.

Mirrors packaging/macos/EtherWaveClient.spec's Analysis/excludes/hiddenimports
structure -- see that file's comments for the rationale, not repeated here.
Windows has no bundle-with-Info.plist step the way macOS does; the output is
just a folder (onedir build, same as the macOS COLLECT step) containing
"EtherWave Server.exe" and its dependencies, zipped for release by CI.
"""

from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(SPECPATH).resolve().parent.parent
SERVER_DIR = PROJECT_ROOT / "server_windows"
ASSETS_DIR = PROJECT_ROOT / "assets"

# Single source of truth for the version, patched by
# .github/workflows/release.yml at release time -- see EtherWaveClient.spec.
APP_VERSION = (ASSETS_DIR / "VERSION").read_text().strip()

# See EtherWaveClient.spec for why these are excluded: EtherWave only uses
# QtCore/QtGui/QtWidgets/QtNetwork (all in PySide6-Essentials), but
# PyInstaller's PySide6 hook can still bundle unused Addons modules/plugins
# if a build machine happens to have the full PySide6 meta-package installed.
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
    [str(SERVER_DIR / "main.py")],
    pathex=[str(SERVER_DIR)],
    binaries=[],
    datas=[
        (str(ASSETS_DIR / "icon.png"), "."),
        (str(ASSETS_DIR / "icon_tray_white.png"), "."),
        (str(ASSETS_DIR / "icon_tray_black.png"), "."),
        (str(ASSETS_DIR / "VERSION"), "."),
    ],
    # comtypes.gen is where comtypes caches generated interface wrappers at
    # runtime -- collect the package itself so that machinery is present in
    # the frozen build; the actual IPolicyConfig/IMMDeviceEnumerator
    # definitions in default_device.py are plain hand-written ctypes/comtypes
    # code (no comtypes.client.GetModule() codegen), so no generated-module
    # data needs bundling beyond the package itself.
    hiddenimports=["sounddevice", "numpy", "comtypes", "comtypes.client", "winreg"],
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
    name="EtherWave Server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ASSETS_DIR / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="EtherWave Server",
)
