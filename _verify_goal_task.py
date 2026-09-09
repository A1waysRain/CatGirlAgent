"""定时任务 M2 回归：goal 授权圈、后台 agent、预算与真实调用审计。"""
import os
import tempfile
import threading
import time

base = tempfile.mkdtemp(prefix="catgirl-goal-")
os.environ["APPDATA"] = base

from backend import llm, tools
from backend.scheduler import Scheduler


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}", detail)
    if not ok:
        raise AssertionError(name)


scope = {
    "tools": ["read_file", "create_pptx", "ui_send_file"],
    "read_dirs": [base], "write_paths": [base], "contacts": ["小明"],
}
goal, frozen = tools.validate_scheduled_goal("根据文件做 PPT 并发送", scope)
scope["tools"].append("launch_app")
check("goal 和授权圈快照落盘", goal and frozen["tools"] == ["read_file", "create_pptx", "ui_send_file"])

for bad_scope, label in [
    ({"tools": ["read_file"]}, "读目录不能为空"),
    ({"tools": ["create_pptx"], "write_paths": [base]}, "危险工具缺读目录不影响"),
    ({"tools": ["ui_send_file"], "read_dirs": [base], "write_paths": [base]}, "发送联系人不能为空"),
    ({"tools": ["ui_send"]}, "拒绝状态依赖工具"),
]:
    try:
        tools.validate_scheduled_goal("测试", bad_scope)
        ok = label == "危险工具缺读目录不影响"
    except ValueError:
        ok = label != "危险工具缺读目录不影响"
    check(label, ok)

runner = tools.scoped_runner({"tools": ["read_file"], "read_dirs": [base], "write_paths": [], "contacts": []})
check("路径越界被拒", "越界喵" in runner("read_file", {"path": "/tmp/not-allowed.txt"}))
check("圈外工具被拒", "越界喵" in runner("launch_app", {"name": "微信"}))

calls = []
def dangerous(path, confirmed=False):
    calls.append((path, confirmed))
    return "已经写好喵"
tools.TOOL_IMPL["goal_danger_test"] = dangerous
try:
    safe_scope = {"tools": ["goal_danger_test"], "read_dirs": [], "write_paths": [], "contacts": []}
    check("危险 scope 可识别", tools.scheduled_goal_needs_confirmation(safe_scope))
    result = tools.scoped_runner(safe_scope)("goal_danger_test", {"path": "x"})
    check("危险工具自动注入 confirmed", result == "已经写好喵" and calls == [("x", True)], str(calls))
finally:
    tools.TOOL_IMPL.pop("goal_danger_test", None)

scheduler = Scheduler()
try:
    scheduler.add_alarm("09:00", "互斥", action={"tool": "get_time", "args": {}}, goal="也做", scope={"tools": ["get_time"]})
    exclusive = False
except ValueError:
    exclusive = True
check("action 与 goal 互斥", exclusive)

done = threading.Event()
received = {}
original = llm.call_deepseek_with_tools
async def fake_agent(messages, schemas, max_rounds=5, runner=None, trace=None):
    received["schemas"] = [item["function"]["name"] for item in schemas]
    received["goal"] = messages[-1]["content"]
    trace.append({"name": "get_time", "args": {}, "result": "现在是 09:00 喵"})
    done.set()
    return "已经查看时间喵"
llm.call_deepseek_with_tools = fake_agent
try:
    goal_alarm = scheduler.add_alarm("09:00", "查看时间", goal="看看现在几点", scope={"tools": ["get_time"]})
    pushed = []
    scheduler._push_alarm = lambda msg, chat_msg=None: pushed.append({"bubble": msg, "chat": chat_msg or msg})
    begin = time.monotonic()
    scheduler._start_goal_task(goal_alarm)
    check("goal 在线程中启动且不阻塞", time.monotonic() - begin < 0.2 and done.wait(2))
    check("只把 scope 工具喂给 agent", received.get("schemas") == ["get_time"], str(received))
    time.sleep(0.05)
    # 成功：气泡=短句（不含长汇报/工具名堆砌），聊天窗=稍全（含步数与工具名）
    got = pushed[-1] if pushed else {}
    check("气泡是简短确认", "「查看时间」好啦喵" in got.get("bubble", ""), str(got))
    check("聊天窗带步数工具名细节", "实际调用 1 步（get_time）" in got.get("chat", ""), str(got))
    check("聊天窗头不拖随机提醒后缀", "主人别忘了喵" not in got.get("chat", ""), str(got))
finally:
    llm.call_deepseek_with_tools = original

print("ALL PASS")
