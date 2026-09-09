"""MaaFramework 应用内操作 worker —— 独立非冻结 Python 子进程。

设计：B2 进程模型。后端（PyInstaller 冻结进程）**不能** ctypes 加载 MaaFramework
原生库（会踩「内存资源不足」），所以把 MaaFramework 放进这个独立、非冻结的
Python 子进程里跑，通过 JSON-RPC（stdin/stdout，每行一个 JSON）与后端通信。

协议：
    请求  {"id": 1, "cmd": "attach", "params": {...}}
    响应  {"id": 1, "ok": true, "result": {...}}  或  {"id":1, "ok": false, "error": "..."}

命令：
    attach     {window: "记事本"}            —— 找窗口 + Win32Controller 连接 + 置前台
    screencap  {out: "/x/1.png"}            —— 截目标窗口存盘，返回 size
    click      {x, y}                       —— 点击（相对窗口坐标）
    type       {text, enter?}               —— 输入文字，可选回车
    observe    {out: "/x/2.png"}            —— 截图存盘 + 返回（供识别/回报，OCR 见 M3）
    exit                                     —— 响应后 os._exit(0)

关键实现约束（M1 实测）：
- 输入必须 Seize（SendInput）+ 先 SetForegroundWindow 置前台——现代 Win11
  记事本（UWP）PostMessage 输入会被拒。
- 退出必须 os._exit(0)：MaaFramework 原生库在 Python 解释器终结阶段会 segfault
  （显式 del + gc.collect() 不崩，仅进程退出时崩）。跳过终结直接退出。
"""

import ctypes
import ctypes.wintypes
import json
import os
import sys
import threading
import time

from maa.controller import (
    MaaWin32InputMethodEnum,
    MaaWin32ScreencapMethodEnum,
    Win32Controller,
)
from maa.pipeline import JOCR, JRecognitionType
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit
from PIL import Image


# 常用中文应用名 → 窗口标题/类名候选（本机有些应用窗口标题是英文）
_APP_ALIASES = {
    "记事本": ["notepad", "Notepad"],
    "微信": ["wechat", "WeChat", "微信"],
    "文件资源管理器": ["explorer", "资源管理器"],
    "计算器": ["calculator"],
}

# 已知应用 → 期望进程 exe（小写）：attach 时按进程过滤，避免标题含关键字但
# 其实是别的程序的窗口被误命中（如 Typora 打开《微信传文件方案.md》标题含"微信"）。
# 微信 4.x 主窗口 exe=Weixin.exe、class=Qt5...（Qt 应用），进程过滤比 class 更稳。
_APP_EXE = {
    "微信": "weixin.exe",
}


def _pid_of_hwnd(hwnd: int) -> int:
    pid = ctypes.c_uint32()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value


def _exe_of_pid(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.c_uint32(512)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value) if ok else ""
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


class Worker:
    def __init__(self) -> None:
        self.ctrl: Win32Controller | None = None
        self.hwnd: int | None = None
        self.title: str | None = None
        self._ocr_res: Resource | None = None
        self._ocr_tasker: Tasker | None = None

    def _find_hwnd(self, window: str) -> tuple[int, str] | None:
        """窗口标题或类名包含匹配（大小写不敏感），自动扩展中文别名；
        已知应用（如微信）额外按进程 exe 过滤，避免标题含关键字但其实是别的
        程序窗口被误命中（如 Typora 打开《微信传文件方案.md》标题含"微信"）。"""
        candidates = [window, *_APP_ALIASES.get(window.strip(), [])]
        lows = [c.lower() for c in candidates if c]
        exe = _APP_EXE.get(window.strip())
        for w in Toolkit.find_desktop_windows():
            hay = f"{w.window_name} {w.class_name}".lower()
            if not any(low and low in hay for low in lows):
                continue
            if exe and _exe_of_pid(_pid_of_hwnd(w.hwnd)).lower() != exe:
                continue
            return w.hwnd, w.window_name
        return None

    def _list_windows(self, limit: int = 15) -> list[str]:
        return [
            w.window_name
            for w in Toolkit.find_desktop_windows()
            if w.window_name.strip()
        ][:limit]

    def attach(self, window: str) -> dict:
        found = self._find_hwnd(window)
        if not found:
            avail = "；".join(self._list_windows())
            raise RuntimeError(
                f"找不到标题含「{window}」的窗口喵。window 参数要传【应用窗口】名（如 微信/记事本），"
                f"不是联系人/按钮/文件名。当前有标题的窗口：{avail or '（无）'}"
            )
        hwnd, title = found
        ctrl = Win32Controller(
            hWnd=hwnd,
            screencap_method=MaaWin32ScreencapMethodEnum.FramePool,
            mouse_method=MaaWin32InputMethodEnum.Seize,
            keyboard_method=MaaWin32InputMethodEnum.Seize,
        )
        ctrl.post_connection().wait()
        if not ctrl.connected:
            raise RuntimeError("连接目标窗口失败（可能需管理员权限）")
        # Seize/SendInput 需要目标窗口在前台
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        self.ctrl, self.hwnd, self.title = ctrl, hwnd, title
        return {"hwnd": hex(hwnd), "title": title}

    def _require_ctrl(self):
        if self.ctrl is None:
            raise RuntimeError("还没 attach 目标窗口喵")
        return self.ctrl

    def screencap(self, out: str) -> dict:
        ctrl = self._require_ctrl()
        job = ctrl.post_screencap()
        job.wait()
        img = job.get()  # numpy ndarray BGR
        im = Image.fromarray(img[:, :, ::-1])
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        im.save(out)
        return {"path": out, "size": list(im.size)}

    def observe(self, out: str, ocr: bool = False) -> dict:
        """截图存盘；可选 OCR 识别窗口文字，返回 [{text, box, score}, ...]。"""
        base = self.screencap(out)
        texts = self._ocr() if ocr else []
        base["texts"] = texts
        return base

    # ---- OCR ----
    def _model_dir(self) -> str:
        env = os.environ.get("CATGIRL_MAA_MODEL")
        if env:
            return env
        # 默认：项目根/maa_assets/model/ocr（打包版 M4 改从 dist\maa\ 或环境变量取）
        return os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "maa_assets", "model", "ocr")
        )

    def _ocr(self) -> list[dict]:
        ctrl = self._require_ctrl()
        if self._ocr_tasker is None:
            model_dir = self._model_dir()
            if not os.path.isfile(os.path.join(model_dir, "det.onnx")):
                raise RuntimeError(f"OCR 模型缺失：{model_dir}（需 det.onnx/rec.onnx/keys.txt）")
            res = Resource()
            job = res.post_ocr_model(model_dir)
            job.wait()
            if not job.succeeded:
                raise RuntimeError("OCR 模型加载失败")
            tasker = Tasker()
            tasker.bind(res, ctrl)
            self._ocr_res, self._ocr_tasker = res, tasker
        tasker = self._ocr_tasker
        job = ctrl.post_screencap()
        job.wait()
        img = job.get()
        t = tasker.post_recognition(JRecognitionType.OCR, JOCR(expected=[]), img)
        t.wait()
        detail = t.get()
        texts: list[dict] = []
        for n in (detail.nodes if detail else []):
            rec = n.recognition
            if rec and rec.hit:
                for r in rec.all_results:
                    texts.append(
                        {
                            "text": getattr(r, "text", ""),
                            "box": list(r.box) if getattr(r, "box", None) else None,
                            "score": round(float(getattr(r, "score", 0) or 0), 4),
                        }
                    )
        return texts

    def _ensure_foreground(self) -> None:
        """操作前把目标窗口置前台（Seize/SendInput 需要前台，否则点击落错窗口）。"""
        hwnd = self.hwnd
        if hwnd is None:
            return
        u32 = ctypes.windll.user32
        u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u32.IsIconic.argtypes = [ctypes.c_void_p]
        u32.IsIconic.restype = ctypes.c_int
        u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u32.SetActiveWindow.argtypes = [ctypes.c_void_p]
        try:
            # Alt 键技巧解锁 Windows 前台锁（与 app.py 抢前台同款）
            u32.keybd_event(0x12, 0, 0, 0)
            u32.keybd_event(0x12, 0, 2, 0)
            if u32.IsIconic(hwnd):
                u32.ShowWindow(hwnd, 9)  # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
            u32.SetActiveWindow(hwnd)
            time.sleep(0.2)
        except Exception:
            pass

    def click(self, x: int, y: int) -> dict:
        self._ensure_foreground()
        self._require_ctrl().post_click(int(x), int(y)).wait()
        return {"ok": True}

    def _focus_chat_input(self) -> None:
        """点击目标窗口的聊天输入框，确保后续粘贴落在输入框里。

        微信聊天输入框在窗口底部、发送按钮上方。OCR 找最底下的「发送」按钮，
        点它上方（水平取窗口中央）；找不到就点窗口底部中央兜底。
        之前「ui_type 后输入框没内容」=粘贴落到了搜索框/菜单/无焦点处。
        """
        try:
            texts = self._ocr()
            sends = [t for t in texts if "发送" in t["text"] and t.get("box")]
            send = max(sends, key=lambda t: t["box"][1]) if sends else None  # 最靠下=发送按钮
            u32 = ctypes.windll.user32
            u32.GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT)]
            rect = ctypes.wintypes.RECT()
            u32.GetClientRect(ctypes.c_void_p(self.hwnd), ctypes.byref(rect))
            if send:
                cx = rect.right // 2                      # 输入框水平中央
                cy = max(send["box"][1] - 30, 40)          # 发送按钮上方 = 输入文本框
            else:
                cx, cy = rect.right // 2, max(rect.bottom - 80, 40)
            self.click(cx, cy)
            time.sleep(0.3)
        except Exception:
            pass

    def type(self, text: str, click_input: bool = False) -> dict:
        """输入文字到焦点位置（中文/非 ASCII 走剪贴板粘贴，绕开 IME 错漏）。

        click_input=True：先点一下目标窗口的聊天输入框（OCR 找「发送」按钮点它上方），
        确保焦点在输入框再粘贴——微信点开聊天后输入框不一定有焦点，直接粘贴会落空。
        """
        self._ensure_foreground()
        if click_input:
            self._focus_chat_input()
        ctrl = self._require_ctrl()
        if any(ord(ch) > 0x7F for ch in text):
            self._paste(text)
        else:
            ctrl.post_input_text(str(text)).wait()
        return {"ok": True}

    def send(self) -> dict:
        """回车发送（微信等聊天应用回车即发送）。"""
        self._ensure_foreground()
        self._require_ctrl().post_press_key(0x0D).wait()  # VK_RETURN
        return {"ok": True}

    def _paste(self, text: str) -> None:
        """把文本放进剪贴板并 Ctrl+V 粘贴（中文输入的可靠方案）。"""
        import ctypes as _ct

        user32, k32 = _ct.windll.user32, _ct.windll.kernel32
        # 64 位下必须显式声明指针/句柄类型，否则句柄被截断成 32 位导致崩溃
        k32.GlobalAlloc.argtypes = [_ct.c_uint, _ct.c_size_t]
        k32.GlobalAlloc.restype = _ct.c_void_p
        k32.GlobalLock.argtypes = [_ct.c_void_p]
        k32.GlobalLock.restype = _ct.c_void_p
        k32.GlobalUnlock.argtypes = [_ct.c_void_p]
        user32.OpenClipboard.argtypes = [_ct.c_void_p]
        user32.OpenClipboard.restype = _ct.c_int
        user32.SetClipboardData.argtypes = [_ct.c_uint, _ct.c_void_p]
        user32.SetClipboardData.restype = _ct.c_void_p
        CF_UNICODETEXT, GHND = 13, 0x0042  # CF_UNICODETEXT / GMEM_MOVEABLE
        data = (text + "\0").encode("utf-16-le")
        hmem = k32.GlobalAlloc(GHND, len(data))
        if not hmem:
            raise RuntimeError("剪贴板内存分配失败")
        ptr = k32.GlobalLock(hmem)
        if not ptr:
            raise RuntimeError("剪贴板内存锁定失败")
        _ct.memmove(ptr, data, len(data))
        k32.GlobalUnlock(hmem)
        try:
            for _ in range(5):  # 剪贴板被占用时重试
                if user32.OpenClipboard(None):
                    try:
                        user32.EmptyClipboard()
                        if user32.SetClipboardData(CF_UNICODETEXT, hmem):
                            break
                    finally:
                        user32.CloseClipboard()
                else:
                    time.sleep(0.1)
        except Exception:
            pass
        # Ctrl+V 粘贴
        self._send_ctrl_v()

    def _send_ctrl_v(self) -> None:
        ctrl = self._require_ctrl()
        ctrl.post_key_down(0x11).wait()  # VK_CONTROL
        ctrl.post_key_down(0x56).wait()  # VK_V
        ctrl.post_key_up(0x56).wait()
        ctrl.post_key_up(0x11).wait()
        time.sleep(0.2)

    def file_paste(self, path: str) -> dict:
        """把文件做成 CF_HDROP 塞剪贴板，然后 Ctrl+V 粘贴到当前输入框。

        调用方需先 attach 微信；粘贴前先点一下输入框（与 type(click_input) 同款，
        防止焦点没在输入框导致粘贴落空）。只粘贴附件，不会发送。
        """
        _clipboard_set_hdrop(path)
        self._ensure_foreground()
        self._focus_chat_input()
        self._send_ctrl_v()
        return {"path": os.path.abspath(path)}


def _clipboard_set_hdrop(path: str) -> None:
    """把文件路径做成 CF_HDROP 塞进剪贴板（与资源管理器复制文件同款格式）。

    CF_HDROP = 15；数据 = DROPFILES 结构（20 字节）+ UTF-16LE 空结尾路径串，
    末尾再补一个空（双空结尾）。微信等支持"粘贴文件"的应用认这个格式。
    """
    import ctypes as _ct

    user32, k32 = _ct.windll.user32, _ct.windll.kernel32
    # 64 位下必须显式声明指针/句柄类型，否则句柄被截断成 32 位导致崩溃（_paste 同款教训）
    k32.GlobalAlloc.argtypes = [_ct.c_uint, _ct.c_size_t]
    k32.GlobalAlloc.restype = _ct.c_void_p
    k32.GlobalLock.argtypes = [_ct.c_void_p]
    k32.GlobalLock.restype = _ct.c_void_p
    k32.GlobalUnlock.argtypes = [_ct.c_void_p]
    k32.GlobalFree.argtypes = [_ct.c_void_p]
    k32.GlobalFree.restype = _ct.c_void_p
    user32.OpenClipboard.argtypes = [_ct.c_void_p]
    user32.OpenClipboard.restype = _ct.c_int
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = _ct.c_int
    user32.SetClipboardData.argtypes = [_ct.c_uint, _ct.c_void_p]
    user32.SetClipboardData.restype = _ct.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = _ct.c_int

    class DROPFILES(_ct.Structure):
        _fields_ = [
            ("pFiles", _ct.c_uint32),
            ("pt_x", _ct.c_int32),
            ("pt_y", _ct.c_int32),
            ("fNC", _ct.c_int32),
            ("fWide", _ct.c_int32),
        ]

    CF_HDROP, GHND = 15, 0x0042  # CF_HDROP / GMEM_MOVEABLE
    wpath = (os.path.abspath(path) + "\0").encode("utf-16-le")
    total = _ct.sizeof(DROPFILES) + len(wpath) + 2  # 末尾双空结尾（GMEM 零初始化）
    hmem = k32.GlobalAlloc(GHND, total)
    if not hmem:
        raise RuntimeError("剪贴板内存分配失败")
    ptr = k32.GlobalLock(hmem)
    if not ptr:
        raise RuntimeError("剪贴板内存锁定失败")
    df = DROPFILES()
    df.pFiles = _ct.sizeof(DROPFILES)  # 文件列表在结构之后的偏移
    df.fWide = 1
    _ct.memmove(ptr, _ct.byref(df), _ct.sizeof(df))
    _ct.memmove(ptr + _ct.sizeof(DROPFILES), wpath, len(wpath))
    k32.GlobalUnlock(hmem)
    for _ in range(5):  # 剪贴板被占用时重试
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                if user32.SetClipboardData(CF_HDROP, hmem):
                    return  # 成功：系统接管 hmem，不要释放
            finally:
                user32.CloseClipboard()
        else:
            time.sleep(0.1)
    k32.GlobalFree(hmem)  # 没设成功才释放（设成功系统接管）
    raise RuntimeError("剪贴板写入失败")


def _pid_alive(pid: int) -> bool:
    """判断进程是否存活（Windows；必须用 WaitForSingleObject 看信号态，
    不能用 OpenProcess 成败——进程被杀后对象短暂残留会误判存活）。"""
    if pid is None:
        return True
    try:
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            # 0x102(258)=WAIT_TIMEOUT=还活着；0=WAIT_OBJECT_0=已终止
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x102
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return True


def _start_parent_watchdog() -> None:
    """父进程（猫娘后端）死亡时跟随退出，防止 worker 变成孤儿进程。

    同桌宠 pet.py 手法：直接 os._exit(0) 跳过解释器终结（规避 MaaFw segfault）。
    """
    pid_s = os.environ.get("CATGIRL_PARENT_PID")
    if not pid_s:
        return
    try:
        parent = int(pid_s)
    except ValueError:
        return

    def _watch() -> None:
        while True:
            time.sleep(2)
            if not _pid_alive(parent):
                os._exit(0)  # 父进程没了，直接跟随退出

    threading.Thread(target=_watch, daemon=True).start()


def _dispatch(w: Worker, cmd: str, params: dict):
    if cmd == "attach":
        return w.attach(params.get("window", ""))
    if cmd == "screencap":
        return w.screencap(params.get("out", ""))
    if cmd == "click":
        return w.click(params.get("x", 0), params.get("y", 0))
    if cmd == "type":
        return w.type(params.get("text", ""), bool(params.get("click_input", False)))
    if cmd == "send":
        return w.send()
    if cmd == "file_paste":
        return w.file_paste(params.get("path", ""))
    if cmd == "observe":
        return w.observe(params.get("out", ""), bool(params.get("ocr", False)))
    raise RuntimeError(f"未知命令：{cmd}")


def main() -> None:
    # stdin/stdout 强制 UTF-8（GBK 终端下 JSON 中文会乱码/解码失败）
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # MaaFramework 原生日志默认写 stdout，会污染 JSON-RPC 流 → 导向文件
    try:
        log_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "catgirl", "logs", "maa")
        os.makedirs(log_dir, exist_ok=True)
        Tasker.set_log_dir(log_dir)
    except Exception:
        pass
    _start_parent_watchdog()  # 主程序死亡 → 跟随退出，防孤儿进程
    w = Worker()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, cmd, params = req.get("id"), req.get("cmd"), req.get("params", {})
        try:
            result = _dispatch(w, cmd, params)
            resp = {"id": rid, "ok": True, "result": result}
        except Exception as e:  # noqa: BLE001 —— worker 边界：任何异常都回传给后端
            resp = {"id": rid, "ok": False, "error": str(e)}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        if cmd == "exit":
            os._exit(0)  # 跳过解释器终结，规避 MaaFw 退出 segfault


if __name__ == "__main__":
    main()
