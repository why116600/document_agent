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

模型配置（DeepSeek，OpenAI 兼容接口），使用前需设置环境变量：
```bash
set MODEL_API_KEY=你的密钥
set MODEL_BASE_URL=https://api.deepseek.com/v1
set MODEL_NAME=deepseek-chat
```

## 运行控制台程序
在 `src` 目录下运行，**沿用命令行参数输入模式**——文件路径直接传参，终端支持按 `Tab` 键自动补齐，减少大模型识别路径的误差与繁琐输入：

```bash
cd src
python -m document_agent.console_app [参考文件列表...] [-w 改写文件] [-m {patch,replace,global}] [--find 查找词] [--replace-with 替换词] [-o 输出路径] [-p 需求文本]
```

### 常用运行示例

#### 1. 全新写作文档
- **无参考资料从零编写**：
  ```bash
  python -m document_agent.console_app
  # 终端提示：请输入文档写作要求：写一份设备管理制度
  ```
- **带参考资料全新撰写（路径可在终端按 Tab 键自动补齐）**：
  ```bash
  python -m document_agent.console_app D:/docs/巡检记录.pdf D:/docs/数据表.xlsx
  # 终端提示：请输入文档写作要求：参考巡检记录写一份月度总结
  ```

#### 2. 改写已有文档（智能识别与三层模式）

- **默认模式：智能意图识别（`auto`，无需手动传 `-m`）**
  无需纠结该用哪种模式，直接输入自然语言改写要求，大模型自动识别最佳改写策略：
  ```bash
  # 自动识别为全局替换：自动提取替换对并走跨 Run 规则替换
  python -m document_agent.console_app -w D:/docs/合同.docx -p "把全文的'甲方'换成'委托方'，'2023年'换成'2026年'"

  # 自动识别为局部微调：采用工具化按需探查（不把整篇文档塞进上下文）
  python -m document_agent.console_app -w D:/docs/制度.docx -p "将第三章的考核指标改为每月一次"

  # 自动识别为全局重塑：按章节流式深度润色重写
  python -m document_agent.console_app -w D:/docs/制度.docx -p "全面润色整篇文档，将口语化表述调整为严谨的企业公文规范"
  ```

- **显式指定模式（可选，优先级高于自动识别）**
  - **`patch`（局部微调修补）**：`python -m document_agent.console_app -w 制度.docx -m patch -p "..."`
  - **`replace`（全局查找替换）**：`python -m document_agent.console_app -w 合同.docx -m replace --find "甲方" --replace-with "委托方"`
  - **`global`（全局重构润色）**：`python -m document_agent.console_app -w 制度.docx -m global -p "..."`

#### 3. 组合使用与非交互调用
- **改写并附带新参考文件另存新文件**：
  ```bash
  python -m document_agent.console_app -w D:/docs/制度.docx D:/docs/新标准.pdf -o D:/docs/制度_2026版.docx -p "参考新标准更新第二章技术指标"
  ```

### 参数说明
- `FILE...`：位置参数，提供 0 到多个参考文件路径，交由检索节点提取素材（支持 `.pdf`, `.docx`, `.xlsx`, `.txt`, `.md`, `.csv`, `.json` 等）；
- `-w, --rewrite`：指定要改写的原 `.docx` 文档路径。提供该参数后进入改写模式；不提供时为新建模式；
- `-m, --mode`：改写模式（`auto` 智能识别、`patch` 局部微调、`replace` 全局替换、`global` 全局重构润色，默认 `auto`）；
- `--find` / `--replace-with`：用于全局替换模式，快速指定查找词与替换词；
- `-o, --output`：指定输出文档路径。改写模式不指定时**原地覆盖原文件**，新写模式不指定时自动保存在 `data` 目录下；
- `-p, --prompt`：写作或改写的要求。如果不传，程序会友好地在控制台根据模式提示输入；
- 所有保存均采用“先写唯一临时文件再原子替换”，保障写盘安全，不会留下半截文档。

## 工作流
`intent`（参数校验与初始化）→ `summary`（参考文件摘要）→ `retrieve`（检索相关素材）→ 按意图分流：
- `new_docx`：规划章节并逐章生成新文档
- `rewrite_docx`：根据 `rewrite_mode` 分流调度：
  - `patch`：极小文档整篇差量修改 / 其余文档（含 4000 字以内的企业文档）工具化按需探查，不把整篇文档放进上下文
  - `replace`：跨 Run 文本查找替换引擎执行批量替换
  - `global`：章节结构划分（超长章节自动按元素分块），先取全局改写纲要约束一致性，再逐章深度重塑并原地替换回原模板；含图片等无法表达内容的章节自动跳过并原样保留
