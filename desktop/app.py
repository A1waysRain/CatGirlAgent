"""猫娘桌面壳：同进程启动 FastAPI 后端 + 内嵌 WebView2 窗口。

用法:
    python desktop/app.py            # 开发运行
    python desktop/app.py --debug    # 打开 WebView2 开发者工具

设计:
    - 后端只监听 127.0.0.1 的随机空闲端口，不对外网开放；
    - uvicorn 在后台线程运行，与桌面壳同进程，无进程间通信；
    - 关闭窗口时优雅停服，进程随之退出。
    - 通过 js_api 向前端暴露原生能力（文件选择框等），供设置界面换头像。
"""
import ctypes
import ipaddress
import os
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import httpx
import pystray
import webview
from PIL import Image

# 保证 backend 包可导入（开发时从项目根运行；打包后用 PyInstaller 处理）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import lan  # noqa: E402
from backend.lan_control import set_lan_control  # noqa: E402
from backend.main import run_lan_server, run_server, set_pet_control, _avatar_dir  # noqa: E402
from backend.notifier import notifier  # noqa: E402
from backend.settings import load_settings  # noqa: E402

# ---- WebView2 后台卡死防护 ----
# 现象：主聊天窗挂后台长时间不用后永久「未响应」，只能结束进程。
# 根因是 WebView2 浏览器进程自身卡死——窗口隐藏/被遮挡久了，Chromium 会把渲染
# 进程挂起/节流，恢复时偶发无法唤醒；GPU 进程崩溃也会让整窗冻结。
# 修法：在创建窗口前通过 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS 注入参数，
# 让 WebView2 浏览器进程不节流后台渲染、并禁用 GPU 加速（规避 GPU 进程崩溃）。
# 这些参数只作用于本进程创建的 WebView2 环境，不影响系统其他软件。
_WEBVIEW2_FLAGS = (
    "--disable-backgrounding-occluded-windows "  # 隐藏/被遮挡时不挂起渲染进程
    "--disable-renderer-backgrounding "           # 渲染进程不做后台节流
    "--disable-background-timer-throttling "      # 后台不节流页面定时器
    # "--disable-gpu"                               # 暂注释测试：疑与新版 WebView2 runtime 初始化冲突（2026-08-20）
)
_existing_args = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").strip()
# 临时禁用 WebView2 flags 测试（2026-08-20 疑与新版 runtime 初始化冲突）
# os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
#     _existing_args + " " + _WEBVIEW2_FLAGS
# ).strip()


def _window_hwnd(window):
    """取 pywebview 窗口的 Win32 句柄（窗口未创建/拿不到返回 None）。"""
    try:
        native = getattr(window, "native", None)
        if native is None:
            return None
        return getattr(native, "Handle", None) or None
    except Exception:
        return None


def _flash_taskbar(hwnd) -> None:
    """任务栏图标+标题栏闪烁，直到窗口回到前台（Windows 原生 FlashWindowEx）。

    提醒到点时主窗不一定抢得到焦点（Windows 前台锁），任务栏闪黄才能真"叮"到主人；
    主人点窗口后自动停止闪烁。
    """
    try:
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]
        FLASHW_ALL = 0x03    # 图标 + 标题栏都闪
        FLASHW_TIMER = 0x04  # 持续闪直到窗口到前台
        info = FLASHWINFO()
        info.cbSize = ctypes.sizeof(FLASHWINFO)
        info.hwnd = int(hwnd)
        info.dwFlags = FLASHW_ALL | FLASHW_TIMER
        info.uCount = 0
        info.dwTimeout = 0
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


class Api:
    """暴露给前端 JS 的原生能力（window.pywebview.api.*）。"""

    def pick_image(self):
        """打开原生文件选择框，返回图片绝对路径；取消返回 None。"""
        try:
            win = webview.windows[0]
            result = win.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Image files (*.png;*.jpg;*.jpeg;*.gif;*.webp;*.bmp)",),
            )
            if result:
                return result[0]
        except Exception:
            pass
        return None

    def pick_file(self):
        """打开原生文件选择框（任意常见文件），返回绝对路径；取消返回 None。"""
        try:
            win = webview.windows[0]
            result = win.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=(
                    "Common files (*.txt;*.md;*.doc;*.docx;*.xls;*.xlsx;*.ppt;*.pptx;*.pdf;*.zip;*.rar;*.7z;*.csv;*.json;*.py;*.js;*.html)",
                    "All files (*.*)",
                ),
            )
            if result:
                return result[0]
        except Exception:
            pass
        return None

    def pick_rag_file(self):
        """选择可导入知识库的文本、Word 或 Excel 文件。"""
        try:
            win = webview.windows[0]
            result = win.create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Knowledge files (*.md;*.txt;*.docx;*.xlsx)",),
            )
            if result:
                return result[0]
        except Exception:
            pass
        return None

    def pick_folder(self):
        """打开原生文件夹选择框，返回文件夹绝对路径；取消返回 None。"""
        try:
            win = webview.windows[0]
            result = win.create_file_dialog(webview.FOLDER_DIALOG)
            if result:
                return result[0]
        except Exception:
            pass
        return None

    def take_screenshot(self):
        """打开全屏区域截图选择器，返回截图 PNG 绝对路径；取消返回 None。"""
        try:
            base = Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "media"
            base.mkdir(parents=True, exist_ok=True)
            out = base / f"shot_{int(time.time() * 1000)}.png"
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--screenshot", "--out", str(out)]
            else:
                script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshot.py")
                cmd = [sys.executable, script, "--out", str(out)]
            r = subprocess.run(cmd, creationflags=flags, timeout=120)
            if r.returncode == 0 and out.exists():
                return str(out)
        except Exception:
            pass
        return None

    def list_lan_ips(self):
        """返回桌面当前可用于手机接入的私网 IPv4。"""
        candidates = set()
        try:
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = item[4][0]
                if _valid_lan_ip(address):
                    candidates.add(address)
        except OSError:
            pass
        return sorted(candidates, key=lambda address: tuple(map(int, address.split("."))))


def pick_port() -> int:
    """向系统申请一个仅绑定本机的空闲端口（bind 0 由 OS 分配）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _valid_lan_ip(value: str) -> bool:
    """仅允许绑定 RFC1918 私网 IPv4，禁止误开到公网或 0.0.0.0。"""
    try:
        ip = ipaddress.ip_address(value)
        return ip.version == 4 and any(ip in network for network in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        ))
    except ValueError:
        return False


def wait_ready(base_url: str, timeout: float = 15.0) -> bool:
    """轮询 /api/chat 直到后端就绪，防止窗口先于服务打开。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(base_url + "/api/chat", timeout=1.5).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def start_pet(base_url: str):
    """独立进程启动桌面宠物。

    开发时：venv 的 python + desktop/pet.py；
    打包后：同一 exe 以 ``--pet`` 模式运行（pet 作为独立进程拉起）。
    """
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--pet", "--base", base_url, "--parent-pid", str(os.getpid())]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        pet_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pet.py")
        cmd = [sys.executable, pet_script, "--base", base_url, "--parent-pid", str(os.getpid())]
        flags = 0
    try:
        return subprocess.Popen(cmd, creationflags=flags)
    except Exception as e:
        print("[猫娘] 桌面宠物启动失败：", e)
        return None


def create_tray(window, on_show, on_quit) -> pystray.Icon | None:
    """创建系统托盘图标：右键菜单「唤出聊天窗口 / 退出」。失败返回 None。

    图标用聊天头像 img/cat.png（dev 与打包版都能定位）；run_detached 在后台线程跑，
    与 webview 主循环共存。注意：该线程非 daemon，退出前必须调用 stop()。
    """
    try:
        icon_path = os.path.join(str(_avatar_dir()), "cat.png")
        if not os.path.isfile(icon_path):
            print("[猫娘] 托盘图标缺失，跳过：", icon_path)
            return None
        image = Image.open(icon_path)
        image.thumbnail((32, 32), Image.LANCZOS)
        menu = pystray.Menu(
            pystray.MenuItem("唤出聊天窗口", lambda icon, item: on_show(), default=True),
            pystray.MenuItem("退出", lambda icon, item: on_quit()),
        )
        tray = pystray.Icon("catgirl_tray", image, "猫娘来咯", menu=menu)
        tray.run_detached()
        return tray
    except Exception as e:
        print("[猫娘] 托盘图标启动失败：", e)
        return None


def main() -> None:
    debug = "--debug" in sys.argv

    # 打包版：主 exe 以宠物模式运行（作为独立进程被 app 模式拉起）
    if "--pet" in sys.argv:
        from desktop.pet import main as pet_main

        pet_main()
        return

    # 打包版：主 exe 以截图选择器模式运行（被 app 模式拉起）
    if "--screenshot" in sys.argv:
        from desktop.screenshot import main as screenshot_main

        screenshot_main()
        return

    port = pick_port()
    base_url = f"http://127.0.0.1:{port}"
    startup_settings = load_settings()
    lan_ip = str(startup_settings.get("lan_ip") or "").strip()
    try:
        lan_port = int(startup_settings.get("lan_port") or 8800)
    except (TypeError, ValueError):
        lan_port = 8800
    if not 1 <= lan_port <= 65535:
        lan_port = 8800
    lan_enabled = bool(startup_settings.get("lan_enabled"))
    lan_token = str(startup_settings.get("lan_token") or "")
    # 手机接入必须由本次桌面运行手动打开；重启不能沿用旧监听。
    if lan_enabled:
        from backend.settings import save_settings
        save_settings({"lan_enabled": False})
    lan_enabled = False
    # LAN server 由 apply_lan 在下方启动；此处保持桌面壳为纯回环模式。
    lan.configure(False)

    server = run_server(
        port=port, log_level="debug" if debug else "warning",
        lan_ip=lan_ip if lan_enabled else "", lan_port=lan_port if lan_enabled else 0,
    )
    lan_holder = {"server": None, "ip": "", "port": 0}
    lan_lock = threading.Lock()
    lan_idle_stop = threading.Event()

    def stop_lan_server() -> None:
        old = lan_holder["server"]
        if old is not None:
            old.should_exit = True
            thread = getattr(old, "catgirl_thread", None)
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    raise ValueError("旧的手机接入服务未能停止，请稍后重试")
        lan_holder.update(server=None, ip="", port=0)
        lan.configure(False)

    def apply_lan(settings: dict) -> None:
        """设置页变更手机接入时停旧监听、清会话、再启动新监听。"""
        enabled = bool(settings.get("lan_enabled"))
        ip = str(settings.get("lan_ip") or "").strip()
        try:
            requested_port = int(settings.get("lan_port") or 8800)
        except (TypeError, ValueError):
            raise ValueError("手机接入端口必须是 1 到 65535 的整数")
        if not 1 <= requested_port <= 65535:
            raise ValueError("手机接入端口必须是 1 到 65535 的整数")
        with lan_lock:
            if not enabled:
                stop_lan_server()
                return
            if not _valid_lan_ip(ip):
                raise ValueError("请选择当前手机热点对应的私网 IPv4 地址")
            token = str(settings.get("lan_token") or "") or lan.new_token()
            if not settings.get("lan_token"):
                from backend.settings import save_settings
                save_settings({"lan_token": token})
            same = lan_holder["server"] and lan_holder["ip"] == ip and lan_holder["port"] == requested_port
            if same:
                lan.configure(True, ip, requested_port, token)
                return
            stop_lan_server()
            lan.configure(True, ip, requested_port, token)
            lan_holder["server"] = run_lan_server(server.catgirl_app, ip, requested_port,
                                                    log_level="debug" if debug else "warning")
            lan_holder.update(ip=ip, port=requested_port)

    set_lan_control(apply_lan)

    def stop_idle_lan() -> None:
        """连续五分钟没有手机侧请求时，只关闭 LAN 监听。"""
        while not lan_idle_stop.wait(15):
            if not lan.is_idle():
                continue
            with lan_lock:
                if not lan.is_idle():
                    continue
                try:
                    stop_lan_server()
                except ValueError as error:
                    print("[猫娘] 手机接入空闲关闭失败：", error)
                else:
                    from backend.settings import save_settings
                    save_settings({"lan_enabled": False})
                    print("[猫娘] 手机接入已空闲 5 分钟，自动关闭")

    threading.Thread(target=stop_idle_lan, daemon=True, name="catgirl-lan-idle").start()

    if not wait_ready(base_url):
        print("[猫娘] 后端启动失败：请检查 .env 中的 DEEPSEEK_API_KEY 等配置", file=sys.stderr)
        server.should_exit = True
        sys.exit(1)

    window = webview.create_window(
        "猫娘来咯",
        base_url,
        width=920,
        height=720,
        min_size=(640, 560),
        background_color="#fdf2f8",
        text_select=True,
        js_api=Api(),
    )

    def raise_main_window() -> None:
        """宠物单击 / 到点提醒 → 把主窗唤到前台；不在前台时任务栏闪烁。"""
        try:
            if window.minimized:
                window.restore()
            window.show()
            hwnd = _window_hwnd(window)
            if hwnd and ctypes.windll.user32.GetForegroundWindow() != hwnd:
                # 主窗不在前台：置顶唤起 + 任务栏闪烁（提醒才真正"叮"到主人）
                window.on_top = True
                window.on_top = False
                _flash_taskbar(hwnd)
            # 已在最前：不折腾置顶，免得打扰正在聊天的你
        except Exception:
            pass

    notifier.set_show_window(raise_main_window)

    # ---- 桌宠启停（设置界面里开关桌宠时由后端回调） ----
    pet_holder = {"proc": None}

    def stop_pet() -> None:
        proc = pet_holder["proc"]
        pet_holder["proc"] = None
        if proc is not None and proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    capture_output=True,
                )
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

    def start_pet_when_needed() -> None:
        if pet_holder["proc"] is not None and pet_holder["proc"].poll() is None:
            return  # 已经在跑
        pet_holder["proc"] = start_pet(base_url)

    set_pet_control(start_pet_when_needed, stop_pet)

    # 按设置决定是否显示桌宠
    if load_settings().get("show_pet", True):
        pet_holder["proc"] = start_pet(base_url)

    # ---- 关闭行为 + 系统托盘 ----
    shutdown_state = {"done": False}

    def real_shutdown() -> None:
        """真正退出：停托盘、停桌宠、停后端（随后由调用方结束进程）。"""
        if shutdown_state["done"]:
            return
        shutdown_state["done"] = True
        lan_idle_stop.set()
        try:
            tray.stop()
        except Exception:
            pass
        print("[猫娘] 正在退出…")
        with lan_lock:
            stop_lan_server()
        stop_pet()
        server.should_exit = True

    def on_tray_quit() -> None:
        """托盘「退出」：托盘线程里强制结束进程，保证不留孤儿。"""
        real_shutdown()
        os._exit(0)

    def on_closing() -> object:
        """窗口关闭：按设置决定隐藏到托盘（后台存活）还是退出。

        隐藏模式返回 False 取消关闭（pywebview 的 closing 事件支持）。
        """
        if shutdown_state["done"]:
            return None
        if load_settings().get("close_behavior", "tray") == "tray":
            try:
                window.hide()
            except Exception:
                pass
            return False  # 取消关闭，进程后台存活
        real_shutdown()
        return None

    tray = create_tray(window, raise_main_window, on_tray_quit)

    window.events.closing += on_closing

    # ---- 启动后确保主窗可见（防御性修复） ----
    # 现象：偶尔启动时主窗处于隐藏态（WebView2 冷启动竞态，排查排除了 on_closing/
    # 托盘，表现为「程序在跑但没界面」）。这里起一个 daemon 线程在前 16 秒内轮询
    # 窗口可见性，若被藏掉就补一次 show()。窗口本来就可见时纯 no-op，不抢焦点。
    def _ensure_window_shown() -> None:
        for _ in range(8):
            time.sleep(2)
            try:
                hwnd = _window_hwnd(window)
                if not hwnd:
                    continue
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    window.show()
                    return
                # 可见但被扔到屏幕外（多显示器拔出 / WebView2 冷启动坐标错乱）——
                # 表现为"程序在跑、后端正常、但看不到窗口"。检测到就挪回工作区。
                r = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
                sw = ctypes.windll.user32.GetSystemMetrics(0)   # SM_CXSCREEN
                sh = ctypes.windll.user32.GetSystemMetrics(1)   # SM_CYSCREEN
                if r.right < 0 or r.bottom < 0 or r.left >= sw or r.top >= sh:
                    window.move(80, 80)
                    return
            except Exception:
                pass

    threading.Thread(target=_ensure_window_shown, daemon=True).start()

    webview.start(debug=debug)


if __name__ == "__main__":
    main()
