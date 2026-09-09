"""桌宠启停回调注册：从 main.py 拆出（2026-08-16 路由拆分）。

desktop/app.py 注册回调，settings 路由在「显示桌宠」开关变化时调用。
main.py re-export set_pet_control 以兼容 `from backend.main import set_pet_control`。
"""
_pet_control = {"start": None, "stop": None}


def set_pet_control(start=None, stop=None) -> None:
    """注册桌宠启停回调（桌面壳调用）。"""
    _pet_control["start"] = start
    _pet_control["stop"] = stop
