"""桌面壳注入手机接入启停回调，避免路由层直接管理 uvicorn 线程。"""

_lan_control = {"apply": None, "status": None}


def set_lan_control(apply=None, status=None) -> None:
    _lan_control["apply"] = apply
    _lan_control["status"] = status
