# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

猫娘来咯：桌面壳 + 网页前端 + FastAPI 后端 三层架构的 DeepSeek 猫娘桌面助手。开发/验证/打包命令都在本目录（`Cat_Girl/`）执行。已 git 管理（2026-09-09，私有 GitHub `A1waysRain/CatGirl`）。

## 常用命令

所有命令用 `.venv\Scripts\python.exe`（Python 3.14 venv）。

```bash
# 开发运行（桌面壳 + 桌宠子进程；pythonw 无控制台，python 带日志）
.venv\Scripts\python.exe desktop\app.py
pythonw desktop\app.py

# 只用后端（浏览器访问 http://127.0.0.1:8000）
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# 验证/回归：脚本在 `tests/`（`_verify_*.py` 一主题一脚本，部分要 Playwright + msedge）。
# 从本目录跑必须 PYTHONPATH 指项目根（cmd 用 %CD%，git-bash 用 $(pwd)）：
PYTHONIOENCODING=utf-8 PYTHONPATH="%CD%" .venv\Scripts\python.exe tests\_verify_frontend.py     # 前端端到端（真后端+mock SSE，不耗 DeepSeek token）
PYTHONIOENCODING=utf-8 PYTHONPATH="%CD%" .venv\Scripts\python.exe tests\_verify_chat_fixes.py   # chat 确定性兜底/regenerate 加固
PYTHONIOENCODING=utf-8 PYTHONPATH="%CD%" .venv\Scripts\python.exe tests\_verify_doc_lineref.py  # 教材《流式输出-大白话对号猫娘》行号核对
# 常用：tests/_verify_compress（上下文压缩）/ tests/_verify_alarms(+_date)（提醒）/ tests/_verify_launch_detect（打开应用）/
#       tests/_verify_write_claim（写文件声称审计）/ tests/_verify_dock / tests/_verify_mood_timeout（桌宠）
# `_e2e_*.py` 消耗真实 DeepSeek token（端到端）；`_smoke_pkg.py` 冒烟打包版 exe

# 语法检查
node --check js/app.js
.venv\Scripts\python.exe -m py_compile backend/main.py

# 打包单文件 exe（改过 backend 代码必须 --clean 重建，否则 exe 里是旧模块）
.venv\Scripts\pyinstaller desktop\build.spec --clean --noconfirm
```

**打包由用户拍板**：功能改完默认不重打包，用户明确说"打包/重打包/出 exe"才执行。

## 架构（三层）

```
desktop/app.py（pywebview 壳）
  └─ 随机端口起 uvicorn 后台线程 → 加载 http://127.0.0.1:<port>
       └─ backend/main.py create_app() 工厂（装配 routers + CORS/Host 中间件 + 静态挂载）
            └─ backend/routers/*.py：按域拆的 APIRouter（chat/sessions/apps/plugins/media/pet/settings/alarms）
```

- **前端**：单文件 `js/app.js`（~1500 行）+ `index.html` + `css/style.css`。无框架、无构建，后端挂载静态。
- **聊天流式链路**（改聊天必读，四层跳转）：
  `routers/chat.py:356 chat()`（确定性命令兜底）→ `chat_service.py:102 stream_answer()`（SSE 事件编排：meta→delta*/status*/error→done，流结束才落库）→ `llm.py:214 call_deepseek_with_tools_stream()`（agent 循环：工具轮聚合 tool_calls 不直播、正文轮直播 delta，最多 5 轮）→ `llm.py:186 _stream_chat_json()`（OpenAI 兼容逐行 SSE 请求）。
  前端 `js/app.js:687 fetchSSE()`（fetch+ReadableStream 读流，`dispatchBlock` 分发）+ `js/app.js:609 createStreamingBot()`（打字机式 reveal）。
  SSE 协议：`data: <json>\n\n`；事件 type = meta/status/delta/done/error（`[DONE]` 被忽略，完成靠 JSON done）。前端请求体字段是 **`chatmassage`**（拼写就这样，改了前后端对不上）+ `session_id`。
- **"确定性兜底"模式**（chat.py 的核心套路）：对"搜/提醒/打开应用/写文档"这类命令用正则 `_extract_*` 先一步识别、后端直接执行（开浏览器/建闹钟/launch_app），成功就往 msgs 注入 system 通知 + 把对应工具加进 `disabled_tools` 统一过滤——治模型"空口声称已办成却从不调工具"的老毛病。流结束还有两类审计兜底：写文件声称（`_CLAIM_WORDS`）、闹钟否认（`_ALARM_DENIAL_RE`），命中就追加"诚实更正" delta。
- **会话/记忆**：`sessions.py` 每会话一个 JSON 文件（`%APPDATA%\catgirl\sessions\`），`append_message` 空内容不追加。上下文压缩 L2：`summary.py` 后台线程把掉出窗口的旧对话摘要记进会话（"记忆小本本"），`build_messages` 拼进 system 之后、recent 之前（保前缀稳定省 DeepSeek 缓存）。
- **微信自动化**：`backend/maa_ops/worker.py`（独立**非冻结**子进程 + JSON-RPC，退出必须 `os._exit(0)`）+ `maa_client.py`（spawn/看门狗/超时强制重启）。MaaFramework 视觉路线（微信 UIA 树是空壳、确认死路）。打包版 worker 用 `dist\python\python.exe`（嵌入版）+ `dist\maa_assets`。
- **桌宠/通知/定时**：`desktop/pet.py`（Tkinter 透明置顶窗，`--pet` 子进程，长轮询 `/api/pet/notifications`）；`notifier.py` 事件队列（notify/emotion/behavior）；`scheduler.py` 定时提醒 + 闹钟（alarms.json）。情绪 `mood.py infer_mood()` 走宠物通道，**不进聊天 SSE**。

## 关键注意事项

- **打包 ≠ 只拷 exe**：部署单元是整个 `dist\`（exe + `python\` 嵌入版 + `maa_assets\` + `img\` + `pet_assets\`）。Maa 微信工具必须连 python\+maa_assets 一起拷。
- **`.env` 不进 exe**：打包版从 `%APPDATA%\catgirl\config.json` 读 Key。
- **venv python 是套壳启动器**：任何 python 命令都是"壳+真身"两层进程，**不是双实例，别去杀**；查进程/杀进程用 `Get-CimInstance` 按命令行确认或按根 PID `taskkill /T`。
- **GBK 终端坑**：`tasklist | grep 中文` 匹配不到（误报 0 进程）；git-bash 里 `curl -d '中文'` body 解析错，要用 `--data @file`；验证脚本要 `PYTHONIOENCODING=utf-8`。
- **WebView2 启动竞态**：app.py 里那组 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`（--disable-gpu + 3 个 --disable-backgrounding-*）已**注释禁用**——它显著加剧"启动后窗口在屏外/未 resize"的一次性竞态，别随便重新启用。
- 中文 exe 名 OK（PyInstaller 6.22 直接生成正确文件名，终端里乱码只是 GBK 显示问题）。
- build.spec：所有路径用 `SPECPATH`/项目根拼绝对路径；**不能 exclude tkinter**（桌宠窗口依赖）；`backend/maa_ops/worker.py` 是非 import 引用必须显式打进 datas；`--noconfirm` 不会清空 dist 子目录。
- 教材在上级目录 `资料\`（流式输出/教学文档，行号与代码对应；改代码后跑 `tests/_verify_doc_lineref.py` 核对行号）。
- 上级目录还有：`项目介绍.md`（能力总览）、`施工方向\`（方案 + 完成归档）、`更新日志-2026-09-30.md`（根目录，全量迭代史）、`换设备声明\`（换电脑部署说明）。根目录有 `CLAUDE.md` 总述各文件夹。
