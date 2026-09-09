"""打包版冒烟：启动 exe → 扫端口探测后端 → 验证新前端/图标/会话 → taskkill 干净退出。"""
import os
import sys
import time
import json
import subprocess
import urllib.request
from urllib.parse import quote

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(HERE, "dist", "猫娘来咯.exe")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def listening_ports() -> list:
    """返回 127.0.0.1 / 0.0.0.0 上 LISTENING 的端口（GBK 兼容解码）。"""
    out = subprocess.run(["netstat", "-ano"], capture_output=True,
                         creationflags=CREATE_NO_WINDOW).stdout.decode("gbk", errors="ignore")
    ports = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            try:
                addr, port = parts[1].rsplit(":", 1)
            except ValueError:
                continue
            if addr in ("127.0.0.1", "0.0.0.0", "[::]"):
                ports.append(int(port))
    return ports


def find_backend() -> str | None:
    """扫所有监听端口，找响应含 sessions 键的猫娘后端。"""
    for port in listening_ports():
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=2) as r:
                data = json.load(r)
                if "sessions" in data and "current" in data:
                    return f"http://127.0.0.1:{port}"
        except Exception:
            continue
    return None


def main():
    assert os.path.exists(EXE), f"缺少 exe: {EXE}"
    proc = subprocess.Popen([EXE], cwd=HERE)
    try:
        base = None
        for _ in range(40):  # 最长等 20s
            base = find_backend()
            if base:
                break
            time.sleep(0.5)
        if not base:
            print("✗ 未找到后端端口")
            sys.exit(1)
        print("后端端口:", base)

        def probe(path):
            # 中文路径段需 URL 编码
            quoted = quote(path, safe="/:?=&")
            with urllib.request.urlopen(base + quoted, timeout=5) as r:
                return r.status

        assert probe("/") == 200
        assert probe("/css/style.css") == 200
        assert probe("/js/app.js") == 200
        assert probe("/img/icons/设置.png") == 200, "图标未内嵌/exe 旁缺失"
        assert probe("/img/icons/刷新.png") == 200
        assert probe("/img/icons/发送.png") == 200

        html = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
        assert 'class="sidebar"' in html and 'id="sessionList"' in html, "HTML 缺侧栏结构"
        assert "/img/icons/设置.png" in html and 'id="collapseBtn"' in html, "HTML 缺图标/折叠"
        assert 'id="group-apps"' in html and 'id="group-plugins"' in html, "HTML 缺设置分组"
        assert 'id="pomodoroWorkInput"' in html, "HTML 缺设置字段"
        assert 'id="chatSearchInput"' in html, "HTML 缺侧栏搜索框"

        with urllib.request.urlopen(base + "/api/sessions", timeout=5) as r:
            data = json.load(r)
            assert "sessions" in data and "current" in data, "sessions API 异常"

        cur = data["current"]
        with urllib.request.urlopen(base + f"/api/sessions/{cur}/search?q=" + quote("的"), timeout=5) as r:
            search = json.load(r)
            assert "results" in search, "search API 异常"

        print("冒烟通过 ✅ | 静态资源 200 | 双栏结构/图标/搜索框/设置字段齐全 | 会话+搜索 API 正常")
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
        time.sleep(1)
        print("已 taskkill /T 退出")


if __name__ == "__main__":
    main()
