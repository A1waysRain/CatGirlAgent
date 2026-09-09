# -*- coding: utf-8 -*-
"""mock 验证：ui_* 目标应用没开时自动启动再重试，且不影响正常路径。"""
import sys; sys.path.insert(0, ".")
import backend.tools as T

t = lambda text, x, y, w, h: {"text": text, "box": [x, y, w, h]}
results = [
    t("文件传输助手", 151, 78, 128, 22),       # 搜索框
    t("搜索网络结果", 160, 133, 129, 23),
    t("文件传输助手", 162, 189, 126, 24),       # 网络结果1(精确)
    t("文件传输助手打开", 164, 246, 167, 24),
    t("文件传输助手", 202, 378, 144, 23),       # ★联系人
    t("聊天记录", 126, 631, 84, 23),
]
CONTACT_CLICK = (202 + int(144*0.6), 378 + 23//2)   # (288, 389)

class FakeClient:
    def __init__(self, fail_attaches):
        self.fail_attaches = fail_attaches
        self.attaches = 0; self.n = 0; self.clicks = []
        self.frames = [[t("搜索", 61, 60, 40, 22)], results]
    def attach(self, w):
        self.attaches += 1
        if self.attaches <= self.fail_attaches:
            raise RuntimeError("找不到窗口")
    def observe(self, out, ocr=False):
        f = self.frames[min(self.n, len(self.frames)-1)]; self.n += 1
        return {"texts": f}
    def click(self, x, y): self.clicks.append((x, y))
    def type(self, text): pass

def run(label, fail_attaches, launch_ok, expect_launch, expect_click):
    fake = FakeClient(fail_attaches)
    T._ui_client = lambda: fake
    T._log = lambda *a, **k: None
    launched = []
    T._launch_app_quiet = lambda n: (launched.append(n), launch_ok)[1]
    T.time.sleep = lambda s: None
    msg = T.tool_ui_search_contact("微信", "文件传输助手")
    hit = fake.clicks[-1] if fake.clicks else None
    ok = (launched == (["微信"] if expect_launch else [])
          and hit == expect_click
          and ("点开了聊天" if expect_click else "失败") in msg)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: 启动={launched} 点击={hit}"
          + (f"\n    msg={msg[:90]}" if not ok else ""))
    return ok

ok = True
ok &= run("微信已开(不启动)", 0, True, False, CONTACT_CLICK)
ok &= run("微信没开→自动启动→重试", 1, True, True, CONTACT_CLICK)
ok &= run("启动失败→报错不硬来", 1, False, True, None)
print("ALL PASS" if ok else "HAS FAILURES")
