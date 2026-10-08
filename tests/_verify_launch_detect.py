"""验证「打开应用」确定性兜底 + launch_app 前台处理（隔离环境，不真启动应用）。

覆盖：
1. _extract_launch_request 各种输入（打开X / 打开X和Y / 带文件 / 设置 / 路径…）
2. _auto_launch_apps 打桩 run_tool：只启动应用表里匹配的，成功/失败分开
3. tool_launch_app 打桩 os.startfile/Popen/_top_windows/_focus_new_windows：走三分支+前台逻辑
4. 端到端 chat 端点：说「打开原神和微信」→ 两个 launch_app 都被真调、且只各调一次
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, ".")

# ---- 隔离 APPDATA，避免污染真实配置/日志；预置一个最小应用表 ----
_tmp = tempfile.mkdtemp(prefix="catgirl_verify_launch_")
os.environ["APPDATA"] = _tmp
os.environ["CATGIRL_SKIP_PET"] = "1"
os.makedirs(os.path.join(_tmp, "catgirl"), exist_ok=True)
with open(os.path.join(_tmp, "catgirl", "apps.json"), "w", encoding="utf-8") as f:
    json.dump({"微信": "C:\\fake\\Weixin.exe", "原神": "D:\\fake\\YuanShen.exe"}, f, ensure_ascii=False)

from backend.routers.chat import _extract_launch_request, _auto_launch_apps
from backend.tools import tool_launch_app

ok = 0
fail = 0


def check(label, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  ✓ {label}")
    else:
        fail += 1
        print(f"  ✗ {label}\n      got  {got!r}\n      want {want!r}")


print("== _extract_launch_request ==")
cases = [
    ("打开原神和微信", ["原神", "微信"]),
    ("帮我打开微信", ["微信"]),
    ("帮我打开原神", ["原神"]),
    ("帮我打开原神和微信、Word", ["原神", "微信", "Word"]),
    ("打开微信呀", ["微信"]),
    ("启动原神", ["原神"]),
    ("打开桌面上的报告.docx", None),        # 带扩展名 → 不是应用
    ("打开E:\\games\\game.exe", None),       # 路径 → 不是应用
    ("打开设置", ["设置"]),                   # 会解析出来，但 _auto_launch_apps 无匹配则不动
    ("帮我打开微信给王小明发消息", ["微信", "王小明发消息"]),  # 微信是应用，剩余交给模型
    ("今天天气怎么样", None),
    ("我不想打开任何东西", None),             # 开头不是打开动词
    ("打开一个记事本", ["记事本"]),
    ("帮我打开原神和微信再打开计算器", ["原神", "微信", "计算器"]),
    ("", None),
    ("打开吧", None),                        # 后面没名字
]
for text, want in cases:
    check(f"_extract_launch_request({text!r})", _extract_launch_request(text), want)


print("== _auto_launch_apps（打桩 run_tool）==")
import backend.tools as tools_mod
_real_tool_impl = tools_mod.TOOL_IMPL["launch_app"]
calls = []
tools_mod.TOOL_IMPL["launch_app"] = lambda name="": calls.append(name) or f"已经帮你启动 {name} 喵"
try:
    ok_launch, fail_launch = _auto_launch_apps(["原神", "微信", "不存在的应用", "计算器"])
    check("成功列表", ok_launch, ["原神", "微信", "计算器"])
    check("失败列表", fail_launch, [])
    check("真调 launch_app 次数", calls, ["原神", "微信", "计算器"])
finally:
    tools_mod.TOOL_IMPL["launch_app"] = _real_tool_impl

print("== _auto_launch_apps 启动失败走 failed ==")
tools_mod.TOOL_IMPL["launch_app"] = lambda name="": "启动失败喵：路径不存在"
try:
    ok_l, fail_l = _auto_launch_apps(["原神"])
    check("失败场景成功列表", ok_l, [])
    check("失败场景失败列表", fail_l, ["原神"])
finally:
    tools_mod.TOOL_IMPL["launch_app"] = _real_tool_impl

print("== tool_launch_app（打桩 os.startfile/Popen/窗口快照）==")
_real_startfile = tools_mod.os.startfile
_real_popen = tools_mod.subprocess.Popen
_real_top = tools_mod._top_windows
_real_focus = tools_mod._focus_new_windows
startfile_calls = []
popen_calls = []
focused = []
tools_mod.os.startfile = lambda t: startfile_calls.append(t)
tools_mod.subprocess.Popen = lambda *a, **k: popen_calls.append(a)
tools_mod._top_windows = lambda: []
tools_mod._focus_new_windows = lambda before, max_wait=2.5: focused.append(max_wait)
try:
    startfile_calls.clear(); popen_calls.clear(); focused.clear()
    r = tool_launch_app("原神")   # apps.json 里有原神，fake 路径不存在 → 走 startfile 兜底
    check("launch_app 原神成功话术", "已经帮你启动 原神" in r, True)
    check("原神走 startfile（fake 路径非文件且不在 PATH）", len(startfile_calls) == 1, True)
    check("调了 _focus_new_windows 且 max_wait=1.0", focused, [1.0])
    focused.clear()
    r = tool_launch_app("计算器")   # 内置 calc.exe → 走 Popen 分支
    check("launch_app 计算器成功话术", "已经帮你启动 计算器" in r, True)
    check("计算器走 Popen", len(popen_calls) == 1, True)
    # 真实 Windows 下 startfile 找不到文件会抛异常 → 失败分支
    tools_mod.os.startfile = lambda t: (_ for _ in ()).throw(OSError("找不到文件"))
    r = tool_launch_app("不存在的应用X")
    check("launch_app 不存在不崩（startfile 抛错→失败话术）", "启动失败" in r, True)
finally:
    tools_mod.os.startfile = _real_startfile
    tools_mod.subprocess.Popen = _real_popen
    tools_mod._top_windows = _real_top
    tools_mod._focus_new_windows = _real_focus


print("== 端到端 chat 端点（打桩 launch_app + 真模型）==")
from fastapi.testclient import TestClient
from backend import main

e2e_calls = []
tools_mod.TOOL_IMPL["launch_app"] = lambda name="": e2e_calls.append(name) or f"已经帮你启动 {name} 喵"
client = TestClient(main.create_app(allowed_origins=None))
try:
    r = client.post("/api/chat_response", json={"chatmassage": "打开原神和微信"})
    check("端到端 200", r.status_code, 200)
    check("launch_app 被调且只调一次", e2e_calls, ["原神", "微信"])
    check("回复里提到已打开", "打开" in r.text or "喵" in r.text, True)
    check("每个应用只启动一次", e2e_calls.count("原神") == 1 and e2e_calls.count("微信") == 1, True)
finally:
    tools_mod.TOOL_IMPL["launch_app"] = _real_tool_impl


print(f"\n结果：{ok} 通过，{fail} 失败")
sys.exit(1 if fail else 0)
