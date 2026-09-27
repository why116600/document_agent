# document_agent
An intelligent agent capable of writing documents based on the content of an organization's self-built knowledge base

## 运行环境
已验证环境：conda 环境 `docx_agent`（`D:\Conda\envs\docx_agent`，Python 3.10）

安装依赖（在仓库根目录执行）：
```bash
D:\Conda\envs\docx_agent\python.exe -m pip install -e .
```

解析 PDF 参考文件时，若文件中含表格，会调用 `camelot` 提取表格（已随上面的依赖一起安装）。
`camelot` 的 `lattice` 模式还需要系统安装 Ghostscript；如果环境中没有 `camelot`，解析 PDF 时会自动降级为只提取文本，不会直接报错。

模型配置（DeepSeek，OpenAI 兼容接口），通过环境变量配置：

- **方式一：终端会话环境变量**：
  ```bash
  # PowerShell
  $env:MODEL_API_KEY="你的密钥"
  
  # CMD
  set MODEL_API_KEY=你的密钥
  ```

- **方式二：Windows 永久用户环境变量**：
  ```powershell
  [System.Environment]::SetEnvironmentVariable('MODEL_API_KEY', '你的密钥', 'User')
  ```

- **方式三：程序运行时的交互输入**：未检测到 `MODEL_API_KEY` 时，程序启动后会提示输入 API Key，输入后仅在本进程内生效（**不会写入任何文件**）；因此密钥既不需要放进 `.env`，也不会落盘。

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
- `FILE...`：位置参数，0 到多个候选文件路径。参考素材支持 `.pdf`, `.docx`, `.xlsx`, `.txt`, `.md`, `.csv`, `.json` 等；**改写目标必须是 `.docx`**。路径不存在或类型不支持会在意图节点直接报错；若无法从需求中确定要改哪份 `.docx`（一个都没有、或有多份而未能匹配上），会直接报错并要求指明具体文件名，**不会自动猜测采用某个候选文件**；
- **意图自动识别**：大模型从需求文本中判断任务类型（全新写作 / 改写）、改写模式（`patch` 局部微调、`replace` 全局替换、`global` 全局重构润色、`format` 仅格式规范化）、替换词对与保存文件名；
- **`format` 模式（格式规范化）**：只统一字体字号、标题层级、行距段距与对齐等**排版样式**，**原文文字一字不改**（图片、公式、域代码、超链接、合并单元格均原样保留）。它先按原文正文格式生成内置方案（同角色全文统一、标题字号形成梯度），再按你的显式要求（如"标题用黑体三号"）微调；写入前打印采用的格式方案，写入后自动做文字长度保全校验。注意：它不会凭空生成标题层级——若需要自动梳理章节标题（会改写文字），请使用 `global`；
- **`global` 模式（全局重塑）**：按章节**重新生成正文**，属于生成式改写，会改写文字与措辞（为全篇口径统一服务）；若只想规范排版、要求原文文字一字不改，请直接使用 `format` 模式（系统也会把"只改格式"的诉求识别为 `format`）；
- **保存行为**：默认一律**另存为新文件**（未指定文件名时智能拟定，兜底为 `<原文件名>_修改版.docx` 或 `data/新生成文档.docx`），不会动原稿；只有需求中明确说"直接在原文件上改 / 覆盖原文件"才会原地覆盖；
- **图数据库**：`src/setting.json` 存在时会尝试连接 Memgraph；连接失败自动降级为纯文档模式（打印警告，不影响写作与改写功能）。未接入图数据库时不会给出节点标签，`retrieve` 节点在"没有参考文件、也没有知识库检索需求"时会直接跳过检索进入写作阶段（省去一次空转的大模型调用）；只要传入了参考文件，仍照常走文档检索；
- 所有保存均采用"先写唯一临时文件再原子替换"，保障写盘安全，不会留下半截文档。

## 工作流
`intent`（参数校验与初始化）→ `summary`（参考文件摘要）→ `retrieve`（检索相关素材；无参考文件且无知识库检索需求时自动跳过）→ 按意图分流：
- `new_docx`：规划章节并逐章生成新文档
- `rewrite_docx`：解析原文档结构，由模型给出"逐元素修改方案"并原地改写，保留原有格式、表格与页面设置

使用以下命令将文档录入知识库中（需先启动本地 Memgraph 图数据库，连接配置见 `src/setting.json`）：
```bash
python -m document_agent.knowledge_base_record <file1> <file2> ...
```
