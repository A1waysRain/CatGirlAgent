"""开机自启动：通过 HKCU 的 Run 注册表项实现（无需管理员权限）。

注册表值指向打包好的 exe；开发环境若 dist 里没有 exe 则返回不可用。
"""
import sys
from pathlib import Path

import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "猫娘来咯"


def _launch_command() -> str:
    """返回注册表要写进去的启动命令（带引号的 exe 路径）。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(__file__).resolve().parent.parent / "dist" / "猫娘来咯.exe"
    if exe.exists():
        return f'"{exe}"'
    return ""


def set_autostart(enabled: bool) -> bool:
    """设置/取消开机自启动。成功返回 True。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = _launch_command()
                if not cmd:
                    return False
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def get_autostart() -> bool:
    """当前是否已开机自启动。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
