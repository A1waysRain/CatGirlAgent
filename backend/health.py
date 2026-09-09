"""轻量内存/健康监控：记「实锤数据」，内存爆了能查到底。

背景（2026-09-01）：用户遇到「内存爆掉崩溃」的情况，已调大系统虚拟内存
（页面文件）预防。这里加两层实锤：
  1. 被动记账：后台 daemon 线程每 HEALTH_INTERVAL 秒把「系统物理内存 + 提交内存
     （commit）+ 猫娘各进程占用」写一行到 %APPDATA%/catgirl/health.log。
     每次 open-append-close（照 actions.log 的做法），进程被 OOM 杀掉也不丢最后几行；
     崩溃后翻文件尾部就能看到内存是怎么一步步涨上去的。
  2. 按需查询：tool_check_system() 调 health.format_summary()，猫娘聊天里
     「看看内存/卡不卡/为什么卡」时实时报数。

设计约束（用户明确担心监控本身会卡）：
  - 不用 subprocess（不 spawn tasklist/powershell——进程创建最贵，内存紧时更致命）；
  - 不轮询 UI 线程、不做「未响应」检测（evaluate_js 心跳有主线程死锁风险）；
  - 全走 ctypes 系统调用：GlobalMemoryStatusEx 一次 + CreateToolhelp32Snapshot
    一次，单次采样毫秒级，60 秒一次，开销可忽略。
"""
import ctypes
import os
import sys
import threading
import time
from pathlib import Path
from ctypes import wintypes

# 采样间隔（秒）。60s 足够看清趋势；改小会写更多行但开销仍然可忽略。
HEALTH_INTERVAL = 60.0
# health.log 行数上限，超出把最早的裁掉，防止无限增长
MAX_LOG_LINES = 2000


# ---------------- Win32 结构 + 原型（64 位必须设 argtypes/restype，防句柄截断） ----------------

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR（64 位必须指针对齐）
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """psapi.h 的 PROCESS_MEMORY_COUNTERS：取进程工作集用。"""
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class PROCESS_MEMORY_COUNTERS_EX(PROCESS_MEMORY_COUNTERS):
    """扩展计数器：PrivateUsage 是进程私有提交，适合观察持续增长。"""
    _fields_ = [("PrivateUsage", ctypes.c_size_t)]


_k = ctypes.windll.kernel32
_k.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
_k.GlobalMemoryStatusEx.restype = wintypes.BOOL
_k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_k.Process32FirstW.restype = wintypes.BOOL
_k.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
_k.Process32NextW.restype = wintypes.BOOL
_k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k.OpenProcess.restype = ctypes.c_void_p
_k.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
_k.K32GetProcessMemoryInfo.restype = wintypes.BOOL
_k.CloseHandle.argtypes = [ctypes.c_void_p]
_k.CloseHandle.restype = wintypes.BOOL

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF  # c_void_p restype 把 -1 包成无符号
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_memory_mb(pid: int) -> tuple[int, int]:
    """返回进程的 (工作集, 私有提交) MB；进程已退出/无权限时均为 0。"""
    h = _k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h or h == INVALID_HANDLE_VALUE:
        return 0, 0
    try:
        pmc = PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if not _k.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return 0, 0
        return _mb(pmc.WorkingSetSize), _mb(pmc.PrivateUsage)
    finally:
        _k.CloseHandle(h)


# ---------------- 采样 ----------------

def _mb(n: int) -> int:
    """字节 → 兆字节（取整，0 以下归 0）。"""
    return int(n // (1024 * 1024))


def sample_memory() -> dict:
    """系统物理内存 + 提交内存当前状态。失败返回空 dict（调用方兜底）。"""
    mse = MEMORYSTATUSEX()
    mse.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not _k.GlobalMemoryStatusEx(ctypes.byref(mse)):
        return {}
    return {
        "load_pct": int(mse.dwMemoryLoad),                       # 物理内存占用百分比
        "mem_total_mb": _mb(mse.ullTotalPhys),
        "mem_avail_mb": _mb(mse.ullAvailPhys),
        "mem_used_mb": _mb(mse.ullTotalPhys - mse.ullAvailPhys),
        # GlobalMemoryStatusEx 的 PageFile 字段实际是系统提交限制/可用提交量，
        # 不是磁盘页面文件的实时写入量，故统一以 commit 命名避免误导。
        "commit_limit_mb": _mb(mse.ullTotalPageFile),
        "commit_avail_mb": _mb(mse.ullAvailPageFile),
        "commit_used_mb": _mb(mse.ullTotalPageFile - mse.ullAvailPageFile),
    }


def _snapshot_procs() -> list[dict]:
    """全进程快照 [{pid, ppid, name}]（工作集单独按 pid 取，快照本身不背内存字段）。失败返回 []。"""
    snap = _k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap is None or snap == 0 or snap == INVALID_HANDLE_VALUE:
        return []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        out = []
        if _k.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                out.append({
                    "pid": int(pe.th32ProcessID),
                    "ppid": int(pe.th32ParentProcessID),
                    "name": pe.szExeFile or "",
                })
                if not _k.Process32NextW(snap, ctypes.byref(pe)):
                    break
        return out
    finally:
        _k.CloseHandle(snap)


def sample_procs(root_pid: int | None = None) -> list[dict]:
    """猫娘进程树占用 [{name, pid, ws_mb, private_mb, depth}]（根进程 + 后代）。

    从 root_pid 出发沿父链递归取后代，保证只统计猫娘自家进程（msedgewebview2.exe
    全系统同名，不能按名收，必须按父子链）；再逐个 OpenProcess 取工作集。
    root_pid 缺省取当前进程。进程树很小（主窗/桌宠/worker/几个 WebView2），开销可忽略。
    """
    root_pid = root_pid or os.getpid()
    procs = _snapshot_procs()
    children: dict[int, list[dict]] = {}
    for p in procs:
        children.setdefault(p["ppid"], []).append(p)

    result = []
    seen: set[int] = set()

    def walk(pid: int, depth: int) -> None:
        if pid in seen:
            return
        seen.add(pid)
        for p in children.get(pid, []):
            ws_mb, private_mb = _process_memory_mb(p["pid"])
            result.append({
                "name": p["name"],
                "pid": p["pid"],
                "ws_mb": ws_mb,
                "private_mb": private_mb,
                "depth": depth,
            })
            walk(p["pid"], depth + 1)

    # 根进程本身（可能不在快照里——被查时正退出之类，忽略即可）
    root_ws_mb, root_private_mb = _process_memory_mb(root_pid)
    result.append({
        "name": os.path.basename(sys.executable or ""),
        "pid": root_pid,
        "ws_mb": root_ws_mb,
        "private_mb": root_private_mb,
        "depth": 0,
    })
    walk(root_pid, 1)
    # 按占用从大到小排，一眼看到谁是大头
    result.sort(key=lambda x: (-x["ws_mb"], x["depth"]))
    return result


# ---------------- 展示/日志 ----------------

def format_summary() -> str:
    """给人（猫娘转述）看的完整摘要：系统内存 + 提交内存 + 猫娘各进程。"""
    m = sample_memory()
    parts = []
    if m:
        parts.append(
            f"系统内存 已用 {m['mem_used_mb']} / {m['mem_total_mb']} MB（{m['load_pct']}%），"
            f"可用 {m['mem_avail_mb']} MB"
        )
        parts.append(
            f"提交内存 已用 {m['commit_used_mb']} / {m['commit_limit_mb']} MB"
            f"（{m['commit_limit_mb'] and m['commit_used_mb'] * 100 // m['commit_limit_mb']}%），"
            f"可用 {m['commit_avail_mb']} MB"
        )
    else:
        parts.append("系统内存读取失败（GlobalMemoryStatusEx 没返回数据）")

    procs = sample_procs()
    if procs:
        lines = [
            f"  - {p['name']}(pid {p['pid']}) 工作集 {p['ws_mb']} MB，私有提交 {p['private_mb']} MB"
            for p in procs[:15]
        ]
        if len(procs) > 15:
            lines.append(f"  - …还有 {len(procs) - 15} 个进程")
        parts.append("猫娘相关进程内存占用：\n" + "\n".join(lines))
    else:
        parts.append("猫娘进程树读取失败（进程快照为空）")
    return "\n".join(parts)


def _compact_procs(procs: list[dict]) -> str:
    """日志用的一行紧凑版：同名进程合并计数，只留大类。"""
    agg: dict[str, dict] = {}
    for p in procs:
        a = agg.setdefault(p["name"], {"count": 0, "ws_mb": 0, "private_mb": 0})
        a["count"] += 1
        a["ws_mb"] += p["ws_mb"]
        a["private_mb"] += p["private_mb"]
    if not agg:
        return "无"
    items = []
    for name, a in sorted(agg.items(), key=lambda x: -x[1]["ws_mb"]):
        tag = f"×{a['count']}" if a["count"] > 1 else ""
        items.append(f"{name}{tag}=工作集{a['ws_mb']}MB/私有提交{a['private_mb']}MB")
    return "+".join(items)


def _log_path() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "health.log"


def make_line() -> str:
    """一行实锤数据（时间戳 + 内存 + 提交内存 + 进程占用）。任何一步失败都不崩。"""
    m = sample_memory()
    mem_txt = "内存读取失败"
    if m:
        mem_txt = (
            f"内存{m['load_pct']}% 已用{m['mem_used_mb']}/{m['mem_total_mb']}MB"
            f" 可用{m['mem_avail_mb']}MB"
            f" | 提交内存{m['commit_used_mb'] * 100 // m['commit_limit_mb'] if m['commit_limit_mb'] else 0}%"
            f" 已用{m['commit_used_mb']}/{m['commit_limit_mb']}MB 可用{m['commit_avail_mb']}MB"
        )
    procs = sample_procs()
    proc_txt = _compact_procs(procs) if procs else "进程快照失败"
    return f"{time.strftime('%Y-%m-%d %H:%M:%S')} {mem_txt} | 进程 {proc_txt}"


_log_lock = threading.Lock()
_log_line_count: int | None = None


def write_line(text: str | None = None) -> None:
    """追加一行到 health.log（open-append-close：崩溃也不丢最后几行），并裁到行数上限。"""
    global _log_line_count
    line = text if text is not None else make_line()
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _log_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if _log_line_count is None:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    _log_line_count = sum(1 for _ in f)
            else:
                _log_line_count += 1
            # 仅在刚越过上限时读+重写，不让稳定运行时每分钟扫描整个日志。
            if _log_line_count > MAX_LOG_LINES:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(lines[-MAX_LOG_LINES:])
                _log_line_count = min(len(lines), MAX_LOG_LINES)
    except Exception:
        pass


def read_recent_log(n: int = 5) -> list[str]:
    """health.log 末尾 n 行（含换行）。文件不存在/读取失败返回 []。"""
    try:
        path = _log_path()
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-n:]
    except Exception:
        return []


# ---------------- 后台看门狗 ----------------

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event = threading.Event()


def start(interval: float = HEALTH_INTERVAL) -> None:
    """启动被动记账线程（幂等：已存活则不再起）。启动即记第一行，之后每 interval 秒记一行。

    两个防重复点：
    1. **`--pet` 桌宠子进程不启动**：打包版桌宠走同一 exe（`--pet` 模式），它会 import
       backend.main → create_app() → 又调一次 start()。主进程的进程树本来就含桌宠，
       桌宠自己再记一笔会让 health.log 每 60 秒双写两行。dev 版桌宠走 pet.py 不经过
       backend.main，不受影响。
    2. **不用 `_thread.is_alive()` 判重**——start() 后线程还没真正跑起来时 is_alive()
       仍是 False，模块级 create_app() 和 run_server 的 create_app() 连续调 start()
       会竞态起两个线程。改用 _stop_event 状态：start 清、stop 设，只有被 stop()
       叫停过才允许重启。
    """
    global _thread
    if "--pet" in getattr(sys, "argv", []):
        return
    with _lock:
        if _thread is not None and not _stop_event.is_set():
            # 已请求过启动且没被 stop() 停止：视为已运行，直接返回
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_loop, kwargs={"interval": interval}, daemon=True)
        _thread.start()


def stop() -> None:
    """停止看门狗（测试/退出用）。"""
    with _lock:
        _stop_event.set()


def _loop(interval: float) -> None:
    write_line()  # 启动先留一条起点
    while not _stop_event.wait(interval):
        write_line()
