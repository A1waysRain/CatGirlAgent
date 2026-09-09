# -*- coding: utf-8 -*-
"""验证 WebView2 后台防卡参数确实进入了浏览器进程命令行。

做法：import desktop.app（其模块级代码会设置 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS）
→ 拉起一个最小 pywebview 窗口 → 等浏览器进程起来 → 用 PowerShell 枚举本机所有
msedgewebview2.exe 的命令行，核对 4 个参数是否都在。输出 PASS/FAIL 后自动关窗退出。

用法（从 Cat_Girl 目录）：.venv/Scripts/python _verify_webview2_flags.py
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webview  # noqa: E402
import desktop.app  # noqa: E402  # 模块级执行 env var 注入（真实代码路径）

FLAGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-gpu",
]

PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dump_wv2.ps1")


def dump_webview2_cmdlines() -> str:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PS1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return r.stdout


def main() -> None:
    print("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS =", os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"))

    window = webview.create_window(
        "verify-webview2-flags",
        html="<h1 style='font-family:monospace'>WebView2 flags check</h1>",
        width=480,
        height=320,
    )

    def _check() -> None:
        time.sleep(4)  # 等浏览器进程起来
        out = dump_webview2_cmdlines()
        print("--- msedgewebview2.exe cmdline dump ---")
        print(out[:4000])
        hit = [f for f in FLAGS if f in out]
        miss = [f for f in FLAGS if f not in out]
        print("HIT :", hit)
        print("MISS:", miss)
        print("RESULT:", "PASS" if not miss else "FAIL")
        try:
            window.destroy()
        except Exception:
            os._exit(0)

    webview.start(_check)


if __name__ == "__main__":
    main()
