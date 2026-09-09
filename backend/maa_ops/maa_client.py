"""MaaClient —— 后端与 MaaFramework worker 子进程的 JSON-RPC 通信层。

后端（可能被打包成冻结 exe）无法直接 ctypes 加载 MaaFramework 原生库，
所以通过独立非冻结 worker 子进程跑 MaaFramework（见 worker.py）。
本模块负责：spawn worker → 发送请求 → 读响应 → 高层 API。

开发模式 worker 用当前解释器（venv python，非冻结）；打包版（M4）改用
exe 旁的外置嵌入版 Python（dist/python/），见 build 时决策。
"""

import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CALL_TIMEOUT = 60.0  # 秒；单条 JSON-RPC 响应超时（首次 OCR 含模型初始化）
_STDERR_MAX = 200  # 保留最近 N 条 worker stderr 供诊断


class MaaClient:
    def __init__(self, worker_script: str | None = None) -> None:
        self._proc: subprocess.Popen | None = None
        self._next_id = 1
        self._worker_script = worker_script or _default_worker_script()

    # ---- 生命周期 ----
    def start(self) -> None:
        """惰性启动：首次调用命令时自动拉起。"""
        self._ensure()

    def stop(self) -> None:
        """通知 worker 退出并回收（worker 内部 os._exit(0)，干净无 segfault）。"""
        if self._proc is None:
            return
        try:
            self._call("exit", timeout=5)  # 短超时，避免卡住关闭流程
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    # ---- 高层命令 ----
    def attach(self, window: str) -> dict:
        return self._call("attach", {"window": window})

    def screencap(self, out: str) -> dict:
        return self._call("screencap", {"out": out})

    def click(self, x: int, y: int) -> dict:
        return self._call("click", {"x": x, "y": y})

    def type(self, text: str, click_input: bool = False) -> dict:
        return self._call("type", {"text": text, "click_input": click_input})

    def send(self) -> dict:
        """回车发送。"""
        return self._call("send")

    def file_paste(self, path: str) -> dict:
        """把文件做成 CF_HDROP 塞剪贴板并 Ctrl+V 粘贴到当前输入框（不发送）。"""
        return self._call("file_paste", {"path": path})

    def observe(self, out: str, ocr: bool = False) -> dict:
        """截图存盘；ocr=True 时额外返回窗口文字 [{text, box, score}, ...]。"""
        return self._call("observe", {"out": out, "ocr": ocr})

    # ---- 内部 ----
    @staticmethod
    def _worker_python() -> str:
        """worker 解释器：冻结版用 exe 旁的嵌入版 Python，否则用当前解释器。"""
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            embed = os.path.join(exe_dir, "python", "python.exe")
            if os.path.isfile(embed):
                return embed
        return sys.executable

    @staticmethod
    def _worker_env() -> dict:
        """worker 环境：冻结版把 OCR 模型路径指到 exe 旁外置目录；
        传父进程 PID 给 worker 看门狗（父死 worker 跟随退出）。"""
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        env["CATGIRL_PARENT_PID"] = str(os.getpid())
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            model_dir = os.path.join(exe_dir, "maa_assets", "model", "ocr")
            if os.path.isdir(model_dir):
                env["CATGIRL_MAA_MODEL"] = model_dir
        return env

    def _ensure(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            [self._worker_python(), self._worker_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # 独立收集 worker 错误，不混进 JSON-RPC 流
            text=True,
            encoding="utf-8",
            creationflags=_NO_WINDOW,
            env=self._worker_env(),
        )
        self._start_reader()

    def _start_reader(self) -> None:
        """后台线程读 stdout（响应）与 stderr（错误日志），响应进队列供 _call 按 id 取。"""
        self._resp_queue: "queue.Queue[dict]" = queue.Queue()
        self._stderr_buf: collections.deque = collections.deque(maxlen=_STDERR_MAX)
        assert self._proc is not None
        threading.Thread(target=self._reader_loop, daemon=True).start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

    def _reader_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue  # 跳过 Maa 原生日志等非 JSON 行
            self._resp_queue.put(resp)

    def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            line = line.rstrip("\r\n")
            if line:
                self._stderr_buf.append(line)

    def _respawn_on_timeout(self, cmd: str) -> None:
        """worker 超时/无响应：杀掉进程树并清引用，下次调用自动拉起新 worker。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                creationflags=_NO_WINDOW,
                capture_output=True,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _call(self, cmd: str, params: dict | None = None, timeout: float = _CALL_TIMEOUT) -> dict:
        """发一条 JSON-RPC 请求并等匹配 id 的响应；超时/无响应则强制重启 worker。

        响应由后台 reader 线程进队列，这里按 id 取（Maa 原生日志等非 JSON 行已被 reader 跳过）。
        """
        self._ensure()
        rid = self._next_id
        self._next_id += 1
        req = {"id": rid, "cmd": cmd, "params": params or {}}
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self._respawn_on_timeout(cmd)
                raise RuntimeError(
                    f"worker 响应超时（{cmd}，{int(timeout)}s）——已强制重启 worker"
                )
            try:
                resp = self._resp_queue.get(timeout=remaining)
            except queue.Empty:
                if self._proc is not None and self._proc.poll() is not None:
                    raise RuntimeError("worker 意外退出")
                continue  # worker 还活着，继续等
            if resp.get("id") != rid:
                continue  # 历史残留响应，跳过
            break
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "worker 命令失败"))
        return resp.get("result", {})


def _default_worker_script() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")


# 单例：后端进程内只维持一个 worker
_client: MaaClient | None = None


def get_client() -> MaaClient:
    global _client
    if _client is None:
        _client = MaaClient()
    return _client


def stop_client() -> None:
    global _client
    if _client is not None:
        _client.stop()
        _client = None
