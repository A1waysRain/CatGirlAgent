"""知识库文件管理回归：导入、同名处理、删除边界与异步重建。"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import rag


def check(name, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {name}", detail)
    if not condition:
        raise AssertionError(name)


root = Path(tempfile.mkdtemp(prefix="catgirl-rag-files-"))
source = root / "source"
source.mkdir()
(source / "notes.txt").write_text("检索增强生成资料", encoding="utf-8")
(source / "unsupported.pdf").write_bytes(b"not a supported document")

rag.DOCS_DIR = root / "rag_data" / "docs"
rag.INDEX_DIR = root / "rag_data" / "index"
rag.DEFAULT_DOCS = []
rag._ready = False
rag._building = False
rag._index = {"vecs": None, "chunks": []}
rag._embed = lambda texts: [[1.0, 0.0] for _ in texts]

first = rag.import_document(str(source / "notes.txt"))
second = rag.import_document(str(source / "notes.txt"))
files = rag.list_documents()
check("导入允许的文本文件", first["path"] == "notes.txt" and (rag.DOCS_DIR / first["path"]).is_file())
check("同名导入不覆盖旧文件", second["path"] == "notes (2).txt" and len(files) == 2, str(files))

try:
    rag.import_document(str(source / "unsupported.pdf"))
    unsupported = False
except ValueError:
    unsupported = True
check("拒绝不支持的文件类型", unsupported)

check("重建会异步建立新索引", rag.rebuild_index())
deadline = time.time() + 3
while not rag._ready and time.time() < deadline:
    time.sleep(0.02)
check("导入文件已进入索引", rag._ready and any(
    c["source"] in {first["path"], second["path"]} for c in rag._index["chunks"]), str(rag._index["chunks"]))

try:
    rag.delete_document("../notes.txt")
    traversal = False
except ValueError:
    traversal = True
check("删除拒绝路径穿越", traversal)
rag.delete_document(first["path"])
check("删除仅作用于选中的入库文件", not (rag.DOCS_DIR / first["path"]).exists() and (rag.DOCS_DIR / second["path"]).exists())

shutil.rmtree(root, ignore_errors=True)
print("ALL PASS")
