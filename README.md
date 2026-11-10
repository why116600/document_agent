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
创建或修改可复用规范时：`intent` → `manage_skill`（生成配置或局部补丁、校验并保存）→ 结束。
文档写作时：

`intent`（参数校验、任务识别与规范选择）→ `skill`（按选定规范准备检索，未匹配时放行）→ `summary`（参考文件摘要）
→ `retrieve`（检索相关素材）→ 按意图分流：
- `new_docx`：规划章节并逐章生成新文档
- `rewrite_docx`：解析原文档结构，由模型给出"逐元素修改方案"并改写文档结构（保留未改动部分的原始格式、表格与页面设置），默认另存为新文件
→ `verify`（按 Skill 验收，未配置 Skill 时直接放行）

使用以下命令将文档录入知识库中（需先启动本地 Memgraph 图数据库，连接配置见 `src/setting.json`）：
```bash
python -m document_agent.knowledge_base_record <file1> <file2> ...
```

## 文档 Skill

用户可以手工编写 Skill，也可以用自然语言描述要求，由模型生成一类文档的写作规范、检索问题、来源限制及验收配置。
保存规则后，系统在写作任务中从用户自然语言选择适用的已启用规范，用户不必指定 `--skill`。
普通控制台与 Web 对话均支持直接创建和修改规范，例如：

- “创建项目周报规范：包含进展、风险和下周计划，仅检索用户附件，关键结论注明来源。id 为 project_weekly。”
- “修改 project_weekly 规范，将行文风格改为简洁，其他要求保持不变。”

创建和修改需要模型；保存前校验配置，创建时不覆盖已有规则，修改时保留未涉及的字段、参考附件和模板，
版本号递增，原文件备份到规则目录的 `.history/`。规范保存在共享规则目录，后续会话可使用；未明确修改对象时要求补充名称或 id。

结构化约束字段表示必须满足的要求；模型按经验补充的篇幅、指标等放在 `recommendations` 字符串数组中，
作为该项未明确指定时的推荐默认值；用户有明确要求时以明确要求为准，未达到推荐值不影响验收。
只有用户明确要求时才将推荐值升为硬性约束；修改时保留未涉及的要求和推荐默认值。
旧 Skill 保持原有约束，不自动降级。术语写法由语义评估核对，不要求所有术语都出现在正文中。

### 配置与管理

规则目录由 `DOCUMENT_SKILLS_DIR` 指定；未设置时依次查找当前目录的 `skills/`、
`src/skills/` 和源码旁的 `src/skills/`。具体规则由用户创建，不随核心代码预置。

每个规则包包含 `skill.yaml`（结构、检索和验收配置）与 `SKILL.md`（写作指令），
可选添加 `references/` 参考规范和 `assets/template.docx` 文档模板；也支持单文件 JSON/YAML 配置。

管理规范有三个入口：控制台自然语言、Web 自然语言、命令行。命令行入口是独立模块
`document_agent.skill_cli`（`console_app.py` 保持 develop 原版，不带参数解析），也可直接用
`document_agent.skills.commands`。在 `src` 目录执行：

```bash
python -m document_agent.skill_cli skill new my_standard --name 文档规范
# 编辑生成的 skill.yaml 和 SKILL.md，定义适用场景、章节、检索问题与来源
python -m document_agent.skill_cli skill validate my_standard
python -m document_agent.skill_cli skill list

# 或用自然语言生成规则包（需要模型配置），生成后可继续编辑和校验
python -m document_agent.skill_cli skill draft "定义项目周报规范：包含进展、风险、下周计划；仅检索用户附件中的进度、问题和负责人，关键结论注明来源" --id project_weekly
python -m document_agent.skill_cli skill validate project_weekly

# 等价的独立管理入口
python -m document_agent.skills.commands list
```

其他管理操作可通过 `python -m document_agent.skill_cli skill --help` 查看。
`new` 创建可编辑的规则模板，`show/list/validate` 查看、列举和校验规则，均无需连接模型或数据库；
`draft` 及普通对话中的自然语言创建、修改需要模型。
控制台（`python -m document_agent.console_app`）与 Web 对话里直接说「创建一份……规范」「修改 project_weekly 规范」，
由 `intent` 识别为 `skill_create` / `skill_update` 后进入 `manage_skill` 节点保存。

### 使用方式

使用规范写文档同样有三个入口：

| 入口 | 用法 |
| --- | --- |
| 控制台自然语言 | `python -m document_agent.console_app D:/docs/参考资料.txt`，在提示符里描述需求，由 `intent` 自动匹配已启用的规范 |
| Web 自然语言 | 在会话中发送需求，同样自动匹配 |
| 命令行 | `python -m document_agent.skill_cli --skill <id/名称/目录/配置文件> --request "需求" [文件...]`，支持显式指定、非交互、临时配置文件 |

系统结合文档类型、规范用途、名称和触发词匹配；明确要求不使用规范时不启用，没有适用项时按普通流程执行，
多个候选无法区分时默认不启用规范并说明原因；只有明确要求"必须使用规范"却找不到唯一一本时才会报错。

```bash
# 控制台 / Web：自然语言自动匹配
python -m document_agent.console_app D:/docs/参考资料.txt
# 提示符输入：按文档规范，根据附件写一份报告，另存为 报告.docx

# 命令行：显式指定 + 非交互 + 结构化报告 + 验收门禁
python -m document_agent.skill_cli --skill my_standard --request "按规范写一份报告" D:/docs/参考资料.txt
python -m document_agent.skill_cli --skill D:/tmp/临时规范.yaml --request-file req.txt --output 报告.docx --report-json report.json --strict-verification
python -m document_agent.skill_cli --list-skills
```

命令行入口的 `--skill` 可直接传目录或配置文件路径，用未安装到规则目录的临时规范；自然语言入口只能匹配已安装的规范。

### 执行与边界

- `intent` 分析任务并确定规范，`skill` 校验来源能力和检索计划，`verify` 在保存后输出验收结果。
- 意图模型区分四类任务：`new`（新写文档）、`rewrite`（改写已有文档）、`skill_create`（建立规范）、`skill_update`（修改规范）。
  "按规范写报告"属于 `new`，"按规范修改这份文档"属于 `rewrite`，都不会改动规范本身。
- 修改规范时需给出规范的 id 或完整名称（`skill_target`），定位不唯一或找不到时会要求补充说明；
  命令行的 `--skill` 也可用于显式指定本次任务要使用的规范。
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
  `.history/<id>_<时间戳>_<随机串>/`。需要回退时，把该目录里的文件复制回规则包即可。
  每本规范只保留最近 20 份备份。
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

