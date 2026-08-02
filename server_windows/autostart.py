"""
EtherWave Server (Windows) - Start-with-Windows toggle

Windows has no systemd-user-service equivalent (see the Linux server's
packaging/arch/etherwave-server.service) -- the standard mechanism for "run
this at login" for an ordinary desktop app is a value under
HKEY_CURRENT_USER\\...\\Run, read by explorer.exe at every login. No
elevation is required since HKCU is per-user.

Registry is the source of truth (not QSettings): a value written here is
also what Windows' own Task Manager "Startup apps" tab shows/lets the user
disable directly, so gui.py reads is_enabled() fresh at launch rather than
trusting a possibly-stale persisted checkbox state.
"""

import sys
import winreg
from pathlib import Path

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "EtherWaveServer"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _command_line() -> str:
    """The command Windows should run at login. When frozen (a PyInstaller
    build -- see packaging/windows/), sys.executable IS "EtherWave
    Server.exe" and needs no arguments. Running from source instead invokes
    the interpreter against this package's main.py directly, preferring a
    sibling pythonw.exe (no console window) over sys.executable's own
    python.exe if one exists next to it."""
    if is_frozen():
        return f'"{sys.executable}"'
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.is_file():
        interpreter = pythonw
    main_py = Path(__file__).resolve().parent / "main.py"
    return f'"{interpreter}" "{main_py}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_WRITE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, _command_line())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
            except OSError:
                pass
