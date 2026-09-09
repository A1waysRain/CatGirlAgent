"""设置页知识库文件管理：仅管理 rag_data/docs 内的可检索资料。"""
from fastapi import APIRouter, HTTPException

from .. import rag

router = APIRouter(prefix="/api/rag")


@router.get("/files")
async def list_rag_files():
    return {"files": rag.list_documents(), "ready": rag._ready, "building": rag._building}


@router.post("/files")
async def import_rag_file(payload: dict):
    try:
        item = rag.import_document(str((payload or {}).get("path") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    rag.rebuild_index()
    return {"status": "ok", "file": item, "building": True}


@router.delete("/files")
async def delete_rag_file(payload: dict):
    try:
        rag.delete_document(str((payload or {}).get("path") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    rag.rebuild_index()
    return {"status": "ok", "building": True}


@router.post("/rebuild")
async def rebuild_rag_index():
    if not rag.rebuild_index():
        raise HTTPException(status_code=400, detail="知识库里还没有可索引的文件")
    return {"status": "ok", "building": True}
