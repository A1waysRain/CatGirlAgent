"""桌面宠物：独立进程 + Tkinter 透明无边框置顶窗。

用法:
    python desktop/pet.py --base http://127.0.0.1:PORT

形象接口（换猫娘图片只看这一处）:
    pet_assets/pet_config.json 是唯一入口，路径相对该文件所在目录：
      "idle"     待机帧列表（多帧循环动画；单帧则静止+漂浮）
      "notify"   通知帧（来消息时播一遍，可空）
      "click"    点击帧（可空）
      "scale"    缩放系数（1.0 原大）
      "bubble_dy" 气泡相对宠物顶部的垂直偏移（负=向上）
      "float"    单帧时是否上下漂浮
      "moods"   情绪表情组 {情绪名: [文件名]}：happy/sad/sleep/excited/alert/
                angry/tease/reluctant/tsundere；缺素材或 think 回退待机（笑）
    换形象 = 改这个 json 里的路径，或往对应帧列表放新 PNG。
"""
import argparse
import json
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path

import tkinter as tk

import httpx

KEY = "#ff00fe"  # 透明抠色：形象图里尽量避免这个颜色

# 情绪事件统一使用 pet_config.json 的键。保留旧键和中文表情名兼容，避免历史事件或
# 手工 API 调用因命名变化静默回退到待机。
MOOD_ALIASES = {
    "surprised": "excited",
    "sleepy": "sleep",
    "笑": "happy",
    "哭唧唧": "sad",
    "困": "sleep",
    "惊讶": "excited",
    "慌张": "alert",
    "生气": "angry",
    "吓": "angry",
    "吐舌头": "tease",
    "不要啊": "reluctant",
    "拽": "tsundere",
    "思考": "think",
}


def normalize_mood(mood: str) -> str:
    """把兼容别名归一为 pet_config.json 使用的标准情绪键。"""
    value = (mood or "happy").strip().lower()
    return MOOD_ALIASES.get(value, value)


def assets_dir() -> Path:
    """形象目录定位：
    - 开发：Cat_Girl/pet_assets；
    - 打包：优先 exe 旁的可编辑 pet_assets（方便换形象），其次内嵌 _MEIPASS 兜底。
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        meipass = getattr(sys, "_MEIPASS", None)
        for cand in (exe_dir / "pet_assets", Path(meipass) / "pet_assets" if meipass else None):
            if cand and cand.is_dir():
                return cand
        return exe_dir / "pet_assets"
    return Path(__file__).resolve().parent.parent / "pet_assets"


ASSETS_DIR = assets_dir()
CONFIG_PATH = ASSETS_DIR / "pet_config.json"


def work_area() -> tuple[int, int, int, int]:
    """屏幕工作区（扣除任务栏），返回 (x, y, w, h)。

    用 SPI_GETWORKAREA 取主屏减去任务栏后的区域，任务栏在哪个方向都正确。
    """
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    r = RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def screen_area() -> tuple[int, int, int, int]:
    """整个屏幕（含任务栏区域），返回 (x, y, w, h)。

    贴边停靠的"底边判定"用它——按屏幕最底判定（用户要求），
    而不是任务栏上沿；停靠位置仍用 work_area 保证可见。
    """
    import ctypes

    return (
        0, 0,
        ctypes.windll.user32.GetSystemMetrics(0),  # SM_CXSCREEN
        ctypes.windll.user32.GetSystemMetrics(1),  # SM_CYSCREEN
    )


def sound_enabled() -> bool:
    """是否播放通知提示音（读用户设置 notification_sound）。"""
    try:
        f = Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "settings.json"
        if f.exists():
            return bool(json.loads(f.read_text(encoding="utf-8")).get("notification_sound", True))
    except Exception:
        pass
    return True


def walk_enabled() -> bool:
    """是否允许闲时踱步（读用户设置 pet_walk，默认 True）。"""
    try:
        f = Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "settings.json"
        if f.exists():
            return bool(json.loads(f.read_text(encoding="utf-8")).get("pet_walk", True))
    except Exception:
        pass
    return True


def pid_alive(pid: int | None) -> bool:
    """判断进程是否存活（Windows）。

    必须用 WaitForSingleObject 看信号态，而不是 OpenProcess 成败：
    进程被杀后对象会短暂残留，OpenProcess 仍能打开造成误判。
    """
    if pid is None:
        return True
    try:
        import ctypes

        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            # 0x102(258)=WAIT_TIMEOUT=还在跑；0=WAIT_OBJECT_0=已终止
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return True


try:
    from PIL import Image, ImageTk

    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def load_config() -> dict:
    """读取形象配置，缺省时用默认单图（../临时形象.png）。"""
    default = {
        "idle": ["临时形象.png"],
        "notify": [],
        "click": [],
        "scale": 1.0,
        "bubble_dy": -60,
        "float": True,
    }
    if CONFIG_PATH.exists():
        try:
            default.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"[宠物] 读取 {CONFIG_PATH.name} 失败，使用默认形象：", e)
    base = CONFIG_PATH.parent if CONFIG_PATH.exists() else ASSETS_DIR
    for key in ("idle", "notify", "click"):
        default[key] = [str((base / p).resolve()) for p in (default.get(key) or [])]
    # 情绪表情组（moods：{情绪名: [文件名]}），路径同样相对 config 目录
    default["moods"] = {
        mood: [str((base / p).resolve()) for p in (paths or [])]
        for mood, paths in (default.get("moods") or {}).items()
    }
    return default


def load_source_image(path: str):
    """加载原图（PIL RGBA），失败返回 None。

    会自动裁掉四周的透明边：不规则形象常有大量留白，窗口若按整图算，
    缩放热区会落在看不见的空区里导致无法缩放。
    """
    p = Path(path)
    if not p.exists():
        print(f"[宠物] 找不到形象文件: {p}")
        return None
    try:
        if HAS_PIL:
            im = Image.open(p).convert("RGBA")
            bbox = im.getchannel("A").getbbox()
            if bbox:
                pad = 2  # 留一点内边距，别裁掉半透明描边
                x0, y0, x1, y1 = bbox
                x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
                x1, y1 = min(im.width, x1 + pad), min(im.height, y1 + pad)
                im = im.crop((x0, y0, x1, y1))
            im = _binarize_alpha(im)
            return im
        print("[宠物] 缺少 Pillow，无法加载形象")
        return None
    except Exception as e:
        print(f"[宠物] 加载形象失败 {p}：", e)
        return None


def _binarize_alpha(im):
    """把半透明毛边做成二值 alpha：彻底透明（<128）或彻底不透明（≥128）。

    窗口用色键透明（-transparentcolor #ff00fe 紫色），显示半透明像素时会和
    紫色底混合，在形象边缘形成一圈紫色光圈。二值化后毛边不再混紫。
    只动 alpha 通道，RGB 原样保留；轻微牺牲抗锯齿圆润度，换干净轮廓。
    """
    r, g, b, a = im.split()
    a = a.point(lambda v: 255 if v >= 128 else 0)
    return Image.merge("RGBA", (r, g, b, a))


# ---- 缩放热区常量 ----
MIN_W = 40
MIN_H = 40
MAX_W = 1200
MAX_H = 1200
EDGE = 8       # 边缘热区厚度
CORNER = 14    # 角落热区边长

# M2 行为状态机常量（行为循环 ~33ms/tick ≈ 30fps，移动才顺滑）
WALK_AFTER = 20      # 空闲多少秒后开始踱步
WALK_STEP = 1        # 踱步每 tick 移动像素（33ms tick 下 ≈ 30px/s）
WALK_HOP = 9         # 踱步每步的起伏幅度（像素）——制造"蹬步"感，别像幽灵平移
WALK_RHYTHM = 0.15   # 脚步节奏：每 tick 相位推进（≈0.7s 一步）
SLEEP_AFTER = 1800   # 空闲多少秒后入睡（30 分钟）

# 提醒气泡展示时长：太短（4 秒）人没看到就没了，太长又一直挂着占屏、遮表情。
# 15 秒足够扫一眼，想早关点一下即可（on_notification 传 clickable=True）。
REMINDER_TIMEOUT = 15000  # 毫秒

# 情绪超时：非默认情绪（傲娇/生气/委屈/吐舌头等）显示多少秒后自动回到待机。
# 后端每次聊天都会推情绪，若情绪一直卡着，桌宠会定格在那一张脸上
# （此前还因此挡住过贴边停靠的滑出）。挂个超时到点回笑。
MOOD_TIMEOUT = 20  # 秒

# 停靠滑出判定（严格：鼠标需真正贴近侧边才滑出）
DOCK_NEAR = 40       # 距停靠边多近才滑出
DOCK_FAR = 90        # 离开多远才收回（迟滞防抖）
CURSORS = {
    "nw": "size_nw_se", "se": "size_nw_se",
    "ne": "size_ne_sw", "sw": "size_ne_sw",
    "n": "size_ns", "s": "size_ns",
    "e": "size_we", "w": "size_we",
}


def state_file() -> Path:
    """尺寸记忆文件：记录桌宠最后一次的显示尺寸，下次启动沿用。"""
    return ASSETS_DIR / "pet_state.json"


def _load_saved_size() -> tuple[int, int] | None:
    """读取记忆的尺寸；缺失/非法则返回 None。"""
    try:
        f = state_file()
        if not f.exists():
            return None
        d = json.loads(f.read_text(encoding="utf-8"))
        w, h = int(d.get("width", 0)), int(d.get("height", 0))
        if MIN_W <= w <= MAX_W and MIN_H <= h <= MAX_H:
            return w, h
    except Exception:
        pass
    return None


class Pet:
    def __init__(self, base_url: str, cfg: dict, parent_pid: int | None = None):
        self.base_url = base_url
        self.cfg = cfg
        self.q = queue.Queue()
        self._stop = False
        self.parent_pid = parent_pid

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", KEY)
        self.root.configure(bg=KEY)

        self.canvas = tk.Canvas(self.root, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack()

        # 保留原图（可自由缩放），展示尺寸 = 原图 × 配置 scale
        self.src_idle = [im for im in (load_source_image(p) for p in cfg["idle"]) if im]
        self.src_notify = [im for im in (load_source_image(p) for p in cfg["notify"]) if im]
        # 情绪表情组：moods -> [PIL 原图]（缺素材的情绪不登记，查表时回退 idle）
        self.src_moods: dict = {}
        for mood, paths in (cfg.get("moods") or {}).items():
            imgs = [im for im in (load_source_image(p) for p in paths) if im]
            if imgs:
                self.src_moods[mood] = imgs
        # 踱步用：水平翻转版待机图（抵达边缘回身时形象朝向行进方向）
        self.src_idle_flip = [im.transpose(Image.FLIP_LEFT_RIGHT) for im in self.src_idle]

        if self.src_idle:
            self.base_w, self.base_h = self.src_idle[0].size
        else:
            self.base_w, self.base_h = 140, 140
        scale = float(cfg.get("scale", 1.0))
        self.w = max(MIN_W, int(self.base_w * scale))
        self.h = max(MIN_H, int(self.base_h * scale))
        # 优先沿用上次手动调好的尺寸（记忆文件）
        saved = _load_saved_size()
        if saved:
            self.w, self.h = saved
        # 启动位置：屏幕工作区（不含任务栏）右下角，留 6px 边距，不挡任务栏
        wx, wy, ww, wh = work_area()
        start_x = max(wx, wx + ww - self.w - 6)
        start_y = max(wy, wy + wh - self.h - 6)
        self.root.geometry(f"{self.w}x{self.h}+{start_x}+{start_y}")
        self.canvas.configure(width=self.w, height=self.h)

        self.canvas_image_id = None
        self.photo_refs = []  # 保持 PhotoImage 引用防被回收
        self._photos_idle: list = []
        self._photos_idle_flip: list = []
        self._photos_notify: list = []
        self._photos_moods: dict = {}
        self._mood = None  # 当前情绪（None/happy = 默认待机态）
        self._mood_job = None  # 情绪超时定时器（after id，到点自动回待机）
        self._rebuild_photos()
        self.idx = 0
        self._anim_job = None
        self._idle_running = False
        self._float_t = 0.0
        self._float_base_y = None
        self._bubble = None
        self._bubble_timeout_job = None  # 普通气泡自动关闭定时器（after id）
        self._persist_bubbles = {}  # key -> (bub, canvas, text_id, w, h)：可更新持久气泡
        self._persist_timeout_jobs = {}  # 持久气泡 key -> 自动关闭定时器（None=常驻）
        self._notify_busy = False

        self.drag_start = None
        self.dragged = False
        self.resize_region = None
        # M5 交互：双击问候 / 长按蹭蹭
        self._single_click_job = None  # 单击延迟唤起主窗（双击时取消）
        self._longpress_job = None     # 长按定时器
        self._longpress_fired = False  # 长按是否已触发（防 release 误开主窗）

        # M2 行为状态机 + 贴边停靠
        self._behavior = "idle"       # idle / walk / sleep
        self._behavior_job = None
        self._last_activity = time.time()
        self._walk_dir = -1
        self._walk_facing = 1         # 踱步朝向：1=默认(朝左), -1=水平翻转(朝右)
        self._walk_phase = 0.0
        self._sleep_phase = 0.0
        self._zzz_id = None
        self._docked = False
        self._dock_side = None
        self._dock_job = None
        self._dock_target = None  # 停靠态平滑滑动目标位置
        self._dock_out = False    # 当前是否已滑出（迟滞用）
        self._dock_slide_job = None
        self._slide_pos = None    # 滑动内部追踪位置（不依赖 winfo，防滞后抖动）
        self._dock_mouse_far = False  # 停靠后鼠标是否离开过边缘（离开再回来才弹出=取回手势）

        self._draw_initial()
        self._build_menu()
        self._bind_events()
        self._resume_idle()  # 启动待机动画（内部 pause-first 防重复）
        self._behavior_loop()
        self._dock_hover_check()
        self._dock_slide_loop()

    # ---------- 形象显示 ----------
    def _photo_from_src(self, img):
        if img.size != (self.w, self.h):
            img = img.resize((self.w, self.h), Image.LANCZOS)
        return ImageTk.PhotoImage(img)

    def _rebuild_photos(self):
        """尺寸变化时按当前 w/h 重新生成全部帧（idle + 翻转 + notify + 各情绪）。"""
        self._photos_idle = [self._photo_from_src(im) for im in self.src_idle]
        self._photos_idle_flip = [self._photo_from_src(im) for im in self.src_idle_flip]
        self._photos_notify = [self._photo_from_src(im) for im in self.src_notify]
        self._photos_moods = {
            mood: [self._photo_from_src(im) for im in imgs]
            for mood, imgs in self.src_moods.items()
        }

    def _show_current_photo(self):
        if self._photos_idle:
            self.show_frame(self._photos_idle[self.idx % len(self._photos_idle)])
        elif self._photos_notify:
            self.show_frame(self._photos_notify[0])

    # ---------- 情绪表情 ----------
    def _in_emotion(self) -> bool:
        """当前是否处于非默认的情绪态（happy/idle 视为默认待机）。"""
        return self._mood not in (None, "happy", "idle")

    def _cancel_mood_job(self):
        """取消情绪超时定时器（新情绪/清情绪/入睡时都调用，防旧定时器误触发）。"""
        if self._mood_job:
            try:
                self.root.after_cancel(self._mood_job)
            except tk.TclError:
                pass
            self._mood_job = None

    def _set_mood(self, mood: str):
        """切换情绪表情。未知/缺素材回退 idle；happy 视为默认待机态（笑）。

        非默认情绪挂一个 MOOD_TIMEOUT 秒的超时，到点自动回待机（_clear_mood），
        防止后端推来的情绪一直卡着定格表情（此前还挡住过停靠滑出）。
        """
        mood = normalize_mood(mood)
        self._touch()  # 情绪事件也是活动（唤醒 + 重置空闲计时）
        self._cancel_mood_job()  # 新情绪覆盖旧的超时
        photos = self._photos_moods.get(mood)
        if mood == "happy" or not photos:
            self._mood = None  # 回到待机（笑.png 就是 idle）
            self._show_current_photo()
            self._resume_idle()
            print(f"[宠物] 情绪 -> 待机（{mood}）", flush=True)
            return
        self._mood = mood
        self._pause_idle()
        self.show_frame(photos[0])
        self._mood_job = self.root.after(int(MOOD_TIMEOUT * 1000), self._clear_mood)
        print(f"[宠物] 情绪 -> {mood}（{MOOD_TIMEOUT}s 后回待机）", flush=True)

    def _clear_mood(self):
        """情绪超时到点：回到默认待机态（笑）。"""
        self._mood_job = None
        if self._in_emotion():
            self._mood = None
            self._restore_display()
            print("[宠物] 情绪超时，回到待机喵", flush=True)

    def _restore_display(self):
        """通知/交互结束后回到当前情绪或待机态。"""
        if self._in_emotion():
            photos = self._photos_moods.get(self._mood) or self._photos_idle
            self.show_frame(photos[0])
        else:
            self._resume_idle()

    def _draw_initial(self):
        if self._photos_idle:
            self.show_frame(self._photos_idle[0])
        else:
            # 无形象文件的兜底：画个粉色圆当占位
            self.canvas.create_oval(
                self.w // 4, self.h // 4, self.w * 3 // 4, self.h * 3 // 4,
                fill="#ffb6c1", outline="#e5a3c0", width=2,
            )
            self.canvas.create_text(
                self.w // 2, self.h // 2, text="猫", fill="#a05070", font=("Microsoft YaHei UI", 16),
            )

    def show_frame(self, photo):
        if photo is None:
            return
        self.photo_refs.append(photo)
        if len(self.photo_refs) > 16:
            self.photo_refs.pop(0)
        if self.canvas_image_id is None:
            self.canvas_image_id = self.canvas.create_image(self.w // 2, self.h // 2, image=photo)
        else:
            self.canvas.itemconfig(self.canvas_image_id, image=photo)

    # ---------- 待机动画 ----------
    def start_idle(self):
        self._idle_running = True
        if len(self._photos_idle) > 1:
            self._anim_loop()
        elif self._photos_idle and self.cfg.get("float", True):
            self._float_loop()
        else:
            self._show_current_photo()

    def _pause_idle(self):
        self._idle_running = False
        if self._anim_job:
            try:
                self.root.after_cancel(self._anim_job)
            except tk.TclError:
                pass
            self._anim_job = None

    def _resume_idle(self):
        self._pause_idle()  # 先停再起，防重复启动多个动画循环
        self.start_idle()

    def _anim_loop(self):
        if self._stop or not self._idle_running:
            return
        photo = self._photos_idle[self.idx % len(self._photos_idle)]
        self.show_frame(photo)
        self.idx += 1
        self._anim_job = self.root.after(300, self._anim_loop)

    def _float_loop(self):
        if self._stop or not self._idle_running:
            return
        photo = self._photos_idle[0]
        self.show_frame(photo)
        y = self.root.winfo_y()
        if self._float_base_y is None:
            self._float_base_y = y
        self._float_t += 0.08
        dy = math.sin(self._float_t) * 6
        # 只在自己没被拖走太多时漂浮，避免和拖动打架
        if abs(y - self._float_base_y) < 80:
            self.root.geometry(f"+{self.root.winfo_x()}+{int(self._float_base_y + dy)}")
        self._anim_job = self.root.after(50, self._float_loop)

    # ---------- M2 行为状态机（空闲踱步 / 入睡 / 唤醒） ----------
    def _touch(self):
        """记录一次活动：重置空闲计时；若在睡则唤醒。"""
        self._last_activity = time.time()
        if self._behavior == "sleep":
            self._wake()

    def _wake(self):
        if self._behavior != "sleep":
            return
        self._behavior = "idle"
        self._hide_zzz()
        if not self._in_emotion():
            self._resume_idle()
        print("[宠物] 醒啦喵…", flush=True)

    def _enter_walk(self):
        if self._docked:
            return
        self._behavior = "walk"
        self._pause_idle()
        self._walk_phase = 0.0
        self._walk_dir = -1                  # 默认先往左，保持默认形象
        self._walk_facing = -self._walk_dir  # 朝左=默认图, 朝右=翻转图
        self._walk_base_y = self.root.winfo_y()  # 蹬步以它为基准，防累积上漂

    def _do_walk(self):
        if self._docked or self._notify_busy or not walk_enabled():  # 踱步被设置关闭则退回 idle
            self._behavior = "idle"
            self._resume_idle()
            return
        wx, wy, ww, wh = work_area()
        x = self.root.winfo_x()
        nx = x + self._walk_dir * WALK_STEP
        if nx < wx or nx + self.w > wx + ww:  # 触边回身：方向反转 + 形象水平翻转
            self._walk_dir *= -1
            self._walk_facing = -self._walk_dir  # 朝左=默认图, 朝右=翻转图
            nx = x + self._walk_dir * WALK_STEP
        # 每"一步"明显起伏（像蹬步），以 _walk_base_y 为基准，不会累积上漂
        self._walk_phase += WALK_RHYTHM
        hop = -int(abs(math.sin(self._walk_phase)) * WALK_HOP)
        y = self._walk_base_y
        frames = self._photos_idle_flip if self._walk_facing == -1 else self._photos_idle
        if frames:
            self.show_frame(frames[0])
        self.root.geometry(f"+{nx}+{y + hop}")

    def _enter_sleep(self):
        if self._docked:
            return
        self._behavior = "sleep"
        self._pause_idle()
        if "sleep" in self._photos_moods:
            self.show_frame(self._photos_moods["sleep"][0])  # 困表情优先
        elif self._photos_idle:
            self.show_frame(self._photos_idle[0])
        self._show_zzz()
        self._sleep_base_y = self.root.winfo_y()  # 慢呼吸基准，防漂
        print("[宠物] 入睡喵…", flush=True)

    def _do_sleep(self):
        # 慢呼吸：比 idle 更慢更小的上下浮动，围绕 _sleep_base_y
        self._sleep_phase += 0.02
        if not self._docked:
            x = self.root.winfo_x()
            dy = int(math.sin(self._sleep_phase) * 2)
            self.root.geometry(f"+{x}+{self._sleep_base_y + dy}")

    def _show_zzz(self):
        if self._zzz_id is None:
            self._zzz_id = self.canvas.create_text(
                8, 8, anchor="nw", text="Zzz…", fill="#7f9fc9",
                font=("Segoe UI", 14, "bold"),
            )

    def _hide_zzz(self):
        if self._zzz_id is not None:
            try:
                self.canvas.delete(self._zzz_id)
            except tk.TclError:
                pass
            self._zzz_id = None

    def _behavior_loop(self):
        if self._stop:
            return
        # 情绪态 / 通知中 / 停靠中：不跑环境行为（保持表情或静置）
        if not self._in_emotion() and not self._notify_busy and not self._docked:
            idle_for = time.time() - self._last_activity
            if self._behavior == "idle":
                if idle_for >= SLEEP_AFTER:
                    self._enter_sleep()
                elif idle_for >= WALK_AFTER and walk_enabled():
                    self._enter_walk()
            elif self._behavior == "walk":
                if idle_for >= SLEEP_AFTER:
                    self._enter_sleep()
                else:
                    self._do_walk()
            elif self._behavior == "sleep":
                self._do_sleep()
        self._behavior_job = self.root.after(33, self._behavior_loop)  # ≈30fps 顺滑

    # ---------- M2 贴边停靠（拖到屏幕边缘收起，鼠标靠近弹出） ----------
    def _dock_cfg(self) -> dict:
        d = self.cfg.get("dock") or {}
        return {
            "enabled": bool(d.get("enabled", True)),
            "margin": int(d.get("margin", 8)),
            "peek": int(d.get("peek", 48)),
        }

    def _detect_dock_side(self) -> str | None:
        if not self._dock_cfg()["enabled"]:
            return None
        sx, sy, sw, sh = screen_area()  # 边判定按整个屏幕（底边=屏幕最底）
        x, y = self.root.winfo_x(), self.root.winfo_y()
        m = self._dock_cfg()["margin"]
        if x <= sx + m:
            return "left"
        if x + self.w >= sx + sw - m:
            return "right"
        if y <= sy + m:
            return "top"
        if y + self.h >= sy + sh - m:
            return "bottom"
        return None

    def _dock(self, side: str):
        self._docked = True
        self._dock_side = side
        self._dock_out = False
        self._dock_target = None
        self._dock_mouse_far = False  # 刚停靠：鼠标还没离开过边缘，不弹出
        self._behavior = "idle"       # 停靠=彻底静置，绝不残留踱步/入睡状态
        self._pause_idle()
        peek = self._dock_cfg()["peek"]
        wx, wy, ww, wh = work_area()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        if side == "left":
            nx, ny = wx - (self.w - peek), y
        elif side == "right":
            nx, ny = wx + ww - peek, y
        elif side == "top":
            nx, ny = x, wy - (self.h - peek)
        else:  # bottom
            nx, ny = x, wy + wh - peek
        # 钳制到工作区范围，防越界
        nx = max(wx - self.w + 1, min(wx + ww - 1, nx))
        ny = max(wy - self.h + 1, min(wy + wh - 1, ny))
        self.root.geometry(f"{self.w}x{self.h}+{nx}+{ny}")
        self._slide_pos = (nx, ny)  # 滑动从已知停靠位置起算，不读 winfo
        print(f"[宠物] 停靠到 {side}", flush=True)

    def _undock(self):
        if not self._docked:
            return
        self._docked = False
        self._dock_side = None
        self._dock_out = False
        self._dock_target = None
        self._slide_pos = None
        if not self._in_emotion():
            self._resume_idle()
        print("[宠物] 解除停靠", flush=True)

    def _dock_hover_check(self):
        """根据鼠标距停靠边的距离更新滑出/收回目标（带迟滞防抖）。"""
        if self._stop:
            return
        # 注意：不挡情绪态——滑出只是窗口位置动画，不打断情绪表情。
        # 若用 _in_emotion() 挡，情绪（如傲娇/生气）会一直卡着，鼠标靠近再也不滑出。
        if self._docked and not self._notify_busy:
            sx, sy, sw, sh = screen_area()   # 鼠标距屏幕边的距离（底边=屏幕最底）
            wx, wy, ww, wh = work_area()     # 停靠/滑出位置仍按工作区（保证可见，不藏进任务栏）
            mx, my = self.root.winfo_pointerx(), self.root.winfo_pointery()
            peek = self._dock_cfg()["peek"]
            side = self._dock_side
            # 鼠标到停靠边的距离 + 滑出/收起位置
            if side == "right":
                d, out_pos, hide_pos = sx + sw - mx, wx + ww - self.w, wx + ww - peek
            elif side == "left":
                d, out_pos, hide_pos = mx - sx, wx, wx - (self.w - peek)
            elif side == "top":
                d, out_pos, hide_pos = my - sy, wy, wy - (self.h - peek)
            else:  # bottom：判定按屏幕最底，位置按工作区（任务栏上方可见）
                d, out_pos, hide_pos = sy + sh - my, wy + wh - self.h, wy + wh - peek
            # 取回手势：鼠标必须先离开过边缘（DOCK_FAR）再靠近才滑出——
            # 否则刚停靠时鼠标通常还停在边缘，一靠近就弹出会像"停靠了还在动"。
            cur = self._dock_out
            if d >= DOCK_FAR:
                self._dock_mouse_far = True   # 已离开 → 之后回来才算主动取回
                want_out = False
            elif d <= DOCK_NEAR:
                want_out = self._dock_mouse_far  # 没离开过就不弹（含刚停靠时）
            else:
                want_out = cur                  # 迟滞死区：保持现状防抖
            self._dock_out = want_out
            # 目标位置：滑出 = 完全显示；否则 = 收起（只露 peek）
            if side in ("right", "left"):
                self._dock_target = (out_pos if want_out else hide_pos, self.root.winfo_y())
            else:
                self._dock_target = (self.root.winfo_x(), out_pos if want_out else hide_pos)
        self._dock_job = self.root.after(150, self._dock_hover_check)

    def _dock_slide_loop(self):
        """停靠态平滑滑动：每 20ms 向 _dock_target 缓动一步（减速滑入/滑出）。

        用内部追踪位置 _slide_pos 推进，不反复读 winfo（读当前可能有 WM 滞后导致抖动）。
        """
        if self._stop:
            return
        if self._docked and self._dock_target is not None:
            if self._slide_pos is None:
                self._slide_pos = (self.root.winfo_x(), self.root.winfo_y())
            px, py = self._slide_pos
            tx, ty = self._dock_target
            dx, dy = tx - px, ty - py
            if abs(dx) < 2 and abs(dy) < 2:
                self.root.geometry(f"{self.w}x{self.h}+{tx}+{ty}")
                self._slide_pos = None
                self._dock_target = None
            else:
                step = max(2, min(10, int((abs(dx) + abs(dy)) * 0.3)))
                nx = px + (1 if dx > 0 else -1) * min(step, abs(dx))
                ny = py + (1 if dy > 0 else -1) * min(step, abs(dy))
                self._slide_pos = (nx, ny)
                self.root.geometry(f"{self.w}x{self.h}+{nx}+{ny}")
        self._dock_slide_job = self.root.after(33, self._dock_slide_loop)  # ≈30fps 顺滑

    # ---------- 交互 ----------
    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="打开猫娘", command=self.open_main)
        self.menu.add_command(label="快搜", command=self.quick_search)
        self.menu.add_command(label="番茄钟", command=self.toggle_pomodoro)
        self.menu.add_command(label="今日提醒", command=self.today_reminders)
        self.menu.add_separator()
        self.menu.add_command(label="喂食", command=self.feed_file)
        self.menu.add_command(label="测试通知", command=self.test_notify)
        self.menu.add_separator()
        self.menu.add_command(label="解除停靠", command=self._undock)
        self.menu.add_separator()
        self.menu.add_command(label="重置大小", command=self.reset_size)
        self.menu.add_command(label="提示：按住 Ctrl 拖动可缩放", state="disabled")
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.quit)

    def _bind_events(self):
        for widget in (self.canvas, self.root):
            widget.bind("<Button-1>", self.on_press)
            widget.bind("<B1-Motion>", self.on_motion)
            widget.bind("<ButtonRelease-1>", self.on_release)
            widget.bind("<Double-Button-1>", self.on_double_click)
            widget.bind("<Button-3>", self.show_menu)
            widget.bind("<Motion>", self.on_hover)

    # ---- 缩放热区 ----
    def _resize_region(self, x: int, y: int):
        """判断 (x, y) 落在哪个缩放热区：角/边 → 区域名；否则 None。停靠态禁用缩放。"""
        if self._docked:
            return None
        left = x < CORNER
        right = x > self.w - CORNER
        top = y < CORNER
        bottom = y > self.h - CORNER
        if left and top:
            return "nw"
        if right and top:
            return "ne"
        if left and bottom:
            return "sw"
        if right and bottom:
            return "se"
        if x < EDGE:
            return "w"
        if x > self.w - EDGE:
            return "e"
        if y < EDGE:
            return "n"
        if y > self.h - EDGE:
            return "s"
        return None

    def on_hover(self, event):
        region = self._resize_region(event.x, event.y)
        self.root.configure(cursor=CURSORS.get(region, "fleur") if region else "fleur")

    def on_press(self, event):
        self._touch()  # 互动即活动（唤醒）
        if self._behavior == "walk":
            self._behavior = "idle"  # 拖动即停踱步，否则每帧被走位覆盖、拖不动
            self._walk_facing = 1    # 拖拽恢复默认朝向
            if self._photos_idle:
                self.show_frame(self._photos_idle[0])
        # 停靠态：不缩放；单击不解除停靠（只有真拖动才取回，防止误点后猫娘"又走起来"）
        if self._docked:
            self.resize_region = None
        elif event.state & 0x0004:
            self.resize_region = "se"
        else:
            self.resize_region = self._resize_region(event.x, event.y)
        self.drag_start = (
            event.x_root, event.y_root,
            self.root.winfo_x(), self.root.winfo_y(),
            self.w, self.h,
        )
        self.dragged = False
        # 长按 1 秒不动 = 蹭蹭反应（拖动/松开都会取消）
        self._longpress_fired = False
        if self._longpress_job:
            try:
                self.root.after_cancel(self._longpress_job)
            except tk.TclError:
                pass
        self._longpress_job = self.root.after(1000, self._on_longpress)
        self._pause_idle()

    def on_motion(self, event):
        if not self.drag_start:
            return
        if self.resize_region:
            self._do_resize(event)
        else:
            sx, sy, wx, wy, *_ = self.drag_start
            dx, dy = event.x_root - sx, event.y_root - sy
            if abs(dx) > 3 or abs(dy) > 3:
                self.dragged = True
                if self._docked:
                    self._undock()  # 停靠态真拖动才解除（取出）
                # 拖动开始：取消长按计时（挪动不算蹭蹭）
                if self._longpress_job:
                    try:
                        self.root.after_cancel(self._longpress_job)
                    except tk.TclError:
                        pass
                    self._longpress_job = None
            self.root.geometry(f"+{wx + dx}+{wy + dy}")

    def _do_resize(self, event):
        """根据拖拽起点与当前指针计算新尺寸/位置，锚定对边不动。"""
        sx, sy, wx, wy, sw, sh = self.drag_start
        dx, dy = event.x_root - sx, event.y_root - sy
        region = self.resize_region
        nw, nh = sw, sh
        if "e" in region:
            nw = sw + dx
        if "w" in region:
            nw = sw - dx
        if "s" in region:
            nh = sh + dy
        if "n" in region:
            nh = sh - dy
        nw = max(MIN_W, min(MAX_W, nw))
        nh = max(MIN_H, min(MAX_H, nh))
        # 位置由钳制后的尺寸反推，保证被锚定的边不移动
        nx = wx + (sw - nw) if "w" in region else wx
        ny = wy + (sh - nh) if "n" in region else wy
        if nw != self.w or nh != self.h:
            self._apply_size(nw, nh)
        self.root.geometry(f"{nw}x{nh}+{nx}+{ny}")
        self.dragged = True  # 缩放也算拖动，避免松开时误触发单击

    def _apply_size(self, w: int, h: int):
        """更新尺寸、画布与图片锚点，让形象重新铺满。"""
        self.w, self.h = w, h
        self.canvas.configure(width=w, height=h)
        self._rebuild_photos()
        self._show_current_photo()
        if self.canvas_image_id is not None:
            self.canvas.coords(self.canvas_image_id, w // 2, h // 2)

    def _save_size(self):
        """把当前尺寸写进记忆文件，作为下次启动的默认大小。"""
        try:
            state_file().write_text(
                json.dumps({"width": self.w, "height": self.h}), encoding="utf-8"
            )
        except Exception:
            pass

    def reset_size(self):
        scale = float(self.cfg.get("scale", 1.0))
        self._apply_size(max(MIN_W, int(self.base_w * scale)), max(MIN_H, int(self.base_h * scale)))
        self._float_base_y = None
        self._save_size()  # 重置的尺寸也作为新的默认
        self.root.geometry(f"{self.w}x{self.h}+{self.root.winfo_x()}+{self.root.winfo_y()}")

    def on_release(self, event):
        was_drag = self.dragged
        self.drag_start = None
        self.resize_region = None
        self.dragged = False
        self._float_base_y = None  # 拖动/缩放后重新锚定漂浮基准
        # 松开即取消长按计时（长按已在 _on_longpress 里处理）
        if self._longpress_job:
            try:
                self.root.after_cancel(self._longpress_job)
            except tk.TclError:
                pass
            self._longpress_job = None
        self._touch()
        if was_drag:
            self._save_size()  # 记住调好的尺寸，下次启动沿用
            side = self._detect_dock_side()  # 拖到边缘 → 贴边停靠
            if side:
                self._dock(side)
                return
        self._resume_idle()
        if self._longpress_fired:
            # 长按触发过（蹭蹭）→ 松手不唤起主窗
            self._longpress_fired = False
            return
        if not was_drag:
            # 单击 → 稍作延迟唤起主窗；若是双击的一部分，会在双击事件里被取消
            if self._single_click_job:
                try:
                    self.root.after_cancel(self._single_click_job)
                except tk.TclError:
                    pass
            self._single_click_job = self.root.after(260, self._fire_single_click)

    def _fire_single_click(self):
        self._single_click_job = None
        self.open_main()  # 单击 → 唤起猫娘主窗

    def on_double_click(self, event):
        """双击：快速问候（取消待触发的单击唤起，防双击时主窗被叫醒两次）。"""
        if self._single_click_job:
            try:
                self.root.after_cancel(self._single_click_job)
            except tk.TclError:
                pass
            self._single_click_job = None
        if self._longpress_job:
            try:
                self.root.after_cancel(self._longpress_job)
            except tk.TclError:
                pass
            self._longpress_job = None
        self._touch()
        self.show_bubble("嘿嘿，主人想本喵啦？（摇摇尾巴）")

    def _on_longpress(self):
        """长按 1 秒不动：蹭蹭反应气泡。"""
        self._longpress_job = None
        self._longpress_fired = True
        self._touch()
        self.show_bubble("（凑过来蹭蹭主人）唔…本喵就蹭一下下喵~")

    def show_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---------- 动作（走 HTTP，避免阻塞 UI） ----------
    def _post(self, url: str, payload: dict | None = None) -> None:
        def do():
            try:
                httpx.post(self.base_url + url, json=payload, timeout=10)
            except Exception:
                pass

        threading.Thread(target=do, daemon=True).start()

    def open_main(self):
        self._touch()
        self._post("/api/pet/show")

    def test_notify(self):
        self._post("/api/pet/notify", {"message": "喵~ 这是测试通知"})

    # ---------- M5 右键菜单功能（快搜 / 番茄钟 / 今日提醒 / 喂食） ----------
    def _ask_text(self, title: str, prompt: str) -> str:
        """弹一个 Tkinter 简易输入框（主线程弹，调用方需在 UI 线程）。"""
        try:
            import tkinter.simpledialog as sd
            val = sd.askstring(title, prompt, parent=self.root)
            return (val or "").strip()
        except Exception:
            return ""

    def quick_search(self):
        """右键「快搜」：输入关键词 → 后端 web_search → 气泡显示前 5 条标题。"""
        q = self._ask_text("快搜", "想搜点啥喵？")
        if not q:
            return
        self._touch()

        def do():
            try:
                r = httpx.get(self.base_url + "/api/search", params={"q": q}, timeout=20)
                data = r.json()
                result = data.get("result") or "没搜到喵"
            except Exception:
                result = "搜索失败喵，等会儿再试试~"
            # 在 UI 线程更新气泡（跨线程碰 Tkinter 会崩）
            self.root.after(0, lambda: self._show_quick_result(result))

        threading.Thread(target=do, daemon=True).start()

    def _show_quick_result(self, text: str):
        # 持久气泡：结果条数多，展示 8 秒再自动收起
        self.show_bubble(text, persist=True, key="quick")
        self.root.after(8000, lambda: self.hide_bubble("quick"))

    def toggle_pomodoro(self):
        """右键「番茄钟」：查询当前状态，未运行则开始，运行中则停止。"""
        self._touch()

        def do():
            try:
                r = httpx.get(self.base_url + "/api/scheduler/state", timeout=10)
                state = r.json()
                running = bool((state.get("pomodoro") or {}).get("running"))
                action = "stop" if running else "start"
                r2 = httpx.post(self.base_url + "/api/scheduler/pomodoro", json={"action": action}, timeout=10)
                st = r2.json()
                if action == "start":
                    mm = (st.get("remaining", 0) or 0) // 60
                    msg = f"🍅 番茄钟开始喵，专注 {mm} 分钟~"
                else:
                    msg = "🍅 番茄钟停啦，休息一下喵~"
            except Exception:
                msg = "番茄钟操作失败喵，等会儿再试试~"
            self.root.after(0, lambda: self.show_bubble(msg))

        threading.Thread(target=do, daemon=True).start()

    def today_reminders(self):
        """右键「今日提醒」：列出后端 scheduler 中已开启的提醒项。"""
        self._touch()

        def do():
            try:
                r = httpx.get(self.base_url + "/api/scheduler/state", timeout=10)
                s = r.json()
                items = []
                if s.get("water"):
                    items.append("喝水提醒（每 45 分钟）")
                if s.get("stretch"):
                    items.append("久坐提醒（每 60 分钟）")
                if s.get("pomodoro_enabled"):
                    items.append("番茄钟")
                if s.get("sleep_nudge"):
                    items.append("深夜劝睡（23:00–06:00）")
                if not items:
                    items.append("一个提醒都没开喵，去设置里开吧~")
                msg = "📋 今日提醒喵：\n" + "\n".join("· " + i for i in items)
            except Exception:
                msg = "获取提醒状态失败喵，等会儿再试试~"
            self.root.after(0, lambda: self.show_bubble(msg, persist=True, key="reminders"))
            self.root.after(8000, lambda: self.hide_bubble("reminders"))

        threading.Thread(target=do, daemon=True).start()

    def feed_file(self):
        """右键「喂食」：拖文件进桌面宠物的兜底方案——原生文件框选文件 →
        上传给猫娘分析。用 tkinter.filedialog（桌宠进程自带，不需 pywebview）。
        """
        self._touch()
        try:
            import tkinter.filedialog as fd
            path = fd.askopenfilename(parent=self.root, title="挑个文件喂给猫娘喵")
        except Exception:
            return
        if not path:
            return

        def do():
            try:
                r = httpx.post(self.base_url + "/api/upload_file", json={"path": path}, timeout=15)
                data = r.json()
                if r.ok:
                    name = data.get("name") or path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                    msg = f"🍖 已把「{name}」丢给猫娘分析喵，去主窗看看结果~"
                else:
                    msg = f"喂食失败喵：{data.get('detail', '')}"
            except Exception:
                msg = "喂食失败喵，文件可能被占用了~"
            self.root.after(0, lambda: self.show_bubble(msg))

        threading.Thread(target=do, daemon=True).start()

    # ---------- 通知 / 事件 ----------
    def on_event(self, event):
        """处理从后端轮询到的事件（dict）；兼容旧 str 通知。

        event = {"type": "notify"|"emotion"|"behavior", ...}
        """
        if isinstance(event, str):  # 旧格式：纯文本通知
            self.on_notification(event)
            return
        etype = event.get("type", "notify")
        try:
            if etype == "emotion":
                mood = normalize_mood(str(event.get("mood") or "happy"))
                if mood == "think" and "think" not in self._photos_moods:
                    # 无思考表情素材 → 回退待机 + 「…」气泡
                    self._set_mood("happy")
                    if not self._notify_busy:
                        self.show_bubble("…")
                else:
                    self._set_mood(mood)
            elif etype == "behavior":
                action = str(event.get("action") or "")
                if action == "sleep":
                    self._cancel_mood_job()  # 入睡时情绪超时一并取消（_mood 归 None）
                    self._mood = None  # 回到环境态，让入睡行为生效（困+Zzz+慢呼吸）
                    self._enter_sleep()
                elif action == "wake":
                    self._touch()  # 唤醒
                # 其余行为指令暂不处理
            elif etype == "bubble":
                # 持久气泡（番茄钟倒计时等）：show 显示/更新，hide 关闭
                key = event.get("key")
                if event.get("action") == "hide":
                    self.hide_bubble(key)
                elif key:
                    self._touch()  # 气泡也算活动（别在计时途中睡着）
                    self.show_bubble(str(event.get("text") or ""), persist=True, key=key)
            else:  # notify
                self.on_notification(str(event.get("message") or "喵~"))
        except Exception as e:
            print(f"[宠物] 处理事件失败：{e}", flush=True)

    def on_notification(self, msg: str):
        if self._notify_busy:
            return
        self._touch()  # 通知也是活动（唤醒）
        self._notify_busy = True
        self._pause_idle()
        try:
            if sound_enabled():
                self._play_chime()
            if self._photos_notify:
                self._play_notify_frames()
            else:
                self._bounce()
            # 提醒气泡：15 秒后自动关，点一下也能立即关——既不因 4 秒太短错过，也不会一直挂着
            self.show_bubble(msg, persist=True, key="reminder", clickable=True,
                             timeout=REMINDER_TIMEOUT)
        finally:
            self._notify_busy = False
            self._restore_display()

    def _play_chime(self):
        """提醒提示音：两段上扬音（比单声 bell 更有存在感），失败回退 bell。"""
        try:
            import winsound
            winsound.Beep(880, 120)                              # A5
            self.root.after(150, lambda: winsound.Beep(1175, 200))  # D6
        except Exception:
            self.root.bell()

    def _play_notify_frames(self):
        for i, photo in enumerate(self._photos_notify):
            self.root.after(i * 120, lambda p=photo: self.show_frame(p))

    def _bounce(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        for i, dy in enumerate([10, -12, 8, -4, 0]):
            self.root.after(i * 70, lambda yy=y + dy: self.root.geometry(f"+{x}+{yy}"))

    def show_bubble(self, text: str, persist: bool = False, key: str | None = None,
                    clickable: bool = False, timeout: int | None = None):
        """显示气泡。persist=True 且带 key 时是「持久气泡」（如番茄钟倒计时）：
        重复同 key 调用只更新文本不新建，直到 hide_bubble(key) 才关闭。
        否则是普通通知气泡，默认 4 秒后自动关闭。
        clickable=True 时气泡可点，点一下立即关闭（提醒气泡点击即消）。
        timeout（毫秒）：到点自动关闭；None 时普通气泡默认 4 秒、持久气泡默认常驻。
        """
        if persist and key and key in self._persist_bubbles:
            self._update_persist_bubble(key, text)
            self._reset_persist_timeout(key, timeout)  # 覆盖更新 → 重新计时
            return
        try:
            if self._bubble:
                self._bubble.destroy()
        except tk.TclError:
            pass
        lines = self._wrap(text, 18)
        line_h = 17
        pad = 10
        w = max((len(l) for l in lines), default=1) * 13 + pad * 2
        h = len(lines) * line_h + pad * 2
        x = self.root.winfo_x() + (self.w - w) // 2
        y = self.root.winfo_y() + int(self.cfg.get("bubble_dy", -60)) - h
        if y < 0:
            y = 0
        bub = tk.Toplevel(self.root)
        bub.overrideredirect(True)
        bub.attributes("-topmost", True)
        bub.attributes("-transparentcolor", KEY)
        bub.configure(bg=KEY)
        bub.geometry(f"{w}x{h}+{x}+{y}")
        c = tk.Canvas(bub, bg=KEY, highlightthickness=0, width=w, height=h)
        c.pack()
        c.create_rectangle(2, 2, w - 2, h - 2, fill="#ffffff", outline="#e5a3c0", width=2)
        tid = c.create_text(
            w // 2, h // 2, text="\n".join(lines), fill="#333333",
            font=("Microsoft YaHei UI", 10), justify="center",
        )
        if clickable:
            # 点气泡一下 → 关掉它（提醒气泡"点一下才关"）
            def _dismiss(_e=None):
                if key and key in self._persist_bubbles:
                    self.hide_bubble(key)
                else:
                    self._close_bubble()
            bub.bind("<Button-1>", _dismiss)
            c.bind("<Button-1>", _dismiss)
        if persist and key:
            self._persist_bubbles[key] = (bub, c, tid, w, h)
            self._reset_persist_timeout(key, timeout)
        else:
            self._bubble = bub
            self._schedule_bubble_timeout(timeout)

    def _schedule_bubble_timeout(self, timeout: int | None):
        """普通气泡：timeout 毫秒后自动关闭（默认 4 秒）。先取消旧定时器防重复。"""
        self._cancel_bubble_timeout()
        ms = int(timeout) if timeout else 4000
        self._bubble_timeout_job = self.root.after(ms, self._close_bubble)

    def _cancel_bubble_timeout(self):
        if self._bubble_timeout_job:
            try:
                self.root.after_cancel(self._bubble_timeout_job)
            except tk.TclError:
                pass
            self._bubble_timeout_job = None

    def _reset_persist_timeout(self, key: str, timeout: int | None):
        """持久气泡：重置自动关闭计时。timeout=None 表示常驻（只有 hide_bubble 能关）。"""
        job = self._persist_timeout_jobs.pop(key, None)
        if job:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        if not timeout:
            return
        self._persist_timeout_jobs[key] = self.root.after(
            int(timeout), lambda: self.hide_bubble(key))

    def _update_persist_bubble(self, key: str, text: str):
        """更新持久气泡的文本与尺寸（同 key 气泡已存在）。"""
        item = self._persist_bubbles.get(key)
        if not item:
            return
        bub, c, tid, _, _ = item
        lines = self._wrap(text, 18)
        line_h = 17
        pad = 10
        w = max((len(l) for l in lines), default=1) * 13 + pad * 2
        h = len(lines) * line_h + pad * 2
        try:
            c.coords(tid, w // 2, h // 2)
            c.itemconfig(tid, text="\n".join(lines))
            c.configure(width=w, height=h)
            x = self.root.winfo_x() + (self.w - w) // 2
            y = self.root.winfo_y() + int(self.cfg.get("bubble_dy", -60)) - h
            if y < 0:
                y = 0
            bub.geometry(f"{w}x{h}+{x}+{y}")
            self._persist_bubbles[key] = (bub, c, tid, w, h)
        except tk.TclError:
            self._persist_bubbles.pop(key, None)

    def hide_bubble(self, key: str):
        """关闭指定 key 的持久气泡。"""
        job = self._persist_timeout_jobs.pop(key, None)
        if job:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        item = self._persist_bubbles.pop(key, None)
        if not item:
            return
        bub, *_ = item
        try:
            bub.destroy()
        except tk.TclError:
            pass

    @staticmethod
    def _wrap(text: str, width: int) -> list[str]:
        lines = []
        for para in text.splitlines() or [""]:
            cur = ""
            for ch in para:
                if len(cur) >= width:
                    lines.append(cur)
                    cur = ""
                cur += ch
            lines.append(cur)
        return [l for l in lines if l] or [""]

    def _close_bubble(self):
        self._cancel_bubble_timeout()
        if self._bubble:
            try:
                self._bubble.destroy()
            except tk.TclError:
                pass
            self._bubble = None

    # ---------- 通知轮询 ----------
    def start_polling(self):
        def loop():
            failures = 0
            while not self._stop:
                try:
                    r = httpx.get(self.base_url + "/api/pet/notifications", timeout=35)
                    data = r.json()
                    t = data.get("type")
                    if t and t != "none":
                        self.q.put(data)  # 整条事件（notify/emotion/behavior）入队
                    failures = 0
                except Exception:
                    # 主程序退出后后端会消失：连续失联就自动关闭，避免成孤儿进程
                    failures += 1
                    if failures >= 8:  # 约 25~30s 连不上后端
                        print("[宠物] 后端失联，宠物自动退出", flush=True)
                        os._exit(0)
                    time.sleep(3)

        threading.Thread(target=loop, daemon=True).start()
        self.root.after(150, self._drain_q)

    def start_watchdog(self):
        """父母进程看门狗：主程序退出 → 宠物 2 秒内跟随退出。

        注意：必须在看门狗线程里直接 os._exit —— 跨线程调 root.after 不可靠，
        sys.exit(0) 也只退出当前线程，主线程还卡在 mainloop 里。
        """
        if self.parent_pid is None:
            return
        print(f"[宠物] 看门狗启动，监听父进程 {self.parent_pid}", flush=True)

        def loop():
            while not self._stop:
                if not pid_alive(self.parent_pid):
                    print("[宠物] 主程序已退出，宠物跟随关闭", flush=True)
                    os._exit(0)
                time.sleep(2)

        threading.Thread(target=loop, daemon=True).start()

    def _drain_q(self):
        if self._stop:
            return
        try:
            while True:
                msg = self.q.get_nowait()
                self.root.after(0, lambda m=msg: self.on_event(m))
        except queue.Empty:
            pass
        self.root.after(150, self._drain_q)

    # ---------- 生命周期 ----------
    def quit(self):
        self._stop = True
        try:
            for job in (self._anim_job, self._behavior_job, self._dock_job, self._dock_slide_job,
                        self._single_click_job, self._longpress_job, self._mood_job):
                if job:
                    self.root.after_cancel(job)
        except tk.TclError:
            pass
        try:
            for job in list(self._persist_timeout_jobs.values()):
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
            self._persist_timeout_jobs.clear()
            for bub, *_ in list(self._persist_bubbles.values()):
                bub.destroy()
            self._persist_bubbles.clear()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        sys.exit(0)


def main():
    ap = argparse.ArgumentParser(description="猫娘桌面宠物")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    ap.add_argument("--parent-pid", type=int, default=None, help="主程序进程号，用于跟随退出")
    # parse_known_args：打包版由主 exe 以 `--pet --base ...` 拉起，忽略 --pet
    args, _ = ap.parse_known_args()
    pet = Pet(args.base, load_config(), parent_pid=args.parent_pid)
    pet.start_polling()
    pet.start_watchdog()
    pet.root.mainloop()


if __name__ == "__main__":
    main()
