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
在 `src` 目录下运行，**全部用自然语言描述**——改写目标、参考文件、输出路径都由系统自动识别：

```bash
cd src
python -m document_agent.console_app
```

运行后直接输入需求，例如：
- 改写文档："帮我改写 D:/docs/设备管理制度.docx，把标题改成《设备管理办法》"
- 新写文档："写一份设备管理制度"
- 带参考资料："参考 D:/docs/巡检记录.pdf 写一份月度总结，存到 D:/docs/总结.docx"

说明：
- 改写目标：从自然语言里识别（例如 "帮我改写 D:/docs/制度.docx"）
- 参考文件：从自然语言里识别（例如 "参考 D:/docs/a.pdf"），交给检索节点提取素材
- 输出路径：从自然语言里识别（例如 "存到 D:/docs/out.docx"）；改写不指定时**原地覆盖原文件**，新写不指定时保存到 `data` 目录
- 所有保存都是"先写临时文件再原子替换"，不会留下半截文档

## 工作流
`intent`（识别写作意图）→ `summary`（参考文件摘要）→ `retrieve`（检索相关素材）→ 按意图分流：
- `new_docx`：规划章节并逐章生成新文档
- `rewrite_docx`：解析原文档结构，由模型给出"逐元素修改方案"并原地改写，保留原有格式、表格与页面设置
