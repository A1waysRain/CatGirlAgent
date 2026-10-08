"""设置路由（从 main.py 拆出，2026-08-16）。

注意：本文件是 backend.routers.settings；下方的 `from ..settings` 是
backend.settings（设置存储模块）——相对导入从父包取，不会和自己撞。
"""
from pathlib import Path
import ipaddress

from fastapi import APIRouter, HTTPException, Request

from ..autostart import get_autostart, set_autostart
from ..paths import _avatar_dir
from ..pet_control import _pet_control
from ..lan_control import _lan_control
from .. import lan
from ..settings import load_settings, save_settings

router = APIRouter(prefix="/api")


def _settings_response(s: dict, reveal_lan_token: bool = True) -> dict:
    data = {
        **s,
        "autostart": get_autostart(),
        "pet_available": bool(_pet_control["start"]),
    }
    if not reveal_lan_token:
        data["lan_token"] = ""
    return data


def _is_lan_request(request: Request) -> bool:
    if getattr(request.state, "mobile_access", False):
        return True
    host = request.headers.get("host", "")
    name, _, port = host.partition(":")
    try:
        return lan.is_lan_host(name, int(port))
    except ValueError:
        return False


def _validate_lan_settings(settings: dict) -> None:
    """即使不经桌面壳，也不允许把危险或无效的 LAN 配置落盘。"""
    try:
        settings["lan_public_origin"] = lan.normalize_origin(settings.get("lan_public_origin", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not settings.get("lan_enabled"):
        return
    try:
        ip = ipaddress.ip_address(str(settings.get("lan_ip") or ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="请选择当前手机热点对应的私网 IPv4 地址")
    private_networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    if ip.version != 4 or not any(ip in network for network in private_networks):
        raise HTTPException(status_code=400, detail="请选择当前手机热点对应的私网 IPv4 地址")
    try:
        port = int(settings.get("lan_port"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="手机接入端口必须是 1 到 65535 的整数")
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="手机接入端口必须是 1 到 65535 的整数")


@router.get("/settings")
async def get_settings(request: Request):
    """返回全部设置 + 开机自启动状态。"""
    return _settings_response(load_settings(), reveal_lan_token=not _is_lan_request(request))


@router.put("/settings")
async def update_settings(payload: dict, request: Request):
    """保存设置；show_pet 变化时实时启停桌宠；autostart 走注册表。"""
    if _is_lan_request(request):
        raise HTTPException(status_code=403, detail="手机端暂不允许修改电脑设置")
    cur = load_settings()
    old_show_pet = cur.get("show_pet", True)
    payload = dict(payload or {})
    # 重置令牌：生成新值后再交给桌面壳启停，旧 cookie 会随配置变化失效。
    if "lan_token" in payload and not str(payload.get("lan_token") or "").strip():
        payload["lan_token"] = lan.new_token()
    candidate = dict(cur)
    for key, value in payload.items():
        if key in candidate:
            candidate[key] = value
    if any(key in payload for key in ("lan_enabled", "lan_ip", "lan_port", "lan_token", "lan_public_origin")):
        _validate_lan_settings(candidate)
        if "lan_public_origin" in payload:
            payload["lan_public_origin"] = candidate["lan_public_origin"]
        apply = _lan_control.get("apply")
        if apply:
            try:
                apply(candidate)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
    if "autostart" in payload:
        set_autostart(bool(payload["autostart"]))
    new = save_settings(payload)
    if new.get("show_pet") != old_show_pet:
        if new.get("show_pet"):
            if _pet_control["start"]:
                _pet_control["start"]()
        else:
            if _pet_control["stop"]:
                _pet_control["stop"]()
    return _settings_response(new)


@router.post("/settings/avatar")
async def change_avatar(payload: dict, request: Request):
    """更换聊天头像：把本地图片转成 PNG 存入头像目录。role=user|cat"""
    if _is_lan_request(request):
        raise HTTPException(status_code=403, detail="手机端暂不允许修改电脑设置")
    role = payload.get("role", "")
    path = (payload.get("path") or "").strip()
    if role not in ("user", "cat"):
        raise HTTPException(status_code=400, detail="参数不对喵")
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=400, detail="图片不存在喵")
    if Path(path).suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        raise HTTPException(status_code=400, detail="只支持图片喵")
    try:
        from PIL import Image

        im = Image.open(path).convert("RGBA")
        name = f"{role}.png"
        im.save(_avatar_dir() / name, "PNG")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"图片处理失败喵：{e}")
    save_settings({f"{role}_avatar": name})
    return {"ok": True, "url": f"/img/{name}", "avatar": name}
