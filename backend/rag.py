# -*- coding: utf-8 -*-
"""猫娘知识库检索（RAG）：本地 embedding + 手搓向量检索。

背景：2026-08-26 在独立练习 rag_qa 跑通 RAG 链路后，按《猫娘接入RAG方案.md》（施工方向\）
接进猫娘本体。形态 = 新增 rag_query 工具，模型按需调用（工具层在 tools.py）。
原则：全程手搓，不引 langchain / chromadb；只多 fastembed 一个依赖（onnxruntime 猫娘已有）。

知识库布局（可被 CATGIRL_RAG_DIR 整体改到别处；CATGIRL_EMBED_MODEL 换 embedding 模型）：
    %APPDATA%\\catgirl\\rag_data\\
    ├── docs\\     # 文档（.md/.txt/.docx/.xlsx），主人往里放即纳入；dev 首启自动同步默认语料
    └── index\\    # 生成物：index.json（chunks）+ vectors.npy（归一化向量）+ sig.json（mtime 签名）
"""
import json
import os
import re
import shutil
import threading
from pathlib import Path

import numpy as np

EMBED_MODEL = os.environ.get("CATGIRL_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
SUPPORTED_DOC_EXTS = {".md", ".txt", ".docx", ".xlsx"}

# 默认语料：dev 源码树里的学习教材 + 项目文档（打包版无源码树则跳过，知识库为空）
# backend/rag.py → parents[0]=backend, [1]=Cat_Girl, [2]=猫娘来咯
_SRC_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOCS = [
    "资料/AI应用核心概念-大白话讲解.md",
    "资料/2.流式输出streaming+SSE/流式输出-大白话对号猫娘.md",
    "资料/3.LangChain和LangGraph/LangChain和LangGraph大学习.md",
    "资料/3.LangChain和LangGraph/LangChain和LangGraph-大白话对号猫娘.md",
    "资料/4.RAG检索增强生成/RAG大学习.md",
    "资料/4.RAG检索增强生成/RAG-大白话对号猫娘.md",
    "项目介绍.md",
    "更新日志-2026-09-27.md",
    "施工方向/完成/猫娘接入RAG方案.md",
    "施工方向/完成/猫娘PPT创作方案.md",
    "施工方向/完成/猫娘接入DeepSeek视觉模型方案.md",
    "施工方向/完成/上下文压缩方案.md",
    "施工方向/完成/桌宠功能扩展方案.md",
    "施工方向/完成/文档读写与修改方案.md",
    "施工方向/完成/流式输出方案.md",
    "施工方向/完成/图片识别插件方案.md",
    "施工方向/完成/应用自动发现方案.md",
    "施工方向/完成/微信传文件方案.md",
    "施工方向/完成/MaaFramework应用内操作方案.md",
]

# ─────────────────────────── 路径 ───────────────────────────

DOCS_DIR: Path | None = None
INDEX_DIR: Path | None = None


def _init_dirs() -> None:
    global DOCS_DIR, INDEX_DIR
    if DOCS_DIR is None:
        env = os.environ.get("CATGIRL_RAG_DIR")
        root = Path(env) if env else Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "rag_data"
        DOCS_DIR = root / "docs"
        INDEX_DIR = root / "index"


# import 即初始化路径（warmup/search 若拿到 None 会直接崩——打包冒烟实测踩过：
# exe 里 warmup 建索引失败被吞，聊天查知识库永远"卡住"）
_init_dirs()


def _sync_default_docs() -> int:
    """dev：把源码树默认语料增量复制进 docs\\（目标存在且不比源旧则跳过）。"""
    if not _SRC_ROOT.exists() or not (_SRC_ROOT / "资料").exists():
        return 0  # 打包版 / 无源码树：不塞默认语料
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for rel in DEFAULT_DOCS:
        src = _SRC_ROOT / rel
        if not src.exists():
            continue
        dst = DOCS_DIR / rel.replace("\\", "/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_mtime_ns >= src.stat().st_mtime_ns:
            continue
        dst.write_bytes(src.read_bytes())
        n += 1
    return n


# ─────────────────────────── 切分（手搓，不引 langchain） ───────────────────────────


def _by_chars(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    """超长文本按字符切，首尾 overlap 重叠保证上下文连贯（练习调过的参数）。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    segs, start = [], 0
    while start < len(text):
        end = start + size
        segs.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return segs


def _split_document(text: str, source: str) -> list[dict]:
    """按 Markdown 标题切块（标题是语义边界），超长节再按字符兜底切。每块带 source + title。"""
    chunks: list[dict] = []
    cur_title, cur_lines = "", []

    def flush():
        nonlocal cur_lines
        body = "\n".join(cur_lines).strip()
        cur_lines = []
        if not body:
            return
        full = (cur_title + "\n" + body).strip()
        for seg in _by_chars(full):
            chunks.append({"text": seg, "source": source, "title": cur_title})

    for line in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(\S.*)$", line)
        if m:
            flush()
            cur_title = f"{'#' * len(m.group(1))} {m.group(2).strip()}"
        else:
            cur_lines.append(line)
    flush()
    return chunks


# ─────────────────────────── embedding（fastembed 懒加载） ───────────────────────────

_embedder = None


def _get_embedder():
    """懒加载 embedding 模型（首次 ~100MB 下载/缓存，之后复用；不拖模块 import）。"""
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(model_name=EMBED_MODEL)
    return _embedder


def _embed(texts: list[str]) -> list[list[float]]:
    # fastembed 返回 np.float32，必须转成 Python float（Chroma 要原生 float；这里同样统一）
    return [[float(x) for x in v] for v in _get_embedder().embed(texts)]


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


# ─────────────────────────── 索引：构建 / 加载 / mtime 检测 ───────────────────────────


def _doc_sig() -> list:
    """docs 目录文件清单（相对路径 + mtime + size），作为「是否需要重建」的签名。"""
    if not DOCS_DIR.exists():
        return []
    sig = []
    for p in sorted(DOCS_DIR.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_DOC_EXTS:
            sig.append([p.relative_to(DOCS_DIR).as_posix(), p.stat().st_mtime_ns, p.stat().st_size])
    return sig


def _list_docs() -> list[Path]:
    if not DOCS_DIR.exists():
        return []
    return [p for p in DOCS_DIR.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_DOC_EXTS]


def list_documents() -> list[dict]:
    """返回 rag_data/docs 的可索引文件清单，供设置页管理。"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in sorted(_list_docs(), key=lambda item: item.as_posix().casefold()):
        stat = path.stat()
        entries.append({
            "path": path.relative_to(DOCS_DIR).as_posix(),
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "extension": path.suffix.lower(),
        })
    return entries


def _invalidate_index() -> None:
    global _ready
    with _index_lock:
        _ready = False


def import_document(source: str) -> dict:
    """复制用户在桌面文件选择器中明确选中的资料到 rag_data/docs。"""
    src = Path((source or "").strip())
    if not src.is_file():
        raise ValueError("文件不存在")
    if src.suffix.lower() not in SUPPORTED_DOC_EXTS:
        raise ValueError("仅支持 .md、.txt、.docx、.xlsx 文件")
    if src.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("文件超过 50MB，暂不适合直接建知识库")
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    name = src.name
    dest = DOCS_DIR / name
    index = 2
    while dest.exists():
        dest = DOCS_DIR / f"{src.stem} ({index}){src.suffix.lower()}"
        index += 1
    shutil.copy2(src, dest)
    _invalidate_index()
    return {"path": dest.relative_to(DOCS_DIR).as_posix(), "size": dest.stat().st_size}


def delete_document(relative_path: str) -> None:
    """只允许删除 docs 根目录内的已入库文件，拒绝路径穿越。"""
    raw = Path((relative_path or "").strip())
    if not str(raw) or raw.is_absolute():
        raise ValueError("文件路径不正确")
    root = DOCS_DIR.resolve()
    target = (DOCS_DIR / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("文件路径不正确")
    if target.suffix.lower() not in SUPPORTED_DOC_EXTS or not target.is_file():
        raise ValueError("没找到可删除的知识库文件")
    target.unlink()
    _invalidate_index()


def rebuild_index() -> bool:
    """标记索引失效并异步重建；返回是否已有可处理文档。"""
    _invalidate_index()
    if not _doc_sig():
        return False
    ensure_index(block=False)
    return True


def _split_structured_sections(sections: list[dict], source: str) -> list[dict]:
    """将 Word/Excel 标题段落切块；标题和 Excel 表头在每个子块中都保留。"""
    chunks = []
    for section in sections:
        title = (section.get("title") or "").strip()
        body = (section.get("text") or "").strip()
        if not body:
            continue
        # 表头是 Excel 行块的语义锚点；长块继续按字符兜底时也必须每段重复。
        header, content = "", body
        if body.startswith("表头："):
            header, _, content = body.partition("\n")
        prefix = "\n".join(part for part in (title, header) if part)
        if not content:
            chunks.append({"text": prefix, "source": source, "title": title})
            continue
        size = max(200, 800 - len(prefix) - (1 if prefix else 0))
        overlap = min(100, max(1, size // 4))
        for seg in _by_chars(content, size=size, overlap=overlap):
            text = (prefix + "\n" + seg).strip() if prefix else seg
            chunks.append({"text": text, "source": source, "title": title})
    return chunks


def _document_chunks(path: Path) -> list[dict]:
    """按文件类型读取，统一返回可向量化的 chunk；读取单个坏文件不影响整库。"""
    source = path.relative_to(DOCS_DIR).as_posix()
    ext = path.suffix.lower()
    if ext in {".md", ".txt"}:
        return _split_document(path.read_text(encoding="utf-8", errors="ignore"), source)
    from .document_extract import extract_docx_sections, extract_xlsx_sections
    if ext == ".docx":
        return _split_structured_sections(extract_docx_sections(path), source)
    if ext == ".xlsx":
        return _split_structured_sections(extract_xlsx_sections(path), source)
    return []


def _build_index() -> bool:
    """扫文档 → 切分 → 向量化 → 持久化。返回是否构建成功。"""
    _sync_default_docs()
    docs = _list_docs()
    if not docs:
        return False
    chunks: list[dict] = []
    for p in docs:
        try:
            chunks.extend(_document_chunks(p))
        except Exception:
            continue
    # 去重（同一文本可能被多份文档/多次切到）
    seen, uniq = set(), []
    for c in chunks:
        if c["text"] in seen:
            continue
        seen.add(c["text"])
        uniq.append(c)
    chunks = uniq
    if not chunks:
        return False
    vecs = _normalize(np.array(_embed([c["text"] for c in chunks]), dtype="float32"))
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.save(INDEX_DIR / "vectors.npy", vecs)
    (INDEX_DIR / "index.json").write_text(
        json.dumps({"chunks": chunks}, ensure_ascii=False), encoding="utf-8")
    (INDEX_DIR / "sig.json").write_text(json.dumps(_doc_sig()), encoding="utf-8")
    return True


_index = {"vecs": None, "chunks": []}
_index_lock = threading.Lock()
_ready = False
_building = False


def _load_index_from_disk() -> bool:
    global _ready
    try:
        chunks = json.loads((INDEX_DIR / "index.json").read_text(encoding="utf-8"))["chunks"]
        vecs = np.load(INDEX_DIR / "vectors.npy")
        _index["chunks"] = chunks
        _index["vecs"] = vecs
        _ready = True
        return True
    except Exception:
        return False


def ensure_index(block: bool = True) -> bool:
    """确保索引就绪。block=True 同步构建到完成（测试用）；block=False 起后台线程立即返回。

    已就绪但文档变了（签名不一致）也要重建——不能只用 _ready 短路，
    否则往 docs 放新文档后永远不更新（实测踩过）。
    """
    global _ready, _building
    # 必须在空库判断和签名比较之前同步：旧实现只在 _build_index() 内同步，
    # 但 build() 看到空 docs 会提前 return，导致 dev 首启永远塞不进默认语料；
    # 已就绪时也会因签名未变直接返回，看不到源码教材的新版本。
    try:
        _sync_default_docs()
    except Exception:
        pass  # 默认语料同步失败不应拖垮聊天；用户手工放入 docs 的文档仍可检索
    if _ready and _load_sig() == _doc_sig():
        return True
    with _index_lock:
        if _ready and _load_sig() == _doc_sig():
            return True
        if _building:
            return _ready
        _building = True

    def build():
        global _ready, _building
        try:
            if not _doc_sig():
                return  # 知识库是空的，别白建
            if _load_sig() == _doc_sig() and (INDEX_DIR / "vectors.npy").exists() \
                    and (INDEX_DIR / "index.json").exists():
                _load_index_from_disk()
            else:
                if _build_index():
                    _load_index_from_disk()
        except Exception:
            pass  # 构建失败保持未就绪，下次再试（不崩聊天）
        finally:
            _building = False

    if block:
        build()
    else:
        threading.Thread(target=build, daemon=True).start()
    return _ready


def _load_sig():
    try:
        return json.loads((INDEX_DIR / "sig.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def warmup() -> None:
    """聊天服务启动时后台预热索引（不阻塞、失败静默）。"""
    try:
        ensure_index(block=False)
    except Exception:
        pass


# ─────────────────────────── 检索 ───────────────────────────

# 查询改写：bge-small-zh 是纯中文模型，对英文缩写不敏感——先展开再检索
# （练习踩过的坑：原文「RAG 是什么」搜不到，「什么是检索增强生成」一击命中）
QUERY_EXPANSION = {
    "RAG": "检索增强生成",
    "LLM": "大语言模型",
    "SSE": "服务器推送事件",
    "API": "接口",
    "embedding": "向量化嵌入",
}


def rewrite_query(q: str) -> str:
    # 注意：不能用 \b 单词边界——Python \w 在 Unicode 模式匹配中文，「什么是SSE」里
    # SSE 前是中文「是」也算 \w，\bSSE\b 会匹配失败（练习 rag_qa 同款正则的隐患）。
    # 改用前后「非 ASCII 字母数字」断言：中文/标点/空格都算边界。
    for abbr, zh in QUERY_EXPANSION.items():
        q = re.sub(rf"(?i)(?<![A-Za-z0-9]){re.escape(abbr)}(?![A-Za-z0-9])", zh, q)
    return q


_SEG_MAX = 600          # 单段返回给模型的上限字符
# 余弦相似度阈值（实测：强相关 0.45~0.71，无关 0.38~0.42，bge 长文本分数天然偏高）
_MIN_SIM = 0.45         # 低于此不算命中（防硬塞不相关片段）


def _fmt_seg(c: dict) -> str:
    head = f"【{c['source']}】"
    if c.get("title"):
        title = c["title"].lstrip("#").strip()
        if title:
            head += f"（{title}）"
    text = c["text"]
    if len(text) > _SEG_MAX:
        text = text[:_SEG_MAX] + "…"
    return f"{head}\n{text}"


def search(query: str, top_k: int = 3) -> tuple[bool, str]:
    """按语义检索知识库。返回 (状态, context)。

    状态：False=building（还没建好）/ 真值下 text 为：
      "empty"   → 知识库还是空的
      "nomatch" → 没找到相关片段
      其他      → 拼好的 context（带来源）
    """
    # 检索可能早于启动预热发生；先同步一次，避免默认语料尚未复制时被误判为空库。
    try:
        _sync_default_docs()
    except Exception:
        pass
    if not _doc_sig():
        return True, "empty"   # 知识库空（docs 目录里没有任何受支持文档）
    if not ensure_index(block=False):
        return False, "building"
    if not _index["chunks"]:
        return True, "empty"
    qv = np.array(_embed([rewrite_query(query)])[0], dtype="float32")
    qn = np.linalg.norm(qv)
    if qn > 0:
        qv = qv / qn
    sims = _index["vecs"] @ qv
    order = np.argsort(-sims)[: top_k]
    parts = []
    for i in order:
        if sims[int(i)] < _MIN_SIM:
            continue
        parts.append(_fmt_seg(_index["chunks"][int(i)]))
    if not parts:
        return True, "nomatch"
    return True, "知识库查到以下片段（可能部分不相关，只取真正对得上问题的），按它回答并报出「来源」：\n\n" + "\n\n---\n\n".join(parts)
