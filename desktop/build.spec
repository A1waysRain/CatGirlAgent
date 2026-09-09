# -*- mode: python ; coding: utf-8 -*-
# 猫娘桌面壳打包配置（单文件 exe）
# 用法: .venv\Scripts\pyinstaller desktop\build.spec --noconfirm
# 产物: dist\猫娘来咯.exe
#
# 说明:
#   - 静态资源 (index.html/css/js) 打进 _MEIPASS，backend 冻结模式下自动定位；
#   - pywebview 在 Windows 用 pythonnet/clr_loader 驱动 .NET + WebView2，需收集其原生库；
#   - uvicorn 按需加载多种协议/循环实现，全部收集避免运行时 ImportError；
#   - 排除其他 GUI 库，减小体积。
#   - 重要: .env 不要打包，DeepSeek Key 由用户级 %APPDATA%\catgirl\config.json 提供。

import os

from PyInstaller.utils.hooks import (
    collect_submodules,
    collect_data_files,
    collect_dynamic_libs,
)

# spec 位于 desktop/，项目根是其上一级；所有路径都用绝对路径，避免相对 spec 目录错位
PROJECT_ROOT = os.path.dirname(SPECPATH)

# 网页前端静态资源 -> 解包到 _MEIPASS 根目录（backend/main.py 冻结时指向这里）
datas = [
    (os.path.join(PROJECT_ROOT, "index.html"), "."),
    (os.path.join(PROJECT_ROOT, "index_m.html"), "."),
    (os.path.join(PROJECT_ROOT, "index_m_login.html"), "."),
    (os.path.join(PROJECT_ROOT, "css"), "css"),
    (os.path.join(PROJECT_ROOT, "js"), "js"),
    # 桌面宠物形象（config + PNG）内嵌兜底；exe 旁的 pet_assets 优先、可编辑换形象
    (os.path.join(PROJECT_ROOT, "pet_assets"), "pet_assets"),
    # 聊天头像（user.jpg / cat.png）内嵌兜底；exe 旁的 img 优先、可替换
    (os.path.join(PROJECT_ROOT, "img"), "img"),
    # 图片识别 OCR：rapidocr 内置 ONNX 模型（不打进 exe 则 OCR 缺模型）
    *collect_data_files("rapidocr_onnxruntime"),
    # MaaFramework worker 脚本：非 import 引用（由 maa_client 以文件路径拉起），
    # 必须显式打包进 _MEIPASS/backend/maa_ops/，让 exe 旁的嵌入版 Python 能找到
    (os.path.join(PROJECT_ROOT, "backend", "maa_ops", "worker.py"), "backend/maa_ops"),
]

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("webview")
    + collect_submodules("clr_loader")
    + collect_submodules("httpcore")
    + collect_submodules("anyio")
    + ["bottle", "websockets", "httptools", "desktop.pet", "desktop.screenshot", "pystray._win32",
       "docx", "pptx", "openpyxl"]  # 文档读写/修改（python-docx / python-pptx / openpyxl）
    + collect_submodules("cv2")
    + collect_submodules("onnxruntime")
    + collect_submodules("rapidocr_onnxruntime")
    + ["numpy"]  # 图片识别 OCR
    # RAG 知识库（backend/rag.py）：fastembed 是懒加载（模块顶层不 import），
    # PyInstaller 静态分析发现不了，必须显式收集 + 带上它的依赖（tokenizers/mmh3 等是 .pyd）
    + collect_submodules("fastembed")
    + collect_submodules("huggingface_hub")
    + ["loguru", "tqdm", "requests", "mmh3", "tokenizers", "py_rust_stemmers"]
)

binaries = (
    collect_dynamic_libs("pythonnet")
    + collect_dynamic_libs("clr_loader")
    + collect_dynamic_libs("onnxruntime")
    + collect_dynamic_libs("cv2")
    # fastembed 的 Rust/C 扩展（tokenizers 分词器 / mmh3 哈希 / py_rust_stemmers 词干）
    + collect_dynamic_libs("tokenizers")
    + collect_dynamic_libs("mmh3")
    + collect_dynamic_libs("py_rust_stemmers")
)

a = Analysis(
    [os.path.join(SPECPATH, "app.py")],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 注意: 不能排除 tkinter —— 桌面宠物窗口依赖它
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "gtk"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="猫娘来咯",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="assets/cat.ico",  # 可选：放一个 256x256 的 ico 后取消注释
)
