"""路径与上传白名单：从 main.py 拆出（2026-08-16 路由拆分）。

被 main.py（静态资源挂载）/ routers/media.py（上传）/ routers/settings.py（头像）共用。
"""
import os
import sys
from pathlib import Path

# PyInstaller 冻结运行（单文件 exe）时静态资源在 _MEIPASS；开发时在项目根
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).resolve().parent.parent


def _avatar_dir() -> Path:
    """可写的头像目录。

    打包版用 exe 旁的 img（可替换不重打包），首启把内嵌默认头像复制过去；
    开发版直接是项目根 img/。
    """
    if getattr(sys, "frozen", False):
        d = Path(sys.executable).resolve().parent / "img"
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "img"
    else:
        d = BASE_DIR / "img"
        bundled = None
    d.mkdir(parents=True, exist_ok=True)
    if bundled:
        import shutil

        for name in ("user.jpg", "cat.png"):
            if not (d / name).exists() and (bundled / name).exists():
                try:
                    shutil.copy2(bundled / name, d / name)
                except Exception:
                    pass
    return d


def _media_dir() -> Path:
    """截图/上传图片目录（%APPDATA%/catgirl/media/），挂载为 /media。"""
    d = Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


# 通用文件上传允许的扩展名（文档/表格/演示/PDF/压缩包/文本/代码/音视频/图片）
_FILE_EXT = {
    ".txt", ".md", ".log", ".csv", ".json", ".xml", ".yml", ".yaml",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sql",
    ".mp3", ".wav", ".mp4", ".mkv",
}
