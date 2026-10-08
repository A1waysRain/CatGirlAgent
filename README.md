# 猫娘来咯 🐱

**一个 Windows 桌面 AI 助理**：不只是聊天——她能操作你的电脑、盯着你的日程、发微信、查资料、还能在你桌面上当一只会走的猫。

单文件 `猫娘来咯.exe`（约 131.7 MB）双击即用，连桌宠一起起来。底层是 **DeepSeek 驱动的 function-calling agent**。

```
┌─────────────────────────────────────────┐
│  桌面壳 desktop/app.py (pywebview)            │ ← 原生窗口，内嵌 WebView2
│  ┌─────────────────────────────────────┐  │
│  │  网页前端 index.html + js/app.js       │  │ ← 无框架、无构建，后端挂载静态
│  └───────────────┬─────────────────────┘  │
│                  │ HTTP 127.0.0.1:随机端口   │
│  ┌───────────────▼─────────────────────┐  │
│  │  FastAPI 后端 backend/（同进程线程）     │  │
│  │   routers/ · llm.py（agent 循环）       │  │
│  │   tools.py（32 个工具）· rag.py · …     │  │
│  └───────────────┬─────────────────────┘  │
└──────────────────┼────────────────────────┘
                   └─→ DeepSeek API ／ 本地 RAG ／ MaaFramework(微信视觉自动化)
```

选 pywebview 而不是 Electron：后端本来就是 Python（FastAPI），pywebview 让 uvicorn 与 GUI **同进程**、无跨语言通信；Win11 自带 WebView2，比 Electron 轻得多。

---

## 功能亮点

### 💬 聊天

- **流式输出（SSE）**：回复像打字机一样逐字吐出；工具执行时气泡里显示「正在激烈操作喵…」。
- **多会话** + **单轮删除 / 重新生成**（重答失败会恢复原答，不会丢你的话）+ **聊天记录搜索**。
- **会话标题**：首轮问答结束后由独立的**提炼子 agent** 概括成短标题，顶栏 ✎ 可手改（改过就不再被覆盖）。
- **两级上下文管理**：L1 按 token 预算取历史；L2「**记忆小本本**」把掉出窗口的旧对话增量摘要进会话。摘要**只追加、不重写**——这是为了保住模型侧的前缀缓存（省钱前提，不是实现细节）。
- **工具结论生命周期**：「已打开原神」这类一次性结论过期后不再进模型上下文，但聊天记录照旧保留。
- **时间感知**：隔了几天回来，她知道中间过了很久，会傲娇地关心，而不是质问「你不是去睡觉了吗」。

### 🛠️ 本地操作（function calling，她真的会动手）

说人话 → 她拆成工具调用 → 干完如实汇报。**共 32 个工具**，基础 7 个：`list_dir` / `read_file` / `open_path` / `launch_app` / `open_url` / `append_file` / `replace_in_file`。

几个"不像 demo"的地方：

- **说裸文件名就行**：「打开 提示词.txt」——会自动在 桌面/文档/下载/当前目录 里按名字找，不用敲全路径。
- **文件 vs 应用自动区分**：`launch_app` 收到 `.docx` 这类会纠正"这是文件不是应用"，不会误开。
- **打开后抢前台焦点**：解开 Windows 前台锁把新窗口顶到最前，不会"点了没反应"。
- **应用自动发现**：扫开始菜单+桌面快捷方式，一次发现 80+ 个可启动应用（微信、原神…）。

#### 安全五约束（她能动手，但绝不乱动）

| 约束 | 做法 |
|---|---|
| 只读写安全类型 | 仅文本类扩展名；`.docx/.xlsx/.pptx` 走专门解析；其余二进制一律拒绝，新建的也不放行 |
| 敏感文件拒绝 | `.env` / `config.json` 等含密钥文件，读和改都被拒 |
| 危险操作二次确认 | **10 个工具**必须带 `confirmed=true` 才动手（写文件、杀进程、发微信…）——先问你、你同意才执行，工具层硬拦 |
| 全程审计 | 每次工具动作写进 `%APPDATA%\catgirl\actions.log` |
| 诚实汇报 | 真调用了才说"已打开/已写入"；**另有后端审计兜底**——她要是空口声称"已生成"却没调工具，后端会在回复末尾自动追加一段更正 |

### 🐱 桌宠

Tkinter 透明无边框置顶窗（独立子进程）：行为状态机（待机 / 闲逛踱步 / 空闲入睡）、**贴边停靠**、拖边缩放、9 种情绪表情随对话切换、**定时提醒**（喝水 / 久坐 / 劝睡 / 番茄钟）与**定点闹钟**（「十点半提醒我做日常」人话直建，支持一次性/每天/每周/指定某天）。

### 📄 文档 & PPT & Excel 创作

`docx / xlsx / pptx` 透明读写（读 / 追加 / 替换 / 排版），每次修改**自动备份 + 保存后重开自检 + 失败回滚**。`create_pptx` 一次生成整套 PPT（**主题色随内容自动匹配**，带演讲备注）；`create_xlsx` 按结构化数据建多 sheet 表格。

### 🔍 联网：搜索即读内容 + 事实核验

- 「搜 xxx」**既打开浏览器给你看，后端也把网页正文读回来**——她真的知道你搜到了什么，而不是只回"页面已打开"。
- **证据分级**：只有读到正文（full）才算证据，搜索摘要/首页导航不算——不支持拿搜索垃圾硬顶。
- **子 agent 提炼**：网页原文先交给一个独立低温度的"提炼工人"压成 要点/列表/来源/缺口/矛盾 再进主对话——**对主 agent 只是一个工具，主循环零改动**。
- **时效性分层**：来源按发布时间分「近期 / 旧稿」，提示词锁死"旧稿只代表过去的说法、不得当当前安排"。
- **确定性兜底**：「搜」是正则识别后**后端直接执行**的，不赌模型自觉——治"嘴上说已打开、其实没调工具"的老毛病。

### 📚 本地知识库（手搓 Mini-RAG）

教材/文档喂进本地知识库，按需 `rag_query` 检索并**带来源引用**回答。fastembed（bge-small-zh）+ numpy 余弦 top-k，**不引 langchain / chromadb**（只加一个依赖，onnxruntime 复用 OCR 已有的）；**本地向量、不上云**。设计上坚持**模型按需调用**而非每轮硬塞 prompt——否则会击穿那段稳定前缀、把缓存全打掉。

### 💬 微信联动（MaaFramework 视觉自动化）

纯视觉控制真微信 PC：搜联系人 → 打字 → 发送，微信没开自动拉起。**发前自动核名**（OCR 聊天标题对不上就拒绝发送）、**标记机制**保证一条消息只发一次、发文件走剪贴板 `CF_HDROP` 粘贴（绕开脆弱的原生文件对话框）。

> 微信 4.x 的 UI 是自研引擎画在窗口里的像素，**UIA 无障碍树是空壳**（实测只有两个空 Pane）——所以这条路只能走视觉。

### 🖼️ 图片识别 & 插件

RapidOCR 中文 OCR 打进 exe（抠小字/表格比视觉模型准）；`mode=vision` 把图片交给 DeepSeek 原生多模态"真看"（能读图表趋势、界面布局）。插件系统 `%APPDATA%\catgirl\plugins\` 即插即用、热重载、坏插件隔离。

### 📱 手机接入（多端）

手机浏览器连同一后端 = **第二前端**，聊天/记忆/提醒/知识库/本地操作**全部同一份**（记录只存电脑，**零同步代码**）——手机可以直接遥控电脑。安全上：默认关、手动开、**5 分钟无访问自动关**，后端只绑私网网卡 IP（不绑 `0.0.0.0`），令牌换 httpOnly cookie，**Key 永不下发手机**。

---

## 技术栈

| 层 | 用了什么 |
|---|---|
| 模型 | DeepSeek `deepseek-flash`（1M 上下文、原生多模态），OpenAI 兼容接口 |
| 后端 | Python 3.14 · FastAPI · uvicorn · httpx（异步）· pydantic |
| 桌面 | pywebview（WebView2）· Tkinter（桌宠）· pystray（托盘）|
| 前端 | 原生 HTML/CSS/JS，无框架无构建；SSE 用 `fetch` + `ReadableStream` 手写解析 |
| 文档 | python-docx / openpyxl / python-pptx |
| 视觉/OCR | MaaFramework（微信自动化，独立非冻结 worker 进程）· RapidOCR + onnxruntime |
| 检索 | fastembed（bge-small-zh）+ numpy |
| 打包 | PyInstaller onefile（`desktop/build.spec`）|

---

## 快速开始

需要 **Windows 10/11 + Python 3.14**（WebView2 运行时 Win11 自带）。

```bash
# 1. 建虚拟环境并装依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 配置 Key（复制模板后填自己的）
copy .env.example .env        # 然后编辑 .env，填 DEEPSEEK_API_KEY

# 3. 启动（弹窗 + 桌宠一起起）
.venv\Scripts\python.exe desktop\app.py
```

也可以只跑后端，用浏览器访问 `http://127.0.0.1:8000`：

```bash
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

**API Key 永不进仓库、也不进 exe**：`.env` 只用于开发（已在 `.gitignore`）；打包版从 `%APPDATA%\catgirl\config.json` 读。

```jsonc
// %APPDATA%\catgirl\config.json
{ "deepseek_api_key": "sk-你的密钥" }
```

### 打包

```bash
.venv\Scripts\pyinstaller desktop\build.spec --clean --noconfirm
# 产物：dist\猫娘来咯.exe
```

⚠️ **部署单元是整个 `dist\`**，不是单个 exe：微信自动化需要外置的 `dist\python\`（嵌入版 Python，跑非冻结的 Maa worker，规避"内存资源不足"）与 `dist\maa_assets\`（OCR 模型）。

---

## 项目结构

```
Cat_Girl/
├── desktop/          # 桌面壳：app.py（壳+托盘+LAN）/ pet.py（桌宠）/ screenshot.py / build.spec
├── backend/
│   ├── main.py       # create_app() 装配：routers + CORS/Host 中间件 + 静态挂载
│   ├── routers/      # 按域拆的 APIRouter：chat/sessions/alarms/apps/plugins/media/pet/settings/rag_files
│   ├── llm.py        # agent 循环（流式）+ 系统提示词
│   ├── tools.py      # 32 个工具的实现与 schema（含安全五约束）
│   ├── chat_service.py  # SSE 编排 + build_messages（记忆/提醒注入）+ 两类审计兜底
│   ├── sessions.py / summary.py / title.py   # 会话、记忆小本本、标题提炼
│   ├── agents.py     # 独立子 agent（网页提炼 worker、标题提炼）
│   ├── rag.py        # 手搓 Mini-RAG：切块 / 向量 / 落盘 / 查询改写
│   ├── scheduler.py  # 定时提醒 + 定点闹钟
│   └── maa_ops/      # 微信自动化：maa_client（spawn/看门狗/超时重启）+ worker（非冻结子进程，JSON-RPC）
├── js/ css/ index.html        # 前端（桌面）
├── index_m.html / js/app_m.js / css/mobile.css   # 手机端页面
├── tests/            # 回归测试与端到端脚本（见下）
└── requirements.txt
```

## 测试

`tests/` 下是这套项目的**回归门禁**，改完代码会真跑一遍（`_verify_*` 打桩不耗 token，`_e2e_*` 真调模型）：

```bash
# 需要从仓库根跑，PYTHONPATH 指向根
set PYTHONPATH=%CD%
.venv\Scripts\python.exe tests\_verify_frontend.py     # 前端端到端（Playwright + 真后端，mock SSE）
.venv\Scripts\python.exe tests\_verify_compress.py     # 上下文压缩 / 记忆小本本
.venv\Scripts\python.exe tests\_verify_alarms.py       # 定时提醒与定点闹钟
.venv\Scripts\python.exe tests\_verify_chat_fixes.py   # 确定性兜底与 regenerate 加固
.venv\Scripts\python.exe tests\_smoke_pkg.py           # 打包版 exe 冒烟
```

## 已知限制

- **微信自动化依赖视觉**：OCR 对小字/数字会误读（所以发送前强制核名）；多文件批量发送未做。
- **免 Key 的联网质量不保证**：默认走 Bing 通道，正文质量看运气；配了搜索 API Key 才稳。
- **不支持 PDF 修改/读取正文**：印刷稿改版式必乱，上传后只能当附件展示。`.doc/.xls` 旧格式需转存。
- **部署需要整个 `dist\`**：只拷 exe 的话微信相关工具不可用（见上）。
- 桌宠是 Tkinter 实现（约 30fps）；想上高帧数需要换成 Qt Quick/QML 重写，暂缓。

## License

[MIT](LICENSE)
