"""应用自动发现路由（从 main.py 拆出，2026-08-16）。

扫描完全由用户主动触发，绝不自动扫描。
"""
import time

from fastapi import APIRouter

from ..tools import _discover_apps, _read_discovered, _save_discovered

router = APIRouter(prefix="/api")


@router.get("/apps")
async def apps_status():
    """应用扫描状态（前端据此判断是否首次、展示数量）。"""
    discovered = _read_discovered()
    return {"scanned": bool(discovered), "count": len(discovered)}


@router.post("/apps/scan")
async def apps_scan():
    """用户主动触发的应用扫描（点击/确认即授权），写缓存快照。"""
    apps = _discover_apps()
    _save_discovered(apps)
    return {"count": len(apps), "scanned_at": time.time()}
