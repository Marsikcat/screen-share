"""Windows integration: autostart, firewall rules, key names."""

import ctypes
import subprocess
import sys
from pathlib import Path

from .config import (APP_NAME, CONTROL_PORT, DISCOVERY_PORT, INSTANCE, ROOT, STREAM_PORT,
                     VOICE_PORT)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = APP_NAME + (f"-{INSTANCE}" if INSTANCE else "")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _launch_command():
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{ROOT / "app.py"}" --autostart'


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def open_firewall():
    """Inbound rules for our ports. Windows shows its own UAC prompt; returns True if applied."""
    rules = [
        (f"{APP_NAME} UDP", "UDP", f"{STREAM_PORT},{STREAM_PORT + 1},{DISCOVERY_PORT},{VOICE_PORT}"),
        (f"{APP_NAME} TCP", "TCP", str(CONTROL_PORT)),
    ]
    cmds = []
    for name, proto, ports in rules:
        cmds.append(f"netsh advfirewall firewall delete rule name='{name}' | Out-Null")
        cmds.append(f"netsh advfirewall firewall add rule name='{name}' dir=in action=allow "
                    f"protocol={proto} localport={ports} profile=any | Out-Null")
    inner = "; ".join(cmds).replace('"', '\\"')
    ps = (f"$p = Start-Process powershell -Verb RunAs -WindowStyle Hidden -Wait -PassThru "
          f"-ArgumentList '-NoProfile','-Command',\"{inner}\"; exit $p.ExitCode")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           timeout=180, creationflags=NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


MOUSE_KEYS = {0x04: "Средняя кнопка мыши", 0x05: "Кнопка мыши 4", 0x06: "Кнопка мыши 5"}


def key_name(vk):
    if vk in MOUSE_KEYS:
        return MOUSE_KEYS[vk]
    try:
        user32 = ctypes.windll.user32
        scan = user32.MapVirtualKeyW(vk, 0)
        extended = vk in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0xA3, 0xA5)
        buf = ctypes.create_unicode_buffer(64)
        if user32.GetKeyNameTextW((scan << 16) | (extended << 24), buf, 64):
            return buf.value
    except (OSError, AttributeError):
        pass
    return f"Клавиша {vk}"


def pressed_key():
    """First virtual key currently held (ignores left/right mouse buttons)."""
    user32 = ctypes.windll.user32
    for vk in range(0x03, 0xFF):
        if vk in (0x0D,):     # Enter would fire when confirming dialogs
            continue
        if user32.GetAsyncKeyState(vk) & 0x8000:
            return vk
    return None
