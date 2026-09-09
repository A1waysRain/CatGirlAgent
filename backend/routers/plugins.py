"""插件系统路由（从 main.py 拆出，2026-08-16）。

注意：本文件是 backend.routers.plugins；下方的 `from .. import plugins` 是
backend.plugins（插件管理器）——相对导入从父包取，不会和自己撞。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import plugins as _plugins_mod

router = APIRouter(prefix="/api")


@router.get("/plugins")
async def plugins_list():
    """当前已装插件列表。"""
    return {"plugins": _plugins_mod.get_plugins(), "new": []}


@router.post("/plugins/reload")
async def plugins_reload():
    """重扫插件目录并热重载（手动拷文件进目录后用）。"""
    plugins, new = _plugins_mod.reload_all()
    return {"plugins": plugins, "new": new}


@router.post("/plugins/add")
async def plugins_add(payload: dict):
    """从用户选择的文件夹添加插件（复制进插件目录并热加载）。"""
    path = (payload.get("path") or "").strip()
    if not path or not Path(path).is_dir():
        raise HTTPException(status_code=400, detail="请选择一个含 plugin.py 的插件文件夹喵")
    if not (Path(path) / "plugin.py").is_file():
        raise HTTPException(status_code=400, detail="这个文件夹里没有 plugin.py，不是插件喵")
    plugins, new = _plugins_mod.add_from_path(path)
    return {"plugins": plugins, "new": new}


@router.delete("/plugins/{pid}")
async def plugins_remove(pid: str):
    """删除插件文件夹并热重载。"""
    if pid in ("", ".", "..") or "/" in pid or "\\" in pid or ":" in pid:
        raise HTTPException(status_code=400, detail="参数不对喵")
    plugins, _ = _plugins_mod.remove(pid)
    return {"plugins": plugins}
