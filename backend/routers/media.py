"""图片/文件上传路由（从 main.py 拆出，2026-08-16）。"""
import shutil
import time
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ..paths import _FILE_EXT, _media_dir

router = APIRouter(prefix="/api")
UPLOAD_LIMIT = 20 * 1024 * 1024
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@router.post("/upload_mobile")
async def upload_mobile(request: Request, filename: str, kind: str = "file"):
    """手机直接发送文件字节，不接受电脑路径，不信任客户端文件名作为存储路径。"""
    original = filename.replace("\\", "/").rsplit("/", 1)[-1]
    ext = Path(original).suffix.lower()
    if kind not in {"image", "file"} or ext not in (IMAGE_EXT if kind == "image" else _FILE_EXT):
        raise HTTPException(status_code=400, detail="不支持这个文件类型")
    if original.lower() in {"config.json", "config.yaml", ".env", ".env.local"}:
        raise HTTPException(status_code=400, detail="不能上传敏感配置文件")
    name = f"mobile_{secrets.token_hex(16)}{ext}"
    dest = _media_dir() / name
    total = 0
    try:
        with dest.open("xb") as output:
            async for chunk in request.stream():
                total += len(chunk)
                if total > UPLOAD_LIMIT:
                    raise HTTPException(status_code=413, detail="文件不能超过 20 MB")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="不能上传空文件")
        if ext in IMAGE_EXT:
            from PIL import Image
            try:
                with Image.open(dest) as image:
                    if image.width * image.height > 25_000_000:
                        raise ValueError("图片像素过多")
                    image.verify()
            except Exception:
                raise HTTPException(status_code=400, detail="图片损坏或尺寸过大") from None
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return {"url": f"/media/{name}", "path": str(dest), "name": original, "kind": kind}


@router.post("/upload")
async def upload_image(payload: dict, request: Request):
    """把用户选中的图片复制进 media 目录，返回可访问 url 与绝对路径。"""
    if getattr(request.state, "mobile_access", False):
        raise HTTPException(status_code=403, detail="手机端请上传文件内容，不能指定电脑路径")
    src = (payload.get("path") or "").strip()
    if not src or not Path(src).is_file():
        raise HTTPException(status_code=400, detail="图片不存在喵")
    if Path(src).suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        raise HTTPException(status_code=400, detail="只支持图片喵")
    try:
        name = f"upload_{int(time.time() * 1000)}{Path(src).suffix.lower()}"
        dest = _media_dir() / name
        shutil.copy2(src, dest)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败喵：{e}")
    return {"url": f"/media/{name}", "path": str(dest)}


@router.post("/upload_file")
async def upload_file(payload: dict, request: Request):
    """把用户选中的任意常见文件复制进 media 目录，返回可访问 url 与绝对路径。"""
    if getattr(request.state, "mobile_access", False):
        raise HTTPException(status_code=403, detail="手机端请上传文件内容，不能指定电脑路径")
    src = (payload.get("path") or "").strip()
    if not src or not Path(src).is_file():
        raise HTTPException(status_code=400, detail="文件不存在喵")
    ext = Path(src).suffix.lower()
    if ext not in _FILE_EXT:
        raise HTTPException(status_code=400, detail=f"这个文件类型本喵还看不了（{ext or '无扩展名'}）喵")
    try:
        name = f"file_{int(time.time() * 1000)}{ext}"
        dest = _media_dir() / name
        shutil.copy2(src, dest)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败喵：{e}")
    return {"url": f"/media/{name}", "path": str(dest), "name": Path(src).name}
