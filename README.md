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
set MODEL_BASE_URL=https://api.deepseek.com
set MODEL_NAME=deepseek-flash
```

2026-11-08 已按[官方更新记录](https://api-docs.deepseek.com/updates/)修正默认模型名。`deepseek-chat` / `deepseek-reasoner` 是已停用的旧名称；当前默认 `deepseek-flash`。官方接口采用非思考模式以适配本工作流的工具调用。遇到 `Bad Request`，先运行 `python -m document_agent.diagnose_model` 查看最小请求、普通调用、结构化调用的具体响应正文；该命令需要在已配置密钥的同一窗口运行。

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
- **意图自动识别**：大模型从需求文本中判断任务类型（全新写作 / 改写）、改写模式（`patch` 局部微调、`replace` 全局替换、`global` 全局重构润色、`format` 仅格式规范化）、替换词对与保存文件名；
- **`format` 模式（格式规范化）**：只统一字体字号、标题层级、行距段距与对齐等**排版样式**，**原文文字一字不改**（图片、公式、域代码、超链接、合并单元格均原样保留）。它先按原文正文格式生成内置方案（同角色全文统一、标题字号形成梯度），再按你的显式要求（如"标题用黑体三号"）微调；写入前打印采用的格式方案，写入后自动做文字长度保全校验。注意：它不会凭空生成标题层级——若需要自动梳理章节标题（会改写文字），请使用 `global`；
- **`global` 模式（全局重塑）**：按章节**重新生成正文**，属于生成式改写，会改写文字与措辞（为全篇口径统一服务）；若只想规范排版、要求原文文字一字不改，请直接使用 `format` 模式（系统也会把"只改格式"的诉求识别为 `format`）；
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
`intent`（参数校验与初始化）→ `skill`（加载用户自定义 Skill，未指定时直接放行）→ `summary`（参考文件摘要）
→ `retrieve`（检索相关素材）→ 按意图分流：
- `new_docx`：规划章节并逐章生成新文档
- `rewrite_docx`：解析原文档结构，由模型给出"逐元素修改方案"并改写文档结构（保留未改动部分的原始格式、表格与页面设置），默认另存为新文件
→ `verify`（按 Skill 验收，未配置 Skill 时直接放行）

使用以下命令将文档录入知识库中（需先启动本地 Memgraph 图数据库，连接配置见 `src/setting.json`）：
```bash
python -m document_agent.knowledge_base_record <file1> <file2> ...
```

## 用户自定义文档 Skill

Skill 是一份声明式配置，用来描述一类文档的写作规范、检索目标与来源策略、证据要求和验收条件。
启用后流程中会多出两个节点：`skill`（匹配、校验、能力预检与检索计划）与 `verify`（程序化验收 + 语义补充评估）。
**未指定 Skill 时，行为与原来完全一致。**

五类东西的职责边界：

| 概念 | 在本系统中的载体 | 作用 |
| --- | --- | --- |
| Skill 指令 | `SKILL.md` 正文 | 告诉模型「应该怎样完成任务」，注入写作/改写提示词 |
| Skill 配置 | `skill.yaml` | 结构化表达写作、检索与验收规则，加载时校验、运行时驱动编排 |
| 检索工具 | `retrieve_file` / `retrieve_gdb` 节点 | 实际读取文件与知识库，返回证据与元数据 |
| 工作流编排 | LangGraph 节点与 `retrieval_plan` | 决定匹配、预检、检索顺序、重试、缺失报告与异常处理 |
| 验证器 | `verify_node` | 执行程序化检查，并调用模型做语义补充评估 |

### 两种组织方式

推荐使用 **Skill 包**（`src/skills/industry_report/` 即完整示例）：

```
skills/<id>/
├── SKILL.md          # 必需：名称、用途、适用条件、执行流程、主要指令
├── skill.yaml        # 必需：A~E 结构化配置（也支持 skill.yml / skill.json）
├── references/       # 可选：术语表、详细规范、示例（.md/.txt/.markdown）
└── assets/           # 可选：template.docx 作为新文档的版式基底
```

也兼容单文件 `skills/<id>.json|.yaml`，适合最小化试用。

| 文件 | 是否必需 | 作用 | 是否影响实际执行 |
| --- | --- | --- | --- |
| `SKILL.md` | 包模式必需 | 人类可读的执行流程与主要指令；可选 YAML frontmatter（`id/name/version/description`，与 `skill.yaml` 冲突会报错） | 正文会作为【Skill 执行说明】注入写作与改写提示词 |
| `skill.yaml` | 必需 | 机器可校验的结构化配置，加载时由 pydantic 校验 | 是，见下方映射表 |
| `references/` | 可选 | 详细规范、术语表、示例；自动读取 `.md/.txt`，单文件最多 6000 字、总量最多 12000 字、最多 20 个 | 会作为【参考规范与术语】注入写作与改写提示词 |
| `assets/template.docx` | 可选 | 新文档的版式基底（封面、页眉、样式） | 新建文档时以该文件为基底，再追加生成内容 |

**为什么没有拆成 `retrieval.yaml` + `writing.yaml`**：现有执行链路一次性加载一个经过 pydantic 校验的配置对象，
拆分会让跨文件的 id/版本/引用模板一致性需要额外校验，收益低于成本。检索相关字段已经集中在
`research` / `evidence` 两节，语义边界清晰，因此采用单一 `skill.yaml`。

### 结构化配置字段

| 分组 | 字段 | 说明 |
| --- | --- | --- |
| A 基本信息 | `id` / `name` / `version` / `description` | 唯一标识、展示名、版本、适用场景；`id` 只能是小写字母、数字、下划线与短横线 |
| A 基本信息 | `enabled` / `created_by` / `created_at` / `updated_at` | 启用状态与审计信息；禁用的 Skill 不会被列出或自动匹配 |
| A 基本信息 | `task.doc_type` / `task.triggers` | 适用任务（`new`/`rewrite`/`any`）与触发词（意图识别时展示给模型） |
| B 写作规范 | `task.purpose` / `audience` / `tone` / `professionalism` / `language` | 文档目标、读者、语气、专业程度、语言 |
| B 写作规范 | `task.length_target` / `task.terminology` / `task.banned_expressions` / `task.extra_requirements` | 篇幅、统一术语写法、禁用表达、其它约束 |
| B 写作规范 | `structure.order` / `numbering` / `output_format` | 章节顺序、编号体例、段落/表格等输出格式 |
| B 写作规范 | `structure.sections` | 章节：`title`、`level`(1~4)、`required`、`must_include`（字面关键词）、`requirements`（自然语言语义要求）、`data_fields`、`min_body_chars`、`evidence_types`、`evidence_required`、`min_citations`、`guidance` |
| B 写作规范 | `structure.format` | 正文字体/字号、标题字体/各级字号、加粗、行距、首行缩进 |
| C 检索目标 | `research.questions` / `data_fields` | 必须回答的问题、必须收集的数据字段 |
| C 检索目标 | `research.field_aliases` | 字段的等价表达；用于字段覆盖检查，不改变指标含义 |
| C 检索目标 | `research.time_range` / `region` | 时间范围、地域范围（仅提示词约束） |
| D 检索策略 | `research.source.priority` | 允许使用的来源与优先级，仅支持 `knowledge`（知识库）与 `file`（用户文件） |
| D 检索策略 | `research.knowledge` | 知识库召回数 `top_k`、最低相似度 `min_similarity`、是否展开邻居、展开轮次 `max_expansions` |
| D 检索策略 | `research.file.max_chars` | 单个文件送入模型的最大字符数，0 表示不限制 |
| D 检索策略 | `research.max_rounds` / `per_question_attempts` | 全局轮次上限 / 单个研究问题最多尝试几次 |
| D 检索策略 | `research.query_expansion` / `on_empty` / `stop_when` | 查询改写、空结果处理、提前结束条件 |
| D 检索策略 | `research.credibility_rule` | 来源可信度与时效性要求（仅提示词与语义评估） |
| D 检索策略 | `evidence.citation_template` / `min_sources_per_key_claim` / `cross_validation` / `conflict_policy` | 引用格式、来源数量、交叉验证与冲突处理 |
| D 检索策略 | `evidence.derived_data` | `label` 要求推算注明公式与输入来源；`forbid` 禁止推算。属于提示词/语义要求，非通用算术校验 |
| E 验收规则 | `verification.checks` / `semantic_review` / `on_failure` | 程序化验收项列表、是否做语义补充评估；`on_failure` 当前固定为 `report` |
| E 验收规则 | `verification.max_section_attempts` | 新建文档每章最多生成次数（1~3，默认3）；正文、关键词、引用或总篇幅预算未通过则反馈重试，用尽次数不保存文档 |

### 配置与真实执行能力的对应

只有下表中的「硬约束」会被代码强制执行；标为「提示词」的规则会写进模型提示词，但无法保证 100% 遵守。

| 配置项 | 实际执行效果 | 类型 |
| --- | --- | --- |
| `structure.sections` / `order` | `new_docx` 直接按清单逐章生成，不再让模型自由规划；`verify` 校验标题与顺序 | 硬约束 |
| `structure.format` | 设为 Normal 样式基础字体/字号/行距/缩进，并把格式要求写入提示词；验收不检查字体 | 半硬约束 |
| `research.source.priority` | `retrieve` 只开放列出的工具，越权调用会被拒绝并反馈给模型 | 硬约束 |
| `research.max_rounds` | 覆盖默认检索轮次预算（默认 3 次） | 硬约束 |
| `research.knowledge.*` | 直接传给 `search_by_vector(top_k)`，按 `min_similarity` 过滤，按 id 去重，控制是否展开邻居与展开轮次 | 硬约束 |
| `research.file.max_chars` | 文件内容超长时截断后再送入模型 | 硬约束 |
| `research.questions` / `data_fields` / `numeric_fields` | 按问题检索；验收排除缺失说明与空字段；numeric_fields 要求有数值 | 程序调度 + 基础验收，准确性另需审核 |
| `structure.sections[].data_fields` / `requirements` | 章节字段写入写作提示词，章节语义要求由审核模型结合证据评估；不靠正则从原文猜测字段事实 | 提示词 / 语义评估 |
| `query_expansion` / `on_empty` | 控制是否改写、空结果时换源或标记缺失；跳过同题相同来源/查询的重复调用 | 程序控制，查询改写由模型完成 |
| `stop_after_no_new_evidence` / `stop_when` | 前者为程序执行的连续空结果阈值，后者为每轮模型判断的自然语言停止条件；评估失败继续按预算检索并记录 | 程序控制 / 模型判断 |
| `time_range` / `region` / `credibility_rule` / `cross_validation` / `conflict_policy` | 写入检索/写作提示词，引导模型行为 | 提示词 |
| `evidence.citation_template` | 写入写作提示词；`citations` / `citation_sources` 按模板解析并校验 | 提示词 + 验收 |
| `verification.checks` | 落盘后逐项执行并输出报告 | 硬约束（只报告） |
| `SKILL.md` / `references/` / `assets/template.docx` | 注入写作与改写提示词；模板作为文档基底 | 硬约束（注入） |

**不在配置中暴露不存在的能力**：系统没有网页检索、BM25/关键词检索器、reranker，知识库节点也没有时间字段，
因此不提供 `web_search`、`strategy: keyword|hybrid`、`date_filter` 这类字段；写了会直接校验失败或被忽略。
`research.on_empty` 在无有效证据时执行：`mark_missing` 结束该题；`fallback_source` 切换下一来源（另一文件也算另一来源）；`rewrite_query` 且 `query_expansion=true` 时优先使用备选查询或模型改写，在同一来源重试。无有效新查询时按队列尝试下一来源，绝不重复同题相同来源和查询。各题先各检索一次，再轮转重试；提前停止、来源或预算不足仍如实报告未覆盖项。`max_rounds` 小于研究问题数时拒绝加载。

### 工作流编排

1. **匹配**（`skill` 节点）：`--skill` 显式指定 > 用户原话出现 Skill id（完整词边界）>
   用户原话出现最长的 Skill 名称 > 最长触发词唯一命中 > 意图识别建议 > 不启用。
   同长度多命中视为歧义，提示改用 `--skill`；禁用 Skill 不参与自动匹配。
2. **校验**：pydantic 校验配置；`doc_type` 与当前任务不匹配直接报错。
3. **能力预检**：逐项检查 `file`（是否有可检索文件）、`knowledge`（图数据库连通性）、
   `template`（文件是否存在）与权限（本地进程暂无权限模型）。部分不可用时跳过该来源并打印；
   全部不可用且技能声明了研究目标时直接报错。
4. **检索计划**：先由模型把研究问题改写成 1~3 条检索式（失败则退回问题原文），
   再按「问题 → 来源优先级 → 文件」展开成有序队列；单题受 `per_question_attempts` 限制，
   全局受 `max_rounds` 限制。
5. **执行与覆盖判断**：每执行一步，下一轮判断新增证据是否回答了当前问题；
   覆盖则跳过该问题剩余步骤，未覆盖则继续下一步；预算或步骤用尽时把未覆盖问题写入
   `retrieval_report.missing`。检索工具返回的可恢复错误不会污染最终状态。
6. **生成**：按 `structure.sections` 逐章生成，注入 SKILL.md、references、原始证据与已接受章节。程序统一章节编号，检查正文、关键词、引用及剩余篇幅，失败有限重试，全部章节通过后才保存。业务事实、计算和未知信息如何处理由Skill说明，不能将文本匹配当作事实证明。
7. **验收**：程序化检查 + 语义补充评估，输出 `verification_report`、`citations` 与
   `unmet_requirements`；控制台会打印验收摘要、可追溯来源数量、未覆盖问题与未满足要求。

### 验收项

| 检查项 | 判定方式 |
| --- | --- |
| `required_sections` | 精确匹配去编号后的必填标题，并检查章内正文达到 `min_body_chars`；仅标题不算通过 |
| `section_order` | 已匹配章节的实际出现顺序是否符合清单 |
| `must_include` | 各章节范围内是否出现字面关键词；自然语言要求放在 `requirements`，由语义审核判断 |
| `required_data` | 字段具有实际内容而非仅标题/缺失说明；`numeric_fields` 还需有数值。支持别名及表格的行指标+列表头，不保证数值与来源一致 |
| `citations` | 需引用章节的引用标记数量是否达标 |
| `citation_sources` | 引用标记中的 `{name}` 是否能对应到已记录检索证据的来源；仅上传但未检索的文件不算已检索来源 |
| `unsupported_claims` | 断言/预测是否带引用或明确标注无依据；排除「无法判断」「不作推测」和待执行建议等边界说明 |
| `terminology` | `task.terminology` 中的主术语是否都出现在正文 |
| `banned_expressions` | 是否出现 `task.banned_expressions` 中的表达 |
| `no_placeholder` | 是否出现「待补充 / TODO / 此处省略」等占位内容 |
| `min_length` | 全文去空白字符数是否落在 `min_chars`~`max_chars` 区间；标题、引用和表格计入，合并单元格只计一次 |
| `semantic_review`（独立开关，不在 `checks` 列表中） | 模型评估章节 `requirements`、口径、冲突与资料一致性。指控须引用正文原句；一致性须提供两处引文，来源问题还须有资料原句。无效引文进入待核实项，遗漏要求或正文截断记为未完成，不据此虚报通过或已确认失败；有效引文本身仍需人工判断 |

### 创建与管理

2026-11-08 本地更新的完整操作步骤见 [本地修改与验收操作](docs/本地修改与验收操作.md)。修改未提交；真实模型场景须单独执行，不能以离线测试代替。

针对首份真实季度报告的空风险章、净利率遗漏、表间口径冲突与验收误报，已完成后续本地修正，详见 [生成与验收修正及重跑命令](docs/生成与验收修正_20261108.md)。

当前已按通用执行器的职责收敛实现，季度示例Skill为v4。移除了实验性的 `data_consistency`、`arithmetic_accuracy` 两项及专用解析模块，不再自动搬动表格内容或把Skill名称当成文档标题。语义要求支持多个独立 `quotes`，只对缺少可靠引文的要求补充一次，不重判原始问题。默认表格排版保留文本、结构和手动分页；模板的表格布局保持不变。当前能力、撤回原因及验证范围见 [架构收敛说明](docs/架构收敛说明.md)；v2/v3文档保留为历史记录。

文件来源专用 Skill（`source.priority: [file]`）在控制台跳过配置文件、数据库、向量模型和远程服务初始化。可用 `--request-file` 读取 UTF-8 需求，`--output` 指定文档路径，`--report-json` 导出所用 Skill、检索报告、验收报告和运行信息。默认生成成功退出0、运行失败退出1；加 `--strict-verification` 后，生成成功但验收未通过或未完成退出2。

报告中 `state=succeeded` 仅表示生成成功。`verification_report` 分开给出 `programmatic_passed`、`data_complete`、`retrieval_complete`、`semantic_review` 和综合 `status/passed`。检索缺失、所需语义审核未执行或正文审核被截断，都不能获得综合通过。`on_failure: report` 仍保留生成文档。

以下命令与 `console_app` 一致，需在 `src` 目录下运行（或先在仓库根目录执行 `pip install -e .`）。

写作和 Skill 管理统一使用 `console_app`：`--skill <id>` 用于写作/改写，`skill <操作>` 用于管理规则。管理逻辑位于内部模块 `skills/commands.py`，由控制台调用。管理操作会在初始化数据库之前执行；只有自然语言生成 `draft` 需要调用模型。

```bash
# 列出 / 查看 / 校验（目录与单文件都支持）
python -m document_agent.console_app skill list
python -m document_agent.console_app skill show industry_report
python -m document_agent.console_app skill validate industry_report
python -m document_agent.console_app skill validate skills/industry_report

# 生成 Skill 包（SKILL.md + skill.yaml）；--single 改为单文件
python -m document_agent.console_app skill new my_standard
python -m document_agent.console_app skill new my_standard --single --yaml

# 用自然语言生成（需要 MODEL_API_KEY 等模型配置）
python -m document_agent.console_app skill draft "写一份面向管理层的季度经营分析报告，要求数据可溯源" --id quarterly_report
```

运行 `python -m document_agent.console_app skill --help` 查看管理命令。首个参数 `skill` 保留作管理入口；若恰好有同名文件，可使用 `./skill` 或文件绝对路径。

### 在写作任务中使用

```bash
# 显式指定 Skill（id、名称、目录或文件路径均可）
python -m document_agent.console_app --skill industry_report D:/docs/行业数据.pdf
# 提示符输入：写一份 2026 年新能源行业研究报告，另存为 行业研究报告.docx

# 也可以在自然语言中直接说明，由意图识别节点匹配（会参考 task.triggers）
python -m document_agent.console_app D:/docs/行业数据.pdf
# 提示符输入：用「行业研究报告」Skill 写一份……
```

### 示例说明：`industry_report` 如何影响执行

`src/skills/industry_report/` 的配置会实际改变以下行为：

1. **计划驱动检索**：3 个研究问题会先被改写成检索式，再按 `knowledge → file` 展开；
   单题最多尝试 2 次、全局最多 6 步。每步结束后判断证据是否回答了该问题，覆盖则跳过剩余步骤，
   未覆盖则继续；预算耗尽时把未覆盖问题写入 `retrieval_report.missing`。
2. **只开放声明的来源**：`source.priority` 之外的检索工具会被拒绝；知识库召回 8 条、
   相似度低于 0.15 丢弃、按 id 去重、最多展开 6 轮。
3. **文件检索截断**：用户提供的 PDF/Excel 超过 30000 字时先截断，避免提示词超长。
4. **章节不可自由发挥**：`structure.sections` 的 5 个章节与顺序会直接作为生成计划，
   `evidence_types`、`must_include` 写入每章提示词。
5. **写作口径统一**：`SKILL.md` 的执行说明与 `references/glossary.md`、`style_guide.md`
   会注入写作提示词，约束结论先行、数据口径、禁用表达与引用写法。
6. **验收 = 程序化 11 项 + 语义评估**：正文缺少「市场规模」触发 `required_data`；
   引用「【来源：不存在的文件.pdf】」触发 `citation_sources`；
   「预计未来增长」既无引用也无「未检索到依据」触发 `unsupported_claims`；
   出现「大概」触发 `banned_expressions`。随后由模型补充评估术语口径、交叉验证、
   数据时间一致性与专业程度。落盘后的验收只报告；生成阶段的逐章检查会在多次失败后停止保存。
7. **返回可追溯结果**：最终输出包含文档路径、`citations`（来源/标题/片段/查询/文件时间等可用元数据）、
   检索覆盖报告与 `unmet_requirements` 清单。

### 已知边界

- 硬约束只有章节清单/顺序、来源工具白名单、检索参数、轮次上限、文件截断、模板与事后验收；
  语气、术语、交叉验证、冲突处理等只能通过提示词引导，模型可能不完全遵守；
- 知识库节点没有时间字段，`research.time_range` / `region` 无法真正过滤，只作为写作依据；
- 系统没有网页检索、关键词/BM25 检索器与 reranker，来源只能是 `file` 与 `knowledge`；
  因此配置里没有对应字段，声明不存在的工具会在加载期校验失败；
- 字体/字号等排版只设置基础样式并写入提示词，验收不检查字体；
- 能力预检只验证「来源是否可连接、文件是否存在」，不验证知识库是否真的存在所需数据；
- 证据是否覆盖研究问题由模型逐轮判断，可能误判；该判断只影响是否继续检索，不改变已获得的证据；
- 来源元数据只能保留系统真实拥有的字段：文件保留路径/标题/后缀/文件修改时间，
  知识库保留节点 id 与来源文件；系统中没有网页链接与发布时间，因此不会伪造；
- `semantic_review` 是补充评估，不参与硬判定以外的强制执行，也不会自动重写；
- 验收默认只报告、不阻断，也不会自动重写未通过的章节。

### 测试

```bash
python -m unittest discover -s tests -v
```

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

