# 端到端：image_vision 插件 mode=vision 真机看图（消耗真实 DeepSeek token）
# 用法：PYTHONIOENCODING=utf-8 .venv\Scripts\python.exe _e2e_vision.py [图片路径]
# 默认用 Cat_Girl/img/cat.png（猫娘头像）。传自定义图片路径可测截图/图表/照片。
import importlib.util
import os
import sys
import time

PLUGIN_PATH = os.path.join(os.environ["APPDATA"], "catgirl", "plugins", "image_vision", "plugin.py")
spec = importlib.util.spec_from_file_location("image_vision_plugin", PLUGIN_PATH)
p = importlib.util.module_from_spec(spec)
sys.modules["image_vision_plugin"] = p
spec.loader.exec_module(p)

img = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "img", "cat.png")
if not os.path.isfile(img):
    print(f"❌ 图片不存在：{img}")
    sys.exit(1)

print(f"→ mode=vision 分析：{img}（真实 DeepSeek 视觉模型，首次调用可能较慢）")
t0 = time.time()
result = p.read_image(img, mode="vision")
dt = time.time() - t0
print(f"（耗时 {dt:.1f}s）")
print("---- 返回 ----")
print(result)
print("----")
if len(result.strip()) > 20 and "看不了图" not in result and "连不上" not in result:
    print("✅ 视觉描述返回成功")
    sys.exit(0)
print("❌ 视觉描述异常")
sys.exit(1)
