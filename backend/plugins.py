"""插件系统：扫描 %APPDATA%/catgirl/plugins/，加载工具插件，支持热插拔。

插件格式：`plugins/<插件名>/plugin.py`，暴露：
- `register() -> [{"name": 工具名, "schema": OpenAI工具schema, "func": 可调用}]`
- 可选：`PLUGIN_NAME`（显示名）、`PLUGIN_DESC`（描述）

- 装插件 = 拷一个含 plugin.py 的文件夹进 plugins 目录（或用设置界面添加），无需重打包 exe。
- 即插即用：reload_all() 移除旧插件工具 → 重扫 → 重新注册；聊天端每次读同一个
  TOOL_SCHEMAS 对象，下次对话立即生效。
- 单个插件加载失败不崩，状态标记（error）隔离。
"""

import importlib.util
import json
import os
import shutil
import sys
import time
from pathlib import Path

_impl = None        # TOOL_IMPL 引用（由 tools.py bind）
_schemas = None     # TOOL_SCHEMAS 引用
_plugin_tools: dict[str, list[str]] = {}   # plugin_id -> 注册的工具名
_cached_plugins: list = []                 # 最近一次扫描的元信息
_loaded_once = False


def _plugins_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "plugins"


def _state_file() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "plugins_state.json"


def _log(action: str) -> None:
    try:
        f = Path(os.environ.get("APPDATA", str(Path.home()))) / "catgirl" / "actions.log"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fp:
            fp.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {action}\n")
    except Exception:
        pass


def bind(impl: dict, schemas: list) -> None:
    """由 tools.py 注入可变的工具表引用，供运行时热插拔。"""
    global _impl, _schemas
    _impl = impl
    _schemas = schemas


def _load_state() -> dict:
    try:
        f = _state_file()
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"known": {}}


def _save_state(state: dict) -> None:
    try:
        f = _state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_plugin(plugin_dir: Path) -> dict:
    """加载单个插件，注册其工具，返回元信息；失败返回 error 状态的元信息。"""
    plugin_id = plugin_dir.name
    mod_path = plugin_dir / "plugin.py"
    if not mod_path.is_file():
        return {"id": plugin_id, "status": "error", "error": "缺少 plugin.py"}
    try:
        spec = importlib.util.spec_from_file_location(f"catgirl_plugin_{plugin_id}", mod_path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        register = getattr(mod, "register", None)
        if not callable(register):
            return {"id": plugin_id, "status": "error", "error": "没有 register()"}
        tools = register() or []
        names: list[str] = []
        for t in tools:
            name = t.get("name")
            if name and t.get("func") and t.get("schema"):
                _impl[name] = t["func"]
                _schemas.append(t["schema"])
                names.append(name)
        _plugin_tools[plugin_id] = names
        return {
            "id": plugin_id,
            "name": getattr(mod, "PLUGIN_NAME", plugin_id),
            "description": getattr(mod, "PLUGIN_DESC", ""),
            "tools": names,
            "status": "loaded",
        }
    except Exception as e:
        return {"id": plugin_id, "status": "error", "error": str(e)[:120]}


def reload_all() -> tuple[list, list]:
    """热重载：移除旧插件工具 → 重扫 plugins 目录 → 重新注册。

    返回 (全部插件元信息, 本次新增插件)。聊天端下一条消息即用上新工具。
    """
    global _cached_plugins, _loaded_once
    if _impl is None:
        return [], []
    # 1) 收集旧插件工具名并移除
    all_names = set()
    for names in _plugin_tools.values():
        all_names.update(names)
    for n in all_names:
        _impl.pop(n, None)
    _schemas[:] = [
        s for s in _schemas
        if ((s.get("function") or {}).get("name") not in all_names)
    ]
    _plugin_tools.clear()
    # 2) 重扫
    plugins: list = []
    d = _plugins_dir()
    if d.is_dir():
        for entry in sorted(d.iterdir()):
            if entry.is_dir():
                plugins.append(_load_plugin(entry))
    _cached_plugins = plugins
    _loaded_once = True
    # 3) 新增检测（对比上次已知）
    state = _load_state()
    known = state.get("known", {})
    new_plugins = [p for p in plugins if p.get("id") and p["id"] not in known]
    state["known"] = {
        p["id"]: {"name": p.get("name", p["id"]), "tools": p.get("tools", [])}
        for p in plugins if p.get("id")
    }
    _save_state(state)
    return plugins, new_plugins


def get_plugins() -> list:
    """当前已加载插件列表（首次调用会触发一次扫描）。"""
    if not _loaded_once:
        reload_all()
    return _cached_plugins


def add_from_path(src: str) -> tuple[list, list]:
    """把用户选择的插件文件夹复制进 plugins 目录，并热加载。"""
    src_dir = Path(src)
    dst_dir = _plugins_dir() / src_dir.name
    try:
        if dst_dir.exists():
            shutil.rmtree(dst_dir, ignore_errors=True)
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_dir, dst_dir)
    except Exception as e:
        return [], []
    return reload_all()


def remove(plugin_id: str) -> tuple[list, list]:
    """删除插件目录并热重载。"""
    dst = _plugins_dir() / plugin_id
    try:
        if dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
    except Exception:
        pass
    return reload_all()


def list_plugins_text() -> str:
    """给猫娘看的插件列表文本（list_plugins 工具用）。"""
    plugins = get_plugins()
    if not plugins:
        return "本喵目前没装插件喵。可以在设置→插件里添加喵"
    lines = []
    for p in plugins:
        if p.get("status") == "loaded":
            tag = "✅ 已加载"
            tools = "、".join(p.get("tools", [])) or "无工具"
            lines.append(f"- {p.get('name', p.get('id'))}：{p.get('description', '')}（工具：{tools}）")
        else:
            lines.append(f"- {p.get('id')}：⚠️ {p.get('error', '加载失败')}")
    return "已装插件喵：\n" + "\n".join(lines)
