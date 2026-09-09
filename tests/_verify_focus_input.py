# -*- coding: utf-8 -*-
"""验证 worker._focus_chat_input 点击位置（真实 GetClientRect + mock _ocr/click）。"""
import sys; sys.path.insert(0, ".")
import ctypes, ctypes.wintypes
import backend.maa_ops.worker as W

def real_rect():
    r = ctypes.wintypes.RECT()
    hwnd = ctypes.windll.user32.GetDesktopWindow()
    ctypes.windll.user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    return r

w = W.Worker()
w.hwnd = ctypes.windll.user32.GetDesktopWindow()  # 真实句柄
w.clicks = []
w.click = lambda x, y: w.clicks.append((x, y))
rect = real_rect()

ok = True
# ① 找到发送按钮(600,900) → 点 (rect.right//2, 870)
w._ocr = lambda: [{"text": "微信搜索", "box": [10,10,50,20]}, {"text": "发送", "box": [600,900,40,22]}]
w._focus_chat_input()
r1 = w.clicks[0] if w.clicks else None
e1 = (rect.right // 2, 900 - 30)
good = r1 == e1
ok &= good
print(f"[{'PASS' if good else 'FAIL'}] 有发送按钮: 点击 {r1} 期望 {e1}")

# ② 无发送按钮 → 兜底点 (rect.right//2, rect.bottom-80)
w.clicks.clear()
w._ocr = lambda: [{"text": "某个聊天", "box": [100,200,100,20]}]
w._focus_chat_input()
r2 = w.clicks[0] if w.clicks else None
e2 = (rect.right // 2, max(rect.bottom - 80, 40))
good2 = r2 == e2
ok &= good2
print(f"[{'PASS' if good2 else 'FAIL'}] 无发送按钮: 点击 {r2} 期望 {e2}")

# ③ OCR 异常 → 静默跳过不点击
w.clicks.clear()
def boom(): raise RuntimeError("ocr失败")
w._ocr = boom
w._focus_chat_input()
good3 = w.clicks == []
ok &= good3
print(f"[{'PASS' if good3 else 'FAIL'}] OCR异常: 静默跳过, clicks={w.clicks}")

print("ALL PASS" if ok else "HAS FAILURES")
