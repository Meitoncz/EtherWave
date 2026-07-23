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

a = Analysis(
    [str(CLIENT_DIR / "main.py")],
    pathex=[str(CLIENT_DIR)],
    binaries=[],
    datas=[(str(ASSETS_DIR / "icon.png"), ".")],
    hiddenimports=["sounddevice", "numpy"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "NSLocalNetworkUsageDescription": (
            "EtherWave discovers and streams audio from EtherWave servers "
            "on your local network."
        ),
    },
)
