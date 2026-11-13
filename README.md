# document_agent
An intelligent agent capable of writing documents based on the content of an organization's self-built knowledge base

## 运行环境
已验证环境：conda 环境 `docx_agent`（`D:\Conda\envs\docx_agent`，Python 3.10）

安装依赖（在仓库根目录执行）：
```bash
D:\Conda\envs\docx_agent\python.exe -m pip install -e .
```
`requirements.txt` 已包含 Web 版需要的 `fastapi`、`uvicorn`、`python-multipart`。

解析 PDF 参考文件时，若文件中含表格，会调用 `camelot` 提取表格（已随上面的依赖一起安装）。
`camelot` 的 `lattice` 模式还需要系统安装 Ghostscript；如果环境中没有 `camelot`，解析 PDF 时会自动降级为只提取文本，不会直接报错。

模型配置（DeepSeek，OpenAI 兼容接口），使用前需设置环境变量：
```bash
set MODEL_API_KEY=你的密钥
set MODEL_BASE_URL=https://api.deepseek.com/v1
set MODEL_NAME=deepseek-chat
```

模型默认值与 develop 版保持一致：`agent/llm_client.py` 与 `backend/config.py` 均为 `https://api.deepseek.com/v1` / `deepseek-chat`，也可用上述环境变量覆盖。遇到 `Bad Request`，先运行 `python -m document_agent.diagnose_model` 查看最小请求、普通调用、结构化调用的具体响应正文；该命令需要在已配置密钥的同一窗口运行。

## 运行控制台程序
在 `src` 目录下运行。采用**自然语言交互模式**：文件路径直接作为位置参数传入（终端支持按 `Tab` 键自动补齐），改写目标、改写模式、输出文件名等全部由你在提示符中输入的自然语言描述，意图识别节点（大模型）会自动分析：

```bash
cd src
python -m document_agent.console_app [候选文件列表...]
# 程序启动后提示：请输入你的需求（自然语言）：
```

### 常用运行示例

#### 1. 全新写作文档
- **无参考资料从零编写**：
  ```bash
  python -m document_agent.console_app
  # 提示符输入：写一份设备管理制度，包含采购、领用、报废三章
  ```
- **带参考资料全新撰写（路径可在终端按 Tab 键自动补齐）**：
  ```bash
  python -m document_agent.console_app D:/docs/巡检记录.pdf D:/docs/数据表.xlsx
  # 提示符输入：参考巡检记录写一份月度总结，另存为 月度总结.docx
  ```

#### 2. 改写已有文档（大模型自动识别改写模式）

把待修改的 `.docx` 作为候选文件传入，在自然语言中描述改写要求即可，模型自动在四种策略中选择：

```bash
# 自动识别为全局替换（replace）：自动提取替换对并走跨 Run 规则替换
python -m document_agent.console_app D:/docs/合同.docx
# 提示符输入：把这份合同中全文的"甲方"换成"委托方"，"2023年"换成"2026年"，另存为新文件

# 自动识别为局部微调（patch）：采用工具化按需探查（不把整篇文档塞进上下文）
python -m document_agent.console_app D:/docs/制度.docx
# 提示符输入：将这份制度第三章的考核指标改为每月一次，另存为新文件

# 自动识别为全局重塑（global）：按章节流式深度润色重写
python -m document_agent.console_app D:/docs/制度.docx
# 提示符输入：全面润色这篇文档，将口语化表述调整为严谨的企业公文规范，另存为 制度_润色版.docx

# 自动识别为格式规范化（format）：只改排版样式，原文文字一字不改
python -m document_agent.console_app D:/docs/粗糙草稿.docx
# 提示符输入：把这个文件的格式规范一下，生成一个新的文件
```

#### 3. 组合使用与非交互调用
- **改写并附带新参考文件**（多个候选文件时，模型自动区分"待修改目标文档"与"辅助参考资料"）：
  ```bash
  python -m document_agent.console_app D:/docs/制度.docx D:/docs/新标准.pdf
  # 提示符输入：参考新标准更新这份制度第二章的技术指标，另存为 制度_2026版.docx
  ```
- **非交互调用**（用管道喂入需求，适合脚本化测试）：
  ```bash
  echo 把这份合同中全文的"2025年"替换为"2026年"，另存为新文件 | python -m document_agent.console_app D:/docs/合同.docx
  ```

### 参数与行为说明
- `FILE...`：位置参数，0 到多个候选文件路径。参考素材支持 `.pdf`, `.docx`, `.xlsx`, `.txt`, `.md`, `.csv`, `.json` 等；**改写目标必须是 `.docx`**。路径不存在或类型不支持会在意图节点直接报错；若候选列表中仅有唯一一份 `.docx`，未明确指明文件名时会自动采用该文件作为改写目标；若候选列表中有多个 `.docx` 且无法从需求中匹配确定，会报错并提示指明具体文件名；
- **意图自动识别**：大模型从需求文本中判断任务类型（全新写作 / 改写）、改写**范围**（整篇 / 指定章节 / 指定元素区间，含正文、表格、页眉页脚等容器）与改写**动作**（`replace` 确定性换词、`edit` 局部增删改、`rewrite` 整段/整章重写、`format` 仅格式规范化），以及替换词对与保存文件名；
- **`replace` 支持范围与容器收窄**：可只替换指定章节或元素区间内的词；`containers` 决定本次改动哪些容器（正文、表格、页眉、页脚），整篇替换也可按用户列举收窄（如"范围包括正文、表格和页眉"则页脚不动）；页眉页脚不支持按章节或区间限定。换词走确定性替换，不会触发整段重写；旧模式名 `patch` / `global` 由兼容层映射为 `edit` / `rewrite`；
- **`format` 模式（格式规范化）**：只统一字体字号、标题层级、行距段距与对齐等**排版样式**，**原文文字一字不改**（图片、公式、域代码、超链接、合并单元格均原样保留）。它先按原文正文格式生成内置方案（同角色全文统一、标题字号形成梯度），再按你的显式要求（如"标题用黑体三号"）微调；写入前打印采用的格式方案，写入后自动做文字长度保全校验。注意：它不会凭空生成标题层级——若需要自动梳理章节标题（会改写文字），请使用 `rewrite`；
- **`rewrite` 模式（全局重塑）**：按章节**重新生成正文**，属于生成式改写，会改写文字与措辞（为全篇口径统一服务）；若只想规范排版、要求原文文字一字不改，请直接使用 `format` 模式（系统也会把"只改格式"的诉求识别为 `format`）；
- **结构化写作约束（可选，默认关闭）**：`new_doc_spec` 从用户原话提取总标题、正文长度与计数口径、表格策略、字体颜色作用域、必写主题、重复策略与文档形态；`new_doc_constraints` 提供生成后的全篇重复/长度/表格校验、修复与兜底。设置 `DOCUMENT_PLAN_MODE=on|shadow` 才启用（仅作用于无 Skill 的新建路径）；`NEW_DOC_GATE_MODE=report`（默认）只记录不阻断，`=block` 才不落盘；`DOCUMENT_VERIFIER_MODE` 控制独立事实审校。该能力在源分支最新自然五例仅 3/5、尚未收口，因此保持默认关闭，不改变现有生成行为；
- **保存行为**：默认一律**另存为新文件**（未指定文件名时智能拟定，兜底为 `<原文件名>_修改版.docx` 或 `data/新生成文档.docx`），不会动原稿；只有需求中明确说"直接在原文件上改 / 覆盖原文件"才会原地覆盖；
- **图数据库（本地检索模式依赖）**：本地模式下启动时读取 `src/setting.json`（键为 `gdb_uri`、`embedding_model`）并按其中的连接串连接 Memgraph；**文件不存在会打印"没找到配置文件"后退出，连接失败也会直接报错终止（没有降级）**。知识库检索（`knowledge` 工具）依赖该服务以及其中已录入的节点数据。若改用远程知识图谱（见下文“知识图谱模式配置”），则不再需要本地 Memgraph；
- 所有保存均采用"先写唯一临时文件再原子替换"，保障写盘安全，不会留下半截文档。

## Web 版（浏览器操作）

除控制台外，项目内置了一套 Web 界面（FastAPI 后端 + 原生 HTML/CSS/JS 前端，无需构建），
支持新建/切换会话、上传参考文件、实时查看生成过程、下载生成文档。

### 启动与停止（Windows PowerShell）

在项目根目录执行：

```powershell
# 先设置大模型密钥（只对当前窗口生效，新开窗口需重设）
$env:MODEL_API_KEY = "你的密钥"

.\start.cmd start          # 也可以直接双击 start.cmd
.\start.cmd status
.\start.cmd restart
.\start.cmd stop
```

`start.cmd` 只是 `start.ps1` 的包装器（用 Bypass 策略调用，避免 Windows 默认执行策略拦截）。
如果要在 PowerShell 里直接运行 `start.ps1`，需要用：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 start
```

> 注意：`start.ps1` 含中文提示，必须保存为 **UTF-8 with BOM**，否则 Windows PowerShell 5.1
> 会按 ANSI 解析导致乱码/语法错误（VS Code 保存时注意编码选择）。

`start.ps1` 参数：`-Port 9000`（临时覆盖端口）、`-BindHost 0.0.0.0`（临时覆盖监听地址）、
`-NoBrowser`、`-Python D:\Conda\envs\docx_agent\python.exe`。
不指定 `-BindHost` 时，监听地址按 `setting.json` 决定（见下文“远程访问配置”）。

### 启动与停止（Linux / macOS）

```bash
cd document_agent
export MODEL_API_KEY="你的密钥"

./start.sh start           # 首次若提示无执行权限：chmod +x start.sh，或使用 bash start.sh start
./start.sh status
./start.sh restart
./start.sh stop
```

`start.sh` 参数：`--port 9000`（临时覆盖端口）、`--host 0.0.0.0`（临时覆盖监听地址）、
`--no-browser`。不指定 `--host` 时，监听地址按 `setting.json` 决定。

两个脚本的行为一致：

- 后台启动服务，等待健康检查通过后再提示成功；
- 检测到桌面环境时，自动用默认浏览器打开本机地址（端口取 `setting.json` 的 `web_port`）
  （Windows 判断 `UserInteractive`，Linux/macOS 判断 `DISPLAY`/`WAYLAND_DISPLAY`，macOS 用 `open`）；
- 运行状态写入项目根目录的 `.run/`（`server.pid`、`server.log`、`server.err.log`）；
- `status` 退出码：0=运行中、1=未运行，便于脚本化判断。

`MODEL_API_KEY` 未设置时服务仍可启动，页面会给出提示，生成任务会直接失败并说明原因。

### 远程访问配置（src/setting.json）

默认情况下，是否允许其他主机访问由配置文件决定；命令行/脚本的 `-BindHost`（Windows）或
`--host`（Linux）可以临时覆盖，覆盖时以命令行参数为准：

```json
{
  "web_allow_remote": false,
  "web_port": 8000
}
```

- 优先级：命令行 `-BindHost` / `--host` > `web_allow_remote` 配置；
- `web_allow_remote: false`（默认）：监听 `127.0.0.1`，只有本机能访问；
- `web_allow_remote: true`：监听 `0.0.0.0`，局域网内其他主机可通过
  `http://<本机IP>:<web_port>/` 访问；
- 临时允许一次远程访问（不修改配置）：`.\start.ps1 start -BindHost 0.0.0.0`
  或 `./start.sh start --host 0.0.0.0`；下次不带参数启动仍按配置执行；
- `web_port`：默认端口；脚本的 `-Port` / `--port` 只临时覆盖端口；
- 修改配置后需要重启服务（`restart`）才生效；
- 启动日志、`start.ps1/start.sh status` 以及页面左下角都会显示当前访问方式及其来源
  （`setting.json 配置` 或 `命令行 -BindHost 覆盖`）；
- 服务本身没有登录鉴权，开启远程访问前请确认处于可信网络，或自行在反向代理层加认证。
  需要账号登录与按用户隔离数据时，请使用下文的**飞书版**。

## 飞书版（/feishu）

在本地 Web 版基础上新增的飞书桌面网页，入口：`http(s)://<主机>:<端口>/feishu`。
它与本地版共用同一个后端进程、知识库和智能体，但**登录与数据完全按飞书用户隔离**。

### 功能

- **飞书账号登录**：走飞书网页应用 OAuth 授权码流程，未登录时任何业务接口都返回 401；
- **显示用户名称**：登录后侧边栏底部显示飞书用户名与头像，并提供退出登录；
- **用户级隔离**：每位飞书用户有独立的会话列表、会话文件夹、生成文档和运行日志；
  接口从登录 Cookie 推导用户，不接受客户端传入的用户标识，其他用户即使拿到会话 ID 也无法访问；
- 新建会话、上传资料、生成/改写、实时日志、下载文档等操作与本地版一致。

### 配置（src/setting.json）

```json
"feishu": {
  "app_id": "cli_xxxxxxxx",
  "app_secret": "请填写飞书应用凭证",
  "accounts_base": "https://accounts.feishu.cn",
  "api_base": "https://open.feishu.cn",
  "redirect_uri": "",
  "base_url": "",
  "session_secret": "",
  "scope": ""
}
```

- `app_id` / `app_secret`：飞书开放平台「企业自建应用」的凭证；建议用环境变量
  `DOC_AGENT_FEISHU_APP_ID` / `DOC_AGENT_FEISHU_APP_SECRET` 提供，避免把密钥写进配置文件；
- `accounts_base`：飞书账号授权与令牌服务地址，默认 `https://accounts.feishu.cn`
  （Lark 国际版填 `https://accounts.larksuite.com`；环境变量 `DOC_AGENT_FEISHU_ACCOUNTS_BASE`）；
- `api_base`：OpenAPI 地址，用于读取用户信息，默认 `https://open.feishu.cn`
  （Lark 国际版填 `https://open.larksuite.com`；环境变量 `DOC_AGENT_FEISHU_API_BASE`）；
- `redirect_uri`：回调地址，留空时按当前访问地址自动推导（`/api/feishu/auth/callback`）；
  通过反向代理或域名访问时建议显式配置，且必须与飞书后台登记完全一致；
- `base_url`：服务对外根地址，用于自动推导回调；环境变量 `DOC_AGENT_FEISHU_BASE_URL`；
- `session_secret`：登录 Cookie 的签名密钥，留空时自动生成并保存在
  `workspace/feishu/session.secret`（环境变量 `DOC_AGENT_FEISHU_SECRET`）；
  更换密钥会使所有已登录用户失效。多进程/多 worker 部署时必须显式配置同一个密钥，
  否则各进程签发的 Cookie 互不认可；
- `scope`：需要用户额外授权的权限（空格分隔，环境变量 `DOC_AGENT_FEISHU_SCOPE`）。
  只读取姓名、头像和 open_id 时不需要填写；如需邮箱、user_id 等敏感字段再按需申请并配置。

### 飞书开放平台配置步骤

1. 在[飞书开放平台](https://open.feishu.cn/)创建「企业自建应用」；
2. 为应用添加「网页应用」能力，并在安全设置的重定向 URL 中登记
   `http(s)://<你的域名或IP>:<端口>/api/feishu/auth/callback`；
3. 发布应用版本并设置可用范围（只对指定部门/成员开放，这决定了谁能登录）。
   读取用户名称/头像/open_id 不需要额外 API 权限；如需邮箱、user_id 等字段，
   再到权限管理开通对应权限并写入 `scope`；
4. 把 `app_id` / `app_secret` 填入 `setting.json` 或环境变量，重启服务；
5. 浏览器访问 `http://<主机>:<端口>/feishu`，点击「使用飞书登录」完成授权。

> 若用局域网 IP 或域名访问，回调地址必须能从浏览器访问到，并且已在飞书后台登记；
> 否则飞书会拒绝授权。飞书开放平台通常要求重定向 URL 使用 **HTTPS**（本地 `localhost`
> 调试除外），局域网 IP + HTTP 往往无法登记；生产环境请使用 HTTPS 域名，
> 并配置 `base_url` / `redirect_uri`。HTTPS 反向代理场景还需正确传递
> `X-Forwarded-Proto`（本服务会据此设置 Cookie 的 `Secure` 属性）。

### 常见问题：授权页提示 20029（redirect_uri 请求不合法）

20029 是飞书对**重定向 URL 未登记或与请求不一致**的报错，不是 `app_secret` 错误
（应用凭证是否正确可以用 `app_access_token` 接口单独验证）。排查步骤：

1. 打开 `/feishu` 登录页，复制页面显示的「飞书回调地址」，例如
   `http://127.0.0.1:8001/api/feishu/auth/callback`；
2. 在飞书开发者后台进入**同一个 `app_id` 对应的应用** → 开发配置 → 安全设置 →
   **重定向 URL**，添加上一步的**完整地址**（协议、域名、端口、路径必须完全一致，
   不能只填域名）；
3. 保存后如有版本发布要求，请创建并发布新版本；确认 `app_id` 与登记重定向 URL 的是同一个应用；
4. 页面用 `localhost` 打开就登记 `localhost`，用 `127.0.0.1` 打开就登记 `127.0.0.1`，
   用局域网 IP 打开就登记该 IP（飞书通常要求非 localhost 使用 HTTPS）；
5. 反向代理/域名场景建议把 `feishu.redirect_uri` 固定为飞书后台登记的地址（或同时配置
   `base_url`），并始终用该地址打开页面，避免浏览器 Host 变化导致回调地址变化。

登录页会显示当前实际使用的回调地址，以及它来自配置还是按访问地址推导；
服务启动日志也会打印「飞书回调」提示。

### 安全与数据

- 服务端**不保存**飞书 `user_access_token`，只保存 HMAC-SHA256 签名的 open_id 会话 Cookie：
  `HttpOnly`、`SameSite=Lax`、默认 7 天有效，HTTPS 下自动加 `Secure`；
- 登录回调校验一次性 state Cookie，防止登录 CSRF；
- 用户数据目录：`workspace/feishu/users/<open_id>/`，其中 `profile.json` 保存名称/头像，
  `sessions/` 保存该用户的会话与文档；
- 每个请求都由服务端根据签名 Cookie 确定用户与目录，接口层没有“切换用户”的参数；
- 飞书应用可用范围之外的用户无法登录；服务本身不再提供第二套账号密码体系；
- 本地版 `/api` 与 `/` 仍保持无登录（用于单机/内网控制台）。如果服务暴露到公网，
  建议在反向代理层限制 `/api`、`/` 的来源，或只对外开放 `/feishu` 与 `/api/feishu`；
  飞书版数据本身始终按用户隔离。

### 飞书版接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/feishu/auth/status` | 登录状态、是否已配置飞书应用、当前用户 |
| `GET /api/feishu/auth/login` | 跳转飞书授权页 |
| `GET /api/feishu/auth/callback` | 飞书回调，校验 state 并写入登录 Cookie |
| `GET /api/feishu/auth/logout` | 退出登录 |
| `/api/feishu/sessions`、`/api/feishu/sessions/{id}/files`、`/api/feishu/sessions/{id}/messages`、`/api/feishu/jobs/{id}/events` | 与会话相关的业务接口，均要求登录且只作用于当前用户目录 |

### 知识图谱模式配置（src/setting.json）

检索阶段（`knowledge` 工具）可以走**远程知识图谱**（HTTP 检索协议）或**本地图数据库**
（Memgraph 篇章树 + 本地知识图谱），由配置文件的 `remote_kg.enabled` 决定：

```json
{
  "remote_kg": {
    "enabled": true,
    "base_url": "http://192.168.1.136:8000",
    "auth_token": "",
    "timeout": 60,
    "max_retries": 2
  }
}
```

- `enabled: true`：图中的 `retrieve_remote_kg` 节点取代 `retrieve_gdb`，启动时**不再连接本地
  Memgraph**（`gdb_uri` 等配置可以留空），检索改走远程知识图谱的只读接口
  （协议见 `src/document_agent/retrieve_tool/retrieve_protocol.md`）；
- `enabled: false`：保持原有本地图数据库检索流程，其余行为不变；
- `base_url`：远程知识图谱地址，末尾斜杠会自动去掉；
- `auth_token`：远程服务配置了 `api.auth_token` 时填写，也可以改用环境变量
  `REMOTE_KG_API_TOKEN` / `API_TOKEN`；
- 临时切换（不改文件）：`DOC_AGENT_REMOTE_KG=1/0` 切换模式、`DOC_AGENT_REMOTE_KG_URL=...`
  改地址、`DOC_AGENT_REMOTE_KG_TIMEOUT=...` 改超时；
- 兼容扁平写法：`remote_kg_enabled` / `remote_kg_url` / `remote_kg_token` / `remote_kg_timeout`；
- 启动日志、`/api/health`（`knowledge_mode` 与 `knowledge.remote_kg`）以及页面左下角的
  「远程知识图谱 / 本地知识库」徽标都会显示当前模式；远程模式还会显示**连接是否正常**、
  延迟与图谱规模（文档/实体数量）；
- 远程模式由后端周期性探测（默认 20 秒，可用 `DOC_AGENT_REMOTE_KG_PROBE_SECONDS` 调整）。
  连不上时生成任务会直接报错并说明原因，不会静默降级到本地；
- **引用规范**：写作时引用知识图谱里的文档，必须使用**完整文档名称（含扩展名）**。
  检索结果会给出「可引用文档」清单；若模型写成了简称，保存前会按清单自动补全为完整名称
  （同名文档无法判断时保持原样）。

### 界面功能

- **新建会话**：后端自动创建 `src/workspace/sessions/<会话ID>/` 文件夹；
- **切换会话**：左侧列表选择历史会话，文件与消息记录从磁盘恢复，可继续之前的工作；
- **上传文件**：拖拽或点击上传，支持 `.docx/.pdf/.xlsx/.doc/.xls/.txt/.md/.csv/.json`，
  既可作为生成参考，也可以作为改写目标（改写目标必须是 `.docx`）；
- **生成文档**：输入自然语言需求（全新撰写或改写），右侧实时显示智能体运行日志；
- **下载文档**：生成结果自动保存到会话文件夹，文件列表与消息卡片都能下载，也可直接在文件夹中取用。

### 工作区、接口与注意事项

- 会话目录：`src/workspace/sessions/<会话ID>/`，文件直接放在会话目录下，
  `.meta/` 子目录保存标题、消息与每次任务的运行日志；可通过 `DOC_AGENT_WORKSPACE` 改工作区位置。
- 监听地址与端口默认由 `setting.json` 的 `web_allow_remote` / `web_port` 决定，
  可用 `-BindHost` / `--host`、`-Port` / `--port` 临时覆盖（见上节）。
- 服务启动后访问 `http://127.0.0.1:<web_port>/docs` 可查看完整的 OpenAPI 接口文档。
- 主要接口：`/api/sessions`（会话）、`/api/sessions/{id}/files`（文件）、
  `/api/sessions/{id}/messages`（提交需求）、`/api/jobs/{id}/events`（SSE 实时进度）；
  `/api/health` 会返回 `remote_access`、`bind_host`、`host_source`、`remote_urls` 等监听信息。
- 每个会话独立上下文；同一会话同一时间只允许一个生成任务（并发提交会返回 409）。
- 多轮对话记忆保存在服务进程内；服务重启后界面仍会显示历史消息，可继续基于会话文件夹中的文件发起新需求。
- 也可直接启动：`cd src && python -m document_agent.backend`（默认按 `setting.json` 监听）；
  `--host` / `--port` 临时覆盖监听地址与端口，`--show-config` / `--print-endpoint` 用于查看最终监听配置。

## 工作流

文档写作时：

`intent`（参数校验、任务识别与规范选择）→ `skill`（按选定规范准备检索，未匹配时放行）→ `summary`（参考文件摘要）
→ `retrieve`（检索相关素材）→ 按意图分流：
- `new_docx`：规划章节并逐章生成新文档
- `rewrite_docx`：解析原文档结构，由模型给出"逐元素修改方案"并改写文档结构（保留未改动部分的原始格式、表格与页面设置），默认另存为新文件
→ `verify`（按 Skill 验收，未配置 Skill 时直接放行）

创建/修改/删除/发布 Skill 不再进入写作图；主入口会拦截管理诉求并提示使用 `/admin` 或管理 CLI。

使用以下命令将文档录入知识库中（需先启动本地 Memgraph 图数据库，连接配置见 `src/setting.json`）：
```bash
python -m document_agent.knowledge_base_record <file1> <file2> ...
```

## 文档 Skill

Skill 是一类文档的声明式写作规范：结构、检索、证据与验收配置。系统把 Skill 分成两个入口：

- **主入口（写作）**：只读已发布 Skill，负责自动匹配、加载、执行与验收；不提供创建/修改/删除/发布。
- **管理入口（管理）**：结构化 CRUD、启停、校验、发布、回滚、附件与审计；支持自然语言创建/修改，但仅本机可用。

### 主入口：用 Skill 写作

使用规范写文档有三个入口：

| 入口 | 用法 |
| --- | --- |
| 控制台自然语言 | `python -m document_agent.console_app D:/docs/参考资料.txt`，在提示符里描述需求，由 `intent` 自动匹配已启用的规范 |
| Web 自然语言 | 在会话中发送需求，同样自动匹配 |
| 命令行 | `python -m document_agent.skill_cli --skill <id/名称/目录/配置文件> --request "需求" [文件...]`，支持显式指定、非交互、临时配置文件 |

主入口只匹配 **enabled + published** 的 Skill。草稿编辑不会影响生产；发布后才生效。
没有 `.state` 的旧 Skill 视为已发布，行为与改造前一致。

```bash
# 控制台 / Web：自然语言自动匹配
python -m document_agent.console_app D:/docs/参考资料.txt
# 提示符输入：按文档规范，根据附件写一份报告，另存为 报告.docx

# 命令行：显式指定 + 非交互 + 结构化报告 + 验收门禁
python -m document_agent.skill_cli --skill my_standard --request "按规范写一份报告" D:/docs/参考资料.txt
python -m document_agent.skill_cli --skill D:/tmp/临时规范.yaml --request-file req.txt --output 报告.docx --report-json report.json --strict-verification
python -m document_agent.skill_cli --list-skills
```

命令行入口的 `--skill` 可直接传目录或配置文件路径，用未安装到规则目录的临时规范；自然语言入口只能匹配已安装且已发布的规范。

### 管理入口：管理 Skill

管理入口有两种等价用法：

- HTTP：`/admin/api/*`，仅接受本机来源；支持结构化 JSON 与自然语言创建/修改。
- CLI：`python -m document_agent.skill_cli skill ...`，或 `python -m document_agent.skills.admin ...`。

常用命令：

```bash
python -m document_agent.skill_cli skill list
python -m document_agent.skill_cli skill new my_standard --name 文档规范
python -m document_agent.skill_cli skill show my_standard
python -m document_agent.skill_cli skill validate my_standard
python -m document_agent.skill_cli skill update my_standard --patch '{"name":"新名称"}'
python -m document_agent.skill_cli skill draft "创建项目周报规范，包含进展、风险和下周计划" --id project_weekly
python -m document_agent.skill_cli skill draft "把行文风格改简洁，保留章节和检索要求" --id project_weekly --update
python -m document_agent.skill_cli skill publish project_weekly
python -m document_agent.skill_cli skill versions project_weekly
python -m document_agent.skill_cli skill rollback project_weekly 1
python -m document_agent.skill_cli skill disable project_weekly
python -m document_agent.skill_cli skill enable project_weekly
python -m document_agent.skill_cli skill delete project_weekly
python -m document_agent.skill_cli skill audit
```

- `new` 创建 Skill 并立即发布，保持旧行为；管理 API 创建默认是草稿，需要显式发布。
- `draft` 用自然语言创建；加 `--update` 用自然语言修改已有 Skill；生成后仍是草稿，发布后生效。
- `update` 接受结构化 JSON 补丁；`draft` 接受自然语言，两者都只在管理入口可用。
- `delete` 默认软删除到 `.trash/`；彻底删除需要 `--purge --confirm <id>`。

HTTP 示例：

```bash
curl http://127.0.0.1:8000/admin/api/skills
curl -X POST http://127.0.0.1:8000/admin/api/skills \
  -H "Content-Type: application/json" \
  -d '{"config": {"id": "my_standard", "name": "文档规范"}, "publish": true}'

# 自然语言创建草稿
curl -X POST http://127.0.0.1:8000/admin/api/skills/draft \
  -H "Content-Type: application/json" \
  -d '{"description": "创建项目周报规范，包含进展、风险和下周计划", "id": "project_weekly"}'

# 自然语言修改草稿
curl -X POST http://127.0.0.1:8000/admin/api/skills/project_weekly/draft \
  -H "Content-Type: application/json" \
  -d '{"description": "把行文风格改简洁，保留章节和检索要求", "version": 3}'
```

管理入口只接受 `127.0.0.1`/`::1`；跨入口访问统一返回 403。管理写操作会记录到 `.audit/`。

### 发布态与数据布局

```
<skills_dir>/
├── <id>/                      # 活动目录（当前可编辑版本）
├── <id>.json|yaml             # 单文件配置
├── .state/<id>.json           # 发布态：draft / published
├── .releases/<id>/v<n>/       # 发布快照，运行时只读这里
├── .history/<id>_<ts>_<rand>/ # 修改前备份
├── .audit/audit-YYYYMM.jsonl  # 管理操作审计
└── .trash/<id>_<ts>/          # 软删除
```

- 修改 Skill 会递增版本号，并把改动前的文件备份到 `.history/`（每本保留最近 20 份）。
- 发布时把活动目录复制为 `.releases/<id>/v<n>/`，主入口只读该快照。
- 没有 `.state` 的旧 Skill 视为已发布；没有 `.releases` 时运行时回退到活动目录。
- 删除默认移到 `.trash/`；`--purge` 才彻底删除，并清理发布态与快照。

### 执行与边界

- `intent` 只区分 `new`（新写文档）和 `rewrite`（改写已有文档），并根据可用 Skill 选择规范。
- 创建、修改、删除、发布 Skill 属于管理诉求：主入口会确定性拦截并返回
  `MAIN_ENTRY_MANAGEMENT_FORBIDDEN`，不生成文档、不写 Skill 文件。
- `skill` 节点校验来源能力与检索计划，`verify` 在保存后输出验收结果。
- `skill_cli` 中仅允许文件来源的 Skill 会跳过知识库初始化；知识库任务仍使用已有本地或远程配置。
  `console_app` 保持 develop 原版行为，始终按 `setting.json` 初始化知识库。
- 章节、来源白名单、检索次数及篇幅由程序约束；业务事实与计算仍需模型审核或人工核对，引用存在不等于事实正确。
- 来源限制请用"仅检索用户附件""只使用知识库"这类明确说法；系统会打印识别到的单来源要求供核对，
  复杂或未识别的表述仍需人工确认。`skill.yaml` 正文里的同类措辞也会触发一致性校验。
- `skill_cli` 的 `--report-json <路径>` 可导出结果；退出码 `0` 表示成功、`1` 表示解析或执行失败、
  使用 `--strict-verification` 后验收未通过或未完成返回 `2`。`console_app` 的退出码保持 develop 原样。

### 配置字段速查

`skill.yaml` 采用单一配置文件，字段分组如下（未知键会被拒绝，`skill validate <id>` 可校验）：

| 分组 | 主要字段 |
| --- | --- |
| 基本信息 | `id`（小写字母/数字/下划线/短横线）、`name`、`version`、`description`、`enabled`、`created_by`、`created_at`、`updated_at` |
| 写作规范 `task` | `doc_type`（new/rewrite/any）、`purpose`、`audience`、`tone`、`professionalism`、`language`、`triggers`、`terminology`、`banned_expressions`、`extra_requirements`、`length_target` |
| 文档结构 `structure` | `order`、`numbering`、`output_format`、`format`（字体/字号/行距/缩进）、`sections`（`title` 必填，另有 `level`、`required`、`must_include`、`requirements`、`data_fields`、`min_body_chars`、`evidence_types`、`evidence_required`、`min_citations`、`guidance`） |
| 检索目标 `research` | `questions`、`data_fields`、`numeric_fields`、`field_aliases`、`time_range`、`region`、`credibility_rule` |
| 检索策略 `research` | `source.priority`（仅 `file`/`knowledge`）、`knowledge`、`file`、`max_rounds`、`per_question_attempts`、`query_expansion`、`on_empty`、`stop_when`、`stop_after_no_new_evidence` |
| 证据要求 `evidence` | `citation_template`（须含 `{name}`）、`min_sources_per_key_claim`、`cross_validation`、`conflict_policy`、`derived_data` |
| 验收规则 `verification` | `checks`、`semantic_review`、`max_section_attempts`、`on_failure`（当前固定 `report`） |
| 推荐默认值 | `recommendations`：字符串数组，最多 30 条、每条不超过 300 字 |

`recommendations` 是**建议**而非硬性要求：只在用户和规范都没指定该项时作为默认值，不计入验收，
未达到不判错，也不得为此编造数据。只有用户明确要求时才把建议升为硬性约束；修改规范时保留未涉及的建议。
`verification.checks` 中若写了 `terminology`，程序化验收会明确标记"跳过（由语义评估处理）"，
不会静默忽略；未知检查项会在报告中标为未通过。

### 备份、恢复与版本兼容

- 每次修改规范会递增版本号，并把改动前的 `skill.yaml`、`SKILL.md` 整份备份到规则目录的
  `.history/<id>_<时间戳>_<随机串>/`。每本规范只保留最近 20 份备份。
- 发布快照保存在 `.releases/<id>/v<n>/`；`rollback` 会先把当前活动目录备份到 `.history/`，
  再用快照覆盖活动目录，并更新 `.state` 的 `published_version`。
- `SKILL.md` 开头的 frontmatter 中除 `id/name/version/description/enabled` 外的自定义键会原样保留。
- 空的 `recommendations` 不会写进配置文件，因此普通规则包可被旧版本程序读取；
  **若填写了 `recommendations`，旧版本不认识该字段会拒绝加载**，混合部署时需统一版本。
- `src/merge.py` 与 `src/setting_example.json` 保持 develop 原版：前者可把示例配置合并进
  `src/setting.json`（缺失键补齐、冲突保留原值、支持 `--dry-run`），后者是配置模板。
  两个文件均保留；`src/setting.json` 含本地地址与口令，已被 `.gitignore` 忽略，不要提交。

## 知识图谱记忆（`document_agent.memory.knowledge_graph`）

`KnowledgeGraphMemory` 是一个基于 **Neo4j** 的知识图谱类，对外提供两个接口：

| 接口 | 作用 |
| --- | --- |
| `add_text(text, source=..., metadata=...)` | 输入字符串：调用大模型抽取实体与关系，去重合并后写入知识图谱 |
| `query(prompt, top_k=..., depth=...)` | 输入提示词：向量检索相关实体 + 邻居关系 + 原文片段，交给大模型归纳作答 |

### 写入与查询原理

- **实体去重**：先按名称精确匹配，再用向量相似度（余弦 ≥ 0.95）合并同义表述，例如"示例科技"会并入"示例科技有限公司"；
- **图谱结构**：`(:KGEntity)` 实体、`(:KGDocument)` 原始文档、`(:KGEntity)-[:相关关系]->(:KGEntity)`、`(:KGDocument)-[:MENTIONS]->(:KGEntity)`；
- **检索**：使用 Neo4j 原生向量索引（`db.index.vector.queryNodes`）；索引不可用时自动退化为本地余弦计算，功能不受影响；
- **降级**：大模型不可用时仍可写入（退化为规则抽取），此时 `query` 返回图谱上下文原文；设 `require_llm: true` 可改为直接报错。

### 用法

在 `src` 目录下运行（与 `console_app` 一致）：

```python
from document_agent.memory.knowledge_graph import KnowledgeGraphMemory

kg = KnowledgeGraphMemory()                       # 自动读取 src/setting.json
kg.add_text("张三于 2024 年加入示例科技有限公司，担任技术总监。", source="员工手册.docx")
print(kg.query("张三在哪个公司担任什么职务？"))
print(kg.stats())                                 # 实体/文档/关系数量
kg.close()
```

### 配置项（`src/setting.json`）

| 键 | 说明 |
| --- | --- |
| `neo4j_uri` | Neo4j 连接串，如 `bolt://localhost:7687`（也兼容原有的 `gdb_uri`） |
| `neo4j_user` / `neo4j_password` | 登录账号；**密码留空表示无鉴权连接** |
| `neo4j_database` | 数据库名，默认 `neo4j` |
| `embedding_model` | 句向量模型名或**本地模型目录的绝对路径** |
| `hf_endpoint` | HuggingFace 镜像，默认 `https://hf-mirror.com`；置空则用官方站 |
| `hf_home` | 模型缓存目录，默认项目内 `.models/` |
| `entity_vector_index` / `document_vector_index` | 向量索引名 |
| `top_k` / `neighbor_limit` | 查询召回实体数 / 邻居关系上限 |
| `enable_vector_index` | 是否使用 Neo4j 向量索引（关掉则本地计算） |
| `require_llm` | 缺少大模型配置时是否直接报错（`false` 则降级运行） |

模型、账号等也可用环境变量覆盖：`NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD`、`NEO4J_DATABASE`。

如果要用远程知识图谱，则需要加入以下配置：
    "remote_kg": {
        "enabled": true,
        "base_url": "http://127.0.0.1:8000",
        "auth_token": "",
        "timeout": 60,
        "max_retries": 2
    }

### 依赖与模型

该模块需要 `neo4j`、`sentence-transformers`（已装入 conda 环境 `llm`）：

```bash
C:\Users\wangh\anaconda3\envs\llm\python.exe -m pip install "neo4j==6.3.0" sentence-transformers
```

默认句向量模型 `all-MiniLM-L6-v2`（384 维）已下载到 `.models/all-MiniLM-L6-v2`（已加入 `.gitignore`），
代码会优先加载该本地目录，因此**离线也能直接使用**；若换用别的模型名，会在首次使用时联网下载。

