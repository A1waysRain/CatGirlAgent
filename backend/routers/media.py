"""图片/文件上传路由（从 main.py 拆出，2026-08-16）。"""
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..paths import _FILE_EXT, _media_dir

router = APIRouter(prefix="/api")


@router.post("/upload")
async def upload_image(payload: dict):
    """把用户选中的图片复制进 media 目录，返回可访问 url 与绝对路径。"""
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
async def upload_file(payload: dict):
    """把用户选中的任意常见文件复制进 media 目录，返回可访问 url 与绝对路径。"""
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
