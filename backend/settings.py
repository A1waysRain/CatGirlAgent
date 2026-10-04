"""用户设置：%APPDATA%/catgirl/settings.json 的读写。

字段（含默认值）：
- show_pet           是否显示桌面宠物
- pet_name           猫娘名字（聊天头部标题）
- theme_color        主题色（十六进制）
- notification_sound 桌宠来消息是否播放提示音
- font_scale         聊天字体缩放
- show_timestamp     消息是否显示时间
- user_avatar / cat_avatar  聊天头像文件名（img/ 下）
- search_open_browser  主人说「搜/查」时是否弹出浏览器（默认开；关掉则只读内容不弹窗）
- allow_auto_web_search 猫娘能否自主联网（默认关：只认「明说搜/查」和「知识库没材料」两条确定性触发）
"""
import json
import os
from pathlib import Path


def _file() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "settings.json"


DEFAULTS = {
    "show_pet": True,
    "pet_name": "猫娘",
    "theme_color": "#ec4899",
    "notification_sound": True,
    "font_scale": 1.0,
    "show_timestamp": False,
    "user_avatar": "user.jpg",
    "cat_avatar": "cat.png",
    # 点击窗口关闭时的行为："tray"=仅隐藏到托盘（进程后台存活）；"exit"=直接退出
    "close_behavior": "tray",
    # 桌宠闲时踱步开关
    "pet_walk": True,
    # 定时提醒（桌宠扩展 M4，全走开关，默认关）
    "water_reminder": False,    # 喝水提醒：每 45 分钟
    "stretch_reminder": False,  # 久坐提醒：每 60 分钟
    "pomodoro": False,          # 番茄钟总开关
    "pomodoro_work": 25,        # 番茄钟工作时长（分钟）
    "pomodoro_break": 5,        # 番茄钟休息时长（分钟）
    "sleep_nudge": False,       # 深夜劝睡（23:00–06:00 偶尔）
    # 手机接入：默认关闭，只绑定用户选择的私网网卡，不监听 0.0.0.0
    "lan_enabled": False,
    "lan_ip": "",
    "lan_port": 8800,
    "lan_token": "",
    # 主人说「搜/查」时是否弹出浏览器给主人看（关掉则只读内容、不弹窗）
    "search_open_browser": True,
    # 猫娘能否自主联网（默认关，2026-10-04 主人拍板）：关=只有「主人明说搜/查」和
    # 「RAG 知识库明确没材料」两条确定性路径会联网，模型不得因“最新/赛程/天气”自行核验
    "allow_auto_web_search": False,
}


def load_settings() -> dict:
    """读取设置，缺字段用默认补全。"""
    data = dict(DEFAULTS)
    try:
        f = _file()
        if f.exists():
            data.update(json.loads(f.read_text(encoding="utf-8")))
    except Exception:
        pass
    return data


def save_settings(updates: dict) -> dict:
    """合并保存设置，只接受已知字段；返回保存后的完整设置。"""
    cur = load_settings()
    for k, v in (updates or {}).items():
        if k in cur:
            cur[k] = v
    try:
        _file().parent.mkdir(parents=True, exist_ok=True)
        _file().write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return cur
