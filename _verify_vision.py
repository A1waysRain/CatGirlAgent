# 验证：image_vision 插件 mode=vision（DeepSeek 视觉模型"真正看图"，2026-08-25）
#  1) schema：mode 枚举含 vision、description 教猫娘何时用哪档、question 参数
#  2) _vision_describe：默认提问 / 自定义提问 / mock 描述解析 / 空 content 回退
#  3) 错误处理：无 Key 引导话术 / 网络异常兜底 / 模型名环境变量覆盖
#  4) 图片预处理：>2000px 等比缩小 / 小图不动 / data URL 格式正确 / 超时放宽
#  5) read_image：mode=vision 含 info+描述且不触发 OCR；mode=all 不含 vision（控费）
#     ；非图片 / 找不到路径沿用现有报错
#
# 全程 mock httpx，不消耗真实 token。
import base64
import importlib.util
import io
import os
import shutil
import sys
import tempfile

PLUGIN_PATH = os.path.join(os.environ["APPDATA"], "catgirl", "plugins", "image_vision", "plugin.py")
if not os.path.isfile(PLUGIN_PATH):
    print(f"❌ 找不到插件：{PLUGIN_PATH}")
    sys.exit(1)

# 从真实部署路径导入插件（插件顶层只 import os/sys/traceback，无副作用）
spec = importlib.util.spec_from_file_location("image_vision_plugin", PLUGIN_PATH)
p = importlib.util.module_from_spec(spec)
sys.modules["image_vision_plugin"] = p
spec.loader.exec_module(p)

from PIL import Image  # noqa: E402

PASS = FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


# ---- 临时素材 ----
_tmp = tempfile.mkdtemp(prefix="verify_vision_")
img_small = os.path.join(_tmp, "small.png")
Image.new("RGB", (120, 90), (200, 30, 60)).save(img_small)
img_big = os.path.join(_tmp, "big.png")
Image.new("RGB", (3000, 2000), (30, 120, 200)).save(img_big)
txt_file = os.path.join(_tmp, "not_img.txt")
with open(txt_file, "w", encoding="utf-8") as f:
    f.write("我不是图片")


# ---- mock httpx ----
import httpx  # noqa: E402
_ORIG_CLIENT = httpx.Client
_PATCH = {"content": "描述A", "err": None, "call": None, "timeout": None}


class FakeResp:
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


class FakeClient:
    def __init__(self, timeout=None):
        _PATCH["timeout"] = timeout

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        _PATCH["call"] = {"url": url, "headers": headers, "payload": json}
        if _PATCH["err"] == "http":
            raise httpx.HTTPError("network down")
        return FakeResp(_PATCH["content"])


def patch_httpx():
    httpx.Client = FakeClient


def restore_httpx():
    httpx.Client = _ORIG_CLIENT


def last_payload():
    return _PATCH["call"]["payload"]


# ---- 1. schema ----
print("== 1. schema ==")
func = p.register()[0]["schema"]["function"]
mode_enum = func["parameters"]["properties"]["mode"]["enum"]
ok("mode 枚举 = [all,info,ocr,vision]", mode_enum == ["all", "info", "ocr", "vision"], str(mode_enum))
desc = func["description"]
ok("description 教猫娘 vision 什么时候用", "真正看图" in desc and "抠" in desc, desc[:80])
ok("question 参数存在且非必填", "question" in func["parameters"]["properties"] and "question" not in func["parameters"].get("required", []))
ok("path 仍必填", func["parameters"]["required"] == ["path"], str(func["parameters"]["required"]))

# ---- 2. _vision_describe 基本 ----
print("== 2. _vision_describe ==")
patch_httpx()
try:
    _PATCH["content"] = "描述A：图里有一张桌子"
    r = p._vision_describe(img_small)
    ok("mock 描述正确解析", r == "描述A：图里有一张桌子", repr(r))
    payload = last_payload()
    ok("model 是视觉模型", payload["model"] == p._VISION_MODEL, payload.get("model"))
    ok("content 是多模态列表", isinstance(payload["messages"][0]["content"], list) and len(payload["messages"][0]["content"]) == 2)
    parts = payload["messages"][0]["content"]
    ok("第一段 text 用默认文案", parts[0]["type"] == "text" and "请用中文简洁描述" in parts[0]["text"], parts[0]["text"])
    ok("第二段 image_url 是 data URL", parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
    ok("温度 0.2 少脑补", payload.get("temperature") == 0.2)
    ok("stream=False", payload.get("stream") is False)
    ok("请求头带 Bearer Key", _PATCH["call"]["headers"]["Authorization"].startswith("Bearer "))
    ok("超时放宽到 ≥90s", _PATCH["timeout"] >= 90, str(_PATCH["timeout"]))

    _PATCH["content"] = "描述B"
    r = p._vision_describe(img_small, question="图里有几个时间点")
    q = last_payload()["messages"][0]["content"][0]["text"]
    ok("自定义提问生效", q == "图里有几个时间点", repr(q))

    _PATCH["content"] = "    "
    r = p._vision_describe(img_small)
    ok("空 content 回退 OCR 提示", "OCR" in r, repr(r))

    # ---- 3. 错误处理 ----
    print("== 3. 错误处理 ==")
    from backend.config import settings as s
    _orig_key = s.deepseek_api_key
    try:
        s.deepseek_api_key = ""
        r = p._vision_describe(img_small)
        ok("无 Key 返回引导话术", "配好 API Key" in r, repr(r))
    finally:
        s.deepseek_api_key = _orig_key

    _PATCH["err"] = "http"
    r = p._vision_describe(img_small)
    _PATCH["err"] = None
    ok("网络异常返回兜底话术", "连不上 DeepSeek" in r, repr(r))

    os.environ["DEEPSEEK_VISION_MODEL"] = "my-vision-v1"
    try:
        spec2 = importlib.util.spec_from_file_location("image_vision_plugin_env", PLUGIN_PATH)
        p2 = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(p2)
        ok("模型名可被环境变量覆盖", p2._VISION_MODEL == "my-vision-v1", p2._VISION_MODEL)
    finally:
        del os.environ["DEEPSEEK_VISION_MODEL"]

    # ---- 4. 图片预处理 ----
    print("== 4. 图片预处理 ==")
    _PATCH["content"] = "x"
    p._vision_describe(img_big)
    b64 = last_payload()["messages"][0]["content"][1]["image_url"]["url"].split(",", 1)[1]
    im2 = Image.open(io.BytesIO(base64.b64decode(b64)))
    ok("大图(3000×2000)被等比缩小", im2.size[0] <= 2000 and im2.size[1] <= 2000, str(im2.size))

    p._vision_describe(img_small)
    b64s = last_payload()["messages"][0]["content"][1]["image_url"]["url"].split(",", 1)[1]
    im3 = Image.open(io.BytesIO(base64.b64decode(b64s)))
    ok("小图(120×90)不被缩放", im3.size == (120, 90), str(im3.size))
finally:
    restore_httpx()

# ---- 5. read_image 分发 ----
print("== 5. read_image 分发 ==")
r = p.read_image(txt_file, mode="vision")
ok("非图片报错", "不是可读取的图片" in r, repr(r))
r = p.read_image(os.path.join(_tmp, "no_such_xyz.png"), mode="vision")
ok("找不到路径报错", "找不到这个图片" in r, repr(r))

orig_ocr = p._ocr_text
orig_vis = p._vision_describe
p._ocr_text = lambda path: "OCR-STUB"
p._vision_describe = lambda path, q="": "VISION-STUB"
try:
    r = p.read_image(img_small, mode="vision")
    ok("vision 含 info+描述", "120×90" in r and "VISION-STUB" in r, r)
    ok("vision 不触发 OCR", "OCR-STUB" not in r, r)
    r2 = p.read_image(img_small, mode="all")
    ok("all 含 OCR", "OCR-STUB" in r2, r2)
    ok("all 不含 vision（控费）", "VISION-STUB" not in r2, r2)
    r3 = p.read_image(img_small, mode="info", question="随便问问")
    ok("info 带 question 正常", "120×90" in r3 and "VISION-STUB" not in r3 and "OCR-STUB" not in r3, r3)
finally:
    p._ocr_text = orig_ocr
    p._vision_describe = orig_vis

shutil.rmtree(_tmp, ignore_errors=True)
print(f"\n结果: {PASS} 过 / {FAIL} 挂")
sys.exit(1 if FAIL else 0)
