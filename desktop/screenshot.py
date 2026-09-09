"""全屏区域截图选择器（独立进程，镜像桌宠的 Tkinter 模式）。

用法: python desktop/screenshot.py --out <保存的 PNG 路径>
- 全屏半透明遮罩 + 拖拽选区（选区挖空显示原画面）；松开鼠标截图保存；Escape 取消。
- 进程 DPI aware，坐标与 ImageGrab 的物理像素一致。
"""

import argparse
import ctypes
import sys
import tkinter as tk
from pathlib import Path

from PIL import ImageGrab

MAGENTA = "#ff00fe"  # 用作 transparentcolor 的挖空色（该色像素变全透明）


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="保存的 PNG 路径")
    # 打包版由 app 模式以 `exe --screenshot --out ...` 拉起，用 known_args 忽略 --screenshot
    args, _ = parser.parse_known_args()

    try:  # DPI aware：坐标对齐物理像素，避免高分屏错位
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    user32 = ctypes.windll.user32
    vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
    vy = user32.GetSystemMetrics(77)
    vw = user32.GetSystemMetrics(78)
    vh = user32.GetSystemMetrics(79)

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.45)  # 整窗半透明（其余区域压暗）
    try:
        root.attributes("-transparentcolor", MAGENTA)  # 选区挖空，露出原画面
    except Exception:
        pass
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.configure(bg="#141428")

    canvas = tk.Canvas(root, cursor="cross", bg="#141428", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    start = [0, 0]
    rect_id = None

    def on_press(e):
        start[:] = [e.x_root, e.y_root]

    def on_drag(e):
        nonlocal rect_id
        if rect_id is not None:
            canvas.delete(rect_id)
        rect_id = canvas.create_rectangle(
            start[0], start[1], e.x_root, e.y_root,
            fill=MAGENTA, outline="#ff4757", width=2,
        )

    def on_release(e):
        nonlocal rect_id
        x0, y0 = min(start[0], e.x_root), min(start[1], e.y_root)
        x1, y1 = max(start[0], e.x_root), max(start[1], e.y_root)
        if x1 - x0 < 5 or y1 - y0 < 5:
            if rect_id is not None:
                canvas.delete(rect_id)
            return  # 拖太小忽略，可继续拖
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        ImageGrab.grab(bbox=(x0, y0, x1, y1)).save(out)
        root.destroy()

    def on_esc(_e):
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", on_esc)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
