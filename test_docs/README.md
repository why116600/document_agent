# 文档测试入口 v1.0.22

更新日期：2026-10-24。**隔离审核稿，未暂存、未提交、未推送。** 顶层保留下面三份主要文档；每个子文件夹都有固定的README，说明用途、文件格式和先看什么，不增加版本化说明文件。

| 文件或目录 | 你用它做什么 |
| --- | --- |
| 本页 README | 怎么开始、成果在哪里、当前做到了什么 |
| [本批三项缺陷测试结果](results/latest_run/README.md) | 看真实结论、失败成果与复现依据 |
| [测试计划](test_plan_v1.0.22.md) | 测什么、怎么判定、怎样记录；已合并控制规则、流程和清单 |
| [缺陷登记表](defect_register_v1.0.22.md) | 记录有证据的问题，不设条数上限 |
| [samples：测试样例](samples/README.md) | 12项已准备好的输入、中文需求、预期和步骤 |
| [results：已有成果](results/README.md) | 已做两项探索的原输入、实际Word成果和对比证据 |
| `start`：本机启动菜单 | 仅本机保留，一个中文菜单及其调用脚本；不随测试资料推送 |

## 文件格式怎么认

| 格式 | 是什么、怎样用 |
| --- | --- |
| `.md` | Markdown文本：README是说明，case是操作步骤，expected是核对标准，report是报告；可用文本编辑器或Markdown预览查看 |
| `.txt` | 纯文本：inputs里是参考资料，requests里是等程序提示后再粘贴的中文任务；不是PowerShell命令 |
| `.docx` | Word文件：inputs里是原稿，outputs里是实际成果；用Word/WPS打开，原件不覆盖 |
| `.json` | 机器可读数据：manifest是路径/大小/哈希清单，baselines是输入结构快照，checks是检查回执；通常查看即可，不当任务输入 |
| `.cmd` | 本机Windows启动入口；日常双击start目录里的start_tests_v1.0.17.cmd，GitHub资料包不含该菜单 |
| `.ps1`、`.py` | 本机菜单内部调用的PowerShell/Python脚本，不需要逐个运行 |
| `.gitattributes` | 本目录的Git文件规则；冻结输入和成果的原始字节，防止换行转换造成哈希不一致；不用运行 |

进入任意子目录先看README；从“返回上一层”可以逐级返回，也能直接回到本页。文件名中CP-xxx关联12项具体用例，EX-xxx关联两项历史探索。不同资料的原版本保留，不要求全部改成导航说明的v1.0.20。

## 开始跑一个测试

下面六步适用于已经准备好环境的本机。`start`菜单和个人接入仅留本机，GitHub下载的测试资料不包含这些工具。团队读者先按[项目README](../README.md#运行控制台程序)确认公开入口与自己的授权环境，再按[samples用例](samples/cases/README.md)复制输入、启动程序并提交对应[中文需求](samples/requests/README.md)，对照预期记录结果；不能把本机菜单的可运行状态当作其他机器已经就绪。

1. 打开本目录的 `start` 文件夹，双击 **start_tests_v1.0.17.cmd**。
2. 选 **1**，输入 **CP-001**（也可以输入1），先看本例检查项。
3. 按回车开始。菜单准备工作副本，并复制本例的中文需求。
4. 等原程序出现“请输入你的需求”，按 **Ctrl+V**，再回车。
5. 任务完成后，菜单 **4 → 1** 打开新建成果；改写类用 **4 → 2** 打开本次工作副本。最终位置以原程序打印为准。
6. 对照预期核对内容、格式和原稿保护，记录实际结果。退出码0不等于内容正确。

建议接着做CP-007（跨位置全文替换）、CP-008（只改指定章节）。要自己选文件或跑原来的演示，主菜单选 **2** 进入助手；助手内 **0** 返回主菜单。主菜单 **5** 只检查已有准备项，**6** 切换Hy3/MiniMax，**0** 退出。

默认Hy3。实际文档任务需要网络、有效的现有模型接入与额度；菜单不会安装依赖或自动更换产品版本。模型会处理选定资料，只使用授权的合成材料。

## 成果与原稿

- 新建Word通常在项目 `.runtime/work/data`。
- 每次任务的冻结输入、工作副本和有限运行记录在项目 `.runtime/test_runs`；改写另存通常位于该批次的 `work` 目录。
- samples和results里的原件不直接交给程序修改。实际需求仍由你在原程序中输入，建议需求文件不能冒充实际输入证据。
- 退出菜单不会自动停数据库。全部任务完成后，可在助手的 **6 → 5** 按提示停库，不处理其他项目的进程。

## 已有测试结果

本次为两次提交中的第一批：只提交DEF-001～DEF-003及对应CP-002、CP-007、CP-008、CP-012，共4项具体场景、5次真实执行，4项失败，保留1份实际Word。先看[本批测试结果与成果](results/latest_run/README.md)。其余已执行结果已完整备份在项目外，待第二批提交；36条正式主用例整体验收未完成。下面另列此前两项人工探索。

| 探索 | 当前核对结果 | 仍需确认 |
| --- | --- | --- |
| 新建Word | 已生成并由用户打开；3天、提前2个工作日等事实和当前文字格式符合检查 | 原资料基本进入表格，精简程度及“200字”口径待确认 |
| 旧Word替换另存 | 7天→3天、1个工作日→2个工作日、v1.0.0→v1.0.1均正确，其他正文及文字格式属性保持 | 增加空页眉/页脚部件，页面影响尚未独立核对 |

直接看[新建成果](results/outputs/EX-001/设备借用说明_v1.0.0.docx)、[替换前](results/inputs/EX-002/设备借用办法_v1.0.0.docx)、[替换后](results/outputs/EX-002/设备借用办法_v1.0.1.docx)。详细证据见[两项探索报告](results/reports/exploration_report_v1.0.22.md)。原探索的预期和归档哈希为事后整理，缺失证据不补造，不将两项整体写成全部通过。

## 测试协作约定

Agent执行本目录任务前也应读取本页。用户负责测试：冻结输入和预期、暴露问题、保留失败、登记并移交；产品修复另行授权，不为了跑通而改源码、模型路由、配置或依赖。

记录完整提交和本机补丁标识；当前保留此前已有本机适配差异，不能把结果当作纯原作者版本验收。个人环境、凭据、原始模型往来、私人日志和数据库不纳入团队提交。start及运行记录留本机；测试资料按明确范围逐文件审查，不能整目录暂存或擅自提交、推送。

共享Python环境只读，安装或变更包须取得用户确认。事实、执行人、Issue、退出码和结果必须有依据。中文富文档使用宋体，文字黑色；Markdown字体由阅读平台控制。缺陷不设固定条数上限，保留所有稳定编号和未关闭事项。

## 提交记录与共享范围

Git和GitHub自带逐次提交记录，查看[本测试分支的提交历史](https://github.com/why116600/document_agent/commits/test/test-plan/)。在GitHub打开仓库后选 `test/test-plan` 分支，再点Commits；打开某次提交可看本次改了哪些文件，也可查看当时的旧版本。下列日期是提交时间，不代表实际推送时间。

| 已核实的提交 | 提交时间（北京时间） | 内容 |
| --- | --- | --- |
| [79e9b73](https://github.com/why116600/document_agent/commit/79e9b730f8ebae1f97b73d7a8fafdf9f2bbfcb23) | 2026-10-21 22:56:13 | 首轮测试设计与执行规范 v1.0.9 |
| [1bc727a](https://github.com/why116600/document_agent/commit/1bc727aaca9bb20810f9187ab43fc2fdb479f184) | 2026-10-22 00:28:27 | 两项探索材料及测试流程 |
| [dca3fc6](https://github.com/why116600/document_agent/commit/dca3fc627d8ae88c2fb0746ca35775fc9ccdf813) | 2026-10-24 01:22:05 | 测试资料整理、12项准备样例与探索成果 v1.0.19 |

v1.0.19资料整理已提交并推送到`dca3fc6`。后续实际提交与推送状态请查看上述分支历史；测试执行不会自动进行Git提交或推送。后续状态以GitHub历史及真实推送结果为准，不把此段当作自动更新的状态页。

v1.0.19资料整理提交了测试文档、samples、results与文件字节规则，并已记录旧 `test_docs/share` 路径移除。本轮新增的是实际测试记录、成果、缺陷登记和相关导航；后续提交范围仍须按真实Git差异审查。移除旧路径不删除过去提交中的内容。`start`、运行目录、个人启动脚本、环境、模型、凭据、原始私人记录及项目外备份不纳入本次提交；既有源码和根配置改动另行审查。

每批次使用一条可说明目的的新提交；正文写明变更、核对和未执行部分。提交后核对提交号、实际文件范围、推送分支及远端结果，并在交付回复给出真实链接；不另建逐次记录文件，不用改写旧提交或强制推送来整理历史。本机可在项目终端运行 `git log --oneline --decorate test/test-plan`；完整日期及说明可用 `git log --format=fuller test/test-plan`，用 `git show 提交号` 查看某次差异。推送测试分支与合并到develop是两件事。

## 本版整理与交接

旧测试目录已完整备份到项目外，本页合并原入口和必要协作规则，计划合并原控制/流程/清单；不再单独增加交接、历史索引或重复说明。备份不随测试资料上传。

v1.0.17当时只调整测试资料布局、文档引用与菜单路径/版本，运行副本统一放既有私有运行区。样例和实际Word成果保留原内容与原版本；产品源码、既有启动脚本、根配置、依赖和模型不改。资料整理和菜单验证不计入业务用例通过。

v1.0.17整理时已核对菜单启动/返回、12项用例列表、本机准备项、工作副本和参数传递、失败状态、样例成果哈希及当前链接。原程序当时已实际启动到“请输入你的需求”，知识库初始化未降级；该次验证禁用了HTTP发送，没有提交写作任务、消耗模型额度或生成新Word。原有成果和业务节点计数保持，真实模型写作仍按用例逐项执行和验收。

v1.0.18只补目录README和本页格式导航，不改变文件夹层级、测试计划、无限条数缺陷表、菜单脚本、样例、预期或实际成果。新README不是测试输入，预置菜单仍按原manifest的指定路径选择文件；手动选文件时不要选择README。未重新执行业务测试。

本次v1.0.19仅修正共享入口和相关引用、补提交历史说明，并增加测试目录文件字节规则。改动的版本化说明已更名，旧版保存在项目外；仍只保留三份顶层主要文档。样例、实际Word、manifest、检查数据、菜单、产品和环境未修改；没有新增业务执行、暂存、提交或推送。

本次v1.0.20只提交前三项真实缺陷、4项支持场景的记录与1份实际Word，并更新必要阅读导航。后三项问题与其余已测记录完整保留，待第二批提交。产品代码、已有启动菜单、环境、模型和原稿均未修改；旧版说明已备份项目外，顶层仍只保留三份主要文档。

## 本次待审范围 v1.0.22

上一笔v1.0.20已由`3ce9133`提交并推送。本审核稿在该提交的独立工作目录中准备，原项目工作区保持；这里只补前三项的证据，不包含第二批缺陷。你审核后才决定是否提交这一版。

| 拟纳入内容 | 用途 |
| --- | --- |
| [缺陷登记表v1.0.22](defect_register_v1.0.22.md) | 仍只有DEF-001～DEF-003，补需求原文、前后对比、日志原文及页面图 |
| [本批报告](results/latest_run/README.md) | 说明补证来源及审核状态，原执行次数不变 |
| [Word真实页面图](results/latest_run/DEF-003_actual_word_page_v1.0.21.png) | 直接看7日、30分钟和缺少75天；原像素保留 |
| [补证清单v1.0.22](results/latest_run/review_evidence_v1.0.22.json) | 程序可读的准确日志摘录、行号、来源哈希与图片哈希 |
| [DEF-003正误标注图](results/latest_run/DEF-003_annotated_comparison_v1.0.22.png) | 黑框指出错误，右侧列正确值；派生说明，原始证据保留 |
| 必要版本导航 | 测试计划和历史探索说明只更新当前登记表/报告引用及自身版本，不改正式36条用例或历史测试事实 |

原始Word和`execution_manifest_v1.0.20.json`均已在上一笔提交，本次不重复生成或修改。产品源码、根配置、个人脚本、环境、密钥、原始日志、本机运行目录、数据库和第二批完整记录均不纳入拟提交范围。

### 精确拟提交路径

以下只供审核，未执行git add。M为修改，D为旧版本移除，A为新文件；旧版保存在项目外备份，输入和原始成果保持。

```text
 M test_docs/README.md
 D test_docs/defect_register_v1.0.20.md
 M test_docs/results/cases/README.md
 D test_docs/results/cases/case_EX-001_v1.0.20.md
 D test_docs/results/cases/case_EX-002_v1.0.20.md
 M test_docs/results/expected/README.md
 D test_docs/results/expected/expected_EX-001_v1.0.20.md
 D test_docs/results/expected/expected_EX-002_v1.0.20.md
 M test_docs/results/inputs/EX-001/README.md
 M test_docs/results/inputs/EX-002/README.md
 M test_docs/results/latest_run/README.md
 M test_docs/results/outputs/EX-001/README.md
 M test_docs/results/outputs/EX-002/README.md
 M test_docs/results/reports/README.md
 D test_docs/results/reports/exploration_report_v1.0.20.md
 D test_docs/test_plan_v1.0.20.md
A  test_docs/defect_register_v1.0.22.md
A  test_docs/results/cases/case_EX-001_v1.0.22.md
A  test_docs/results/cases/case_EX-002_v1.0.22.md
A  test_docs/results/expected/expected_EX-001_v1.0.22.md
A  test_docs/results/expected/expected_EX-002_v1.0.22.md
A  test_docs/results/latest_run/DEF-003_actual_word_page_v1.0.21.png
A  test_docs/results/latest_run/DEF-003_annotated_comparison_v1.0.22.png
A  test_docs/results/latest_run/review_evidence_v1.0.22.json
A  test_docs/results/reports/exploration_report_v1.0.22.md
A  test_docs/test_plan_v1.0.22.md
```
