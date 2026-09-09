# 猫娘来咯 🐱

一只傲娇又温柔的猫娘聊天软件。**桌面壳 + 网页前端 + FastAPI 后端** 三层架构：

```
┌───────────────────────────────┐
│   桌面壳 desktop/app.py (pywebview) │  ← 原生窗口，内嵌 WebView2
│  ┌─────────────────────────┐  │
│  │  网页前端 index.html + js  │  │  ← 现有前端，零改动复用
│  └──────────┬──────────────┘  │
│             │ HTTP 127.0.0.1:随机端口
│  ┌──────────▼──────────────┐  │
│  │  FastAPI 后端 backend/    │  │  ← 同进程后台线程
│  │     └─ DeepSeek API      │  │
│  └─────────────────────────┘  │
└───────────────────────────────┘
```

## 目录结构

```
Cat_Girl/
├── desktop/
│   ├── app.py          # 桌面壳入口：随机端口 + uvicorn 线程 + 窗口
│   └── build.spec      # PyInstaller 打包配置（单文件 exe）
├── backend/
│   ├── main.py         # FastAPI 应用工厂 + run_server()
│   ├── llm.py          # DeepSeek 调用 + 猫娘人设提示词
│   ├── config.py       # 配置读取（.env + 用户级 config.json 兜底）
│   └── schemas.py      # 请求/响应模型
├── index.html / css/ / js/   # 现有网页前端（未改动）
├── requirements.txt
└── .env                # DeepSeek 配置（仅开发用，不打进 exe）
```

## 开发运行

```bash
# 1. 安装依赖（一次性）
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. 启动桌面壳（会弹出猫娘窗口）
.venv\Scripts\python.exe desktop\app.py
# 带调试工具: .venv\Scripts\python.exe desktop\app.py --debug
```

也可以只用后端（浏览器访问 http://127.0.0.1:8000）：

```bash
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

## 打包单文件 exe

```bash
.venv\Scripts\pyinstaller desktop\build.spec --noconfirm
# 产物: dist\猫娘来咯.exe
```

**API Key 安全**：`.env` 和密钥**不**会打进 exe。打包后首次使用，在用户目录新建

```
%APPDATA%\catgirl\config.json
```

内容：

```json
{ "deepseek_api_key": "sk-你的密钥" }
```

程序启动时若 `.env` 未配置 Key，会自动读取该文件（开发时 `.env` 优先）。

## 常见问题

- **窗口打开但猫娘不回复**：检查 DeepSeek Key 是否有效、网络是否可达。
- **打不开窗口**：Windows 11 自带 WebView2 运行时；老系统需装
  [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。
- **打包后缺模块**：确认用 `build.spec` 构建（已收集 uvicorn / webview /
  pythonnet 的隐藏依赖）。
