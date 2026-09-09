# -*- coding: utf-8 -*-
"""验证 2026-09-01 内存「实锤数据」监控（隔离 APPDATA，不写真实日志）：
1. health.sample_memory：物理内存 + 提交内存数值合法
2. health.sample_procs：进程树包含根进程和子进程（模拟桌宠/worker/WebView2 都是主进程后代）
3. health 日志：write_line/read_recent_log/轮转（不无限增长）
4. tool_check_system + 注册（TOOL_IMPL/TOOL_SCHEMAS）
5. chat() 确定性兜底：说「看看内存」→ 注入真实数据 + 摘掉 check_system（照搜/提醒模式）
6. _extract_system_check_request 触发/不触发
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, ".")

import backend.routers.chat as chat_mod
from backend.routers.chat import _extract_system_check_request as F

_tmp_dirs = []


def fresh_appdata():
    d = tempfile.mkdtemp(prefix="catgirl_verify_health_")
    os.environ["APPDATA"] = d
    _tmp_dirs.append(d)


ok = True


def check(label, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  {extra}" if extra else ""))


print("===== 1. 内存/提交内存采样合法 =====")
fresh_appdata()
from backend import health

m = health.sample_memory()
check("采样返回非空", bool(m), str(m))
check("物理内存 total>0", m.get("mem_total_mb", 0) > 0)
check("可用≤总量", m.get("mem_avail_mb", 1 << 30) <= m.get("mem_total_mb", 0))
# 已用、总量、可用各自取整 MB 后再算可能差 1（9203 vs 9204），放宽容差
check("已用=总量-可用", abs(m.get("mem_used_mb") - (m.get("mem_total_mb") - m.get("mem_avail_mb"))) <= 1)
check("提交限制 total>0", m.get("commit_limit_mb", 0) > 0)
check("提交可用≤限制", m.get("commit_avail_mb", 1 << 30) <= m.get("commit_limit_mb", 0))

print("===== 2. 进程树含根进程 + 子进程 =====")
ps = health.sample_procs(os.getpid())
check("根进程在树里", any(p["pid"] == os.getpid() for p in ps), str(ps[:3]))
# 造一个真子进程（模拟桌宠/worker），验证父链能收进来
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
time.sleep(1.0)
ps2 = health.sample_procs(os.getpid())
check("子进程在树里", any(p["pid"] == child.pid for p in ps2),
      f"child={child.pid} 树={[p['pid'] for p in ps2]}")
ws_child = next((p["ws_mb"] for p in ps2 if p["pid"] == child.pid), 0)
check("子进程工作集取到", ws_child >= 0)
child.kill()
child.wait(timeout=5)

print("===== 3. 日志读写 + 轮转 =====")
line = health.make_line()
check("一行日志含内存和进程", "内存" in line and "进程" in line, line[:80])
health.write_line(line)
got = health.read_recent_log(3)
check("写入后能读回", any(line.strip() == x.strip() for x in got), str(got))
# 轮转：把上限调小，写一堆行，文件只留尾部
_real_max = health.MAX_LOG_LINES
health.MAX_LOG_LINES = 5
for i in range(8):
    health.write_line(f"测试行 {i}")
tail = health.read_recent_log(20)
check("超上限裁到最多 5 行", len(tail) <= 5, f"实际 {len(tail)} 行")
check("保留的是最后几行", any("测试行 7" in x for x in tail), str(tail))
health.MAX_LOG_LINES = _real_max

print("===== 4. 看门狗幂等启停 =====")
health.start(interval=60)
tid1 = health._thread
health.start()   # 重复 start 不应起新线程
check("重复 start 幂等（同线程）", health._thread is tid1 and tid1.is_alive())
health.stop()
tid1.join(timeout=2)
health.start(interval=60)
tid2 = health._thread
check("停止后可重新启动", tid2 is not tid1 and tid2.is_alive())
health.stop()
tid2.join(timeout=2)

# --pet 桌宠子进程不启动看门狗（打包版桌宠走同一 exe --pet，会二次 import backend.main）
_saved_argv = sys.argv
sys.argv = ["猫娘来咯.exe", "--pet"]
health.start(interval=60)
check("--pet 子进程不启动看门狗", health._thread is tid2 and not health._thread.is_alive())
sys.argv = _saved_argv
health.start(interval=60)
check("退出 --pet 后可正常启动", health._thread is not tid2 and health._thread.is_alive())
health.stop()
health._thread.join(timeout=2)

print("===== 5. check_system 工具 + 注册 =====")
from backend import tools

out = tools.tool_check_system()
check("工具返回含内存/提交内存/进程", "内存" in out and "提交内存" in out and "进程" in out, out[:80])
check("工具不误称页面文件已用", "页面文件(虚拟内存) 已用" not in out, out[:120])
check("TOOL_IMPL 注册", "check_system" in tools.TOOL_IMPL)
check("TOOL_SCHEMAS 注册", any(s["function"]["name"] == "check_system" for s in tools.TOOL_SCHEMAS))

print("===== 6. _extract_system_check_request 触发 =====")
triggers = [
    "看看内存", "看下内存占用", "查一下内存够不够", "检查一下系统状态",
    "帮我看看是不是内存爆了", "为什么这么卡", "咋这么卡", "怎么卡卡的",
    "为什么会卡", "怎么又卡了", "现在卡不卡", "卡不卡", "卡不卡啊",
    "内存够不够", "虚拟内存够吗", "页面文件多少了", "是不是内存不够",
    "本机内存占用高不高", "电脑虚拟内存够不够", "看下卡顿不", "帮我看看是不是卡",
]
for t in triggers:
    check(f"触发：{t}", bool(F(t)))
notriggers = [
    "这游戏一点都不卡", "已经不卡了", "今天很流畅", "我卡里没钱了",
    "看看这张卡", "刷卡", "卡牌游戏", "帮我打开微信", "今天天气怎么样",
    "你好呀", "", "为什么卡里没钱",
]
for t in notriggers:
    check(f"不触发：{t!r}", not F(t))

print("===== 7. chat() 确定性兜底（打桩） =====")
captured = {}


async def fake_stream_answer(sid, msgs, user_text, tools=None, user_msg_id=None,
                             write_requested=False, write_state=None, alarm_created=None):
    captured["tools"] = sorted(t["function"]["name"] for t in (tools or []))
    captured["msgs"] = msgs
    yield {"type": "done", "message_ids": {"assistant": "fake"}}


chat_mod.stream_answer = fake_stream_answer
_real_sys = chat_mod.tool_check_system
chat_mod.tool_check_system = lambda: "【真实数据】系统内存 已用 10000 / 16384 MB，页面文件 已用 2000 / 29036 MB，猫娘相关进程：猫娘来咯.exe(pid 1) 500 MB"


async def call_chat(text):
    captured.clear()
    resp = await chat_mod.chat(SimpleNamespace(chatmassage=text, session_id=None))
    async for _ in resp.body_iterator:
        pass
    return captured


async def run():
    c = await call_chat("看看内存")
    notes = " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system")
    check("说'看看内存'：check_system 被摘掉", "check_system" not in c["tools"], str(c["tools"]))
    check("说'看看内存'：真实数据注入 system", "系统状态本喵已直接查好" in notes and "10000 / 16384" in notes)
    check("说'看看内存'：其他只读工具保留", "read_file" in c["tools"] and "get_time" in c["tools"])

    c = await call_chat("为什么这么卡")
    notes = " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system")
    check("'为什么这么卡'：check_system 被摘掉", "check_system" not in c["tools"])
    check("'为什么这么卡'：真实数据注入", "系统状态本喵已直接查好" in notes)

    c = await call_chat("你好呀")
    check("无关消息：check_system 保留", "check_system" in c["tools"], str(c["tools"]))
    check("无关消息：无系统状态注入", "系统状态本喵已直接查好" not in
          " ".join(m.get("content", "") for m in c["msgs"] if m["role"] == "system"))


asyncio.run(run())
chat_mod.tool_check_system = _real_sys

print("===== 8. import backend.main 装配（顺带看门狗随 create_app 起） =====")
fresh_appdata()
try:
    import backend.main as bm
    check("create_app 可装配", bm.app is not None)
    health.stop()  # 停掉装配时起的看门狗，避免写真实日志
except Exception as e:
    check(f"create_app 可装配：{e}", False)

print()
print("全部通过" if ok else "存在失败项")
# 清理自己建的临时目录（只删自己建的，绝不碰真实数据）
for d in _tmp_dirs:
    shutil.rmtree(d, ignore_errors=True)
sys.exit(0 if ok else 1)
