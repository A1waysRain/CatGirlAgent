# -*- coding: utf-8 -*-
"""验证 create_pptx 主题色：内容关键词自动推断 / 显式 theme / 自定义 accent / 默认。"""
import os, sys, tempfile, shutil
tmp = tempfile.mkdtemp(prefix="catgirl_theme_")
os.environ["APPDATA"] = tmp
sys.path.insert(0, ".")
import backend.tools as T
from pptx import Presentation

def check(label, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))
    return cond

def make(decks, style=None, theme_key=None):
    p = os.path.join(tmp, f"t{theme_key}.pptx")
    r = T.tool_create_pptx(p, decks, style=style, confirmed=True)
    if "已经生成" not in r:
        return r, None, None
    prs = Presentation(p)
    bg = str(prs.slides[0].background.fill.fore_color.rgb)
    return r, bg, prs

ok = True
decks = [{"layout": "title", "title": "水务运营平台汇报"}, {"layout": "content", "title": "水质监测", "bullets": ["水源调度"]}]

# ① 内容含"水务"→ ocean 青
r, bg, _ = make(decks, theme_key="1")
ok &= check("水务→海洋青(ocean)", bg == "0F9D9D", f"bg={bg}")
ok &= check("返回带主题名", "海洋青" in r, r[:50])

# ② 财务内容 → business 蓝
r2, bg2, _ = make([{"layout":"title","title":"年度财务总结"},{"layout":"content","title":"营收","bullets":["预算","成本"]}], theme_key="2")
ok &= check("财务→商务蓝(business)", bg2 == "2E5AAC", f"bg={bg2}")

# ③ 显式 theme=nature → 自然绿
r3, bg3, _ = make(decks, {"theme": "nature"}, theme_key="3")
ok &= check("显式 nature→自然绿", bg3 == "43A047", f"bg={bg3}")

# ④ 自定义 accent 覆盖
r4, bg4, _ = make(decks, {"accent": "123456"}, theme_key="4")
ok &= check("自定义 accent=123456", bg4 == "123456", f"bg={bg4}")

# ⑤ 无关键词 → 默认猫娘粉
r5, bg5, _ = make([{"layout":"title","title":"随便什么标题"}], theme_key="5")
ok &= check("无关键词→猫娘粉", bg5 == "FF7BAC", f"bg={bg5}")

# ⑥ 医疗内容 → medical
r6, bg6, _ = make([{"layout":"title","title":"临床护理规范"},{"layout":"content","title":"患者管理"}], theme_key="6")
ok &= check("医疗→医疗青", bg6 == "00A9A0", f"bg={bg6}")

shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "HAS FAILURES")
