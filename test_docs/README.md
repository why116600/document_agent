# 文档测试入口 v1.0.28

更新日期 2026-11-16。用虚构样例运行指定提交、核对成果、登记问题，再交给开发者修复。顶层保留三份主要文档。

| 入口 | 用途 |
| --- | --- |
| 本页README | 看入口、格式和提交边界 |
| [测试计划](test_plan_v1.0.28.md) | 测什么、步骤和判定 |
| [缺陷登记表](defect_register_v1.0.28.md) | DEF-001～DEF-012 的正误及回归状态，不设条数上限 |
| [本轮报告与真实Word](results/latest_run/README.md) | 历史失败和当前候选对照 |
| [samples](samples/README.md) | 12项虚构输入、中文任务、步骤与运行前预期 |
| [results](results/README.md) | 实际测试及两项历史探索 |
| start本机菜单 | 日常本机启动，团队资料不含该菜单 |

**最近一轮提测。** 在已同步 `develop` 的 `feature/skills-system@aa8d192` 上，用同一份虚构规则和请求做四次实际写作。Hy3 的 Skill 组通过本样例，Hy3 普通组内容不全，DeepSeek 两组都未生成 Word。本轮合入验收未通过。 [四次测试报告](results/latest_run/README.md#2026-11-16-skill-候选合入前测试)

日常回归以 `develop` 为主。11 月 11 日图谱回归在 `f8eaefb` 执行五次，复现 DEF-008，新登记 DEF-009。`feature/add-rewrite-function` 暂停单独测。 [图谱回归](results/latest_run/README.md#2026-11-11-develop-图谱回归与-skill-样例) · [执行安排](test_plan_v1.0.28.md#当前基线与后续执行)

| 格式 | 怎么用 |
| --- | --- |
| .md | 给人阅读，也便于程序处理；README是用途，case是步骤，expected是预期 |
| .txt | inputs是参考资料，requests是等程序提示后输入的中文任务，不是PowerShell命令 |
| .docx | inputs是原稿，成果目录是实际Word，用Word/WPS对照 |
| .json | 来源、哈希与检查回执，不当任务输入 |
| .cmd、.ps1、.py | 本机菜单及内部调用脚本 |

进子目录先看README。CP-xxx为12项准备样例，EX-xxx为两项历史探索；原资料及Word的v1.0.15/v1.0.0是它自身版本，不随报告变更。

11 月 6 日在 `f5bde52` 完成两组写作对照，两份 Word 均缺总标题，分别记录事实遗漏和 Skill 字段名缺失。11 月 10 日在同一提交完成四份 Word 的验收报告对照和十个规范选择用例。原 CP-007、CP-008 及已有缺陷按实际被测提交跟踪。 [历史测试结果](results/latest_run/README.md)

## 本机怎么跑

下面的本机菜单用于日常练习。正式回归从独立检出目录运行固定的 `develop` 提交，并按该提交的公开入口准备配置和测试数据。

1. 双击start里的start_tests_v1.0.17.cmd，选1，再输入用例号。
2. 按提示准备工作副本，等出现“请输入你的需求”，粘贴对应requests中文任务并回车。
3. 按打印路径找成果，对照expected核对事实、范围、格式和原稿保护。
4. 保存准确需求与失败成果，再登记问题。退出0、有文件或能打开均不单独代表通过。

真实任务需要网络及有效现有接入，只用授权虚构材料。共享Python只读，安装、升级或卸载包需用户确认。测试不为了跑通而修改产品代码。

11 月 6 日和 11 月 11 日使用的隔离服务均已停止。第 6 项四组样例已在 `aa8d192` 验证发布、加载及首次文件检索分派，Word 模板套用待实测。 [第 6 项样例](samples/skill_template_checks_20261111/README.md)

## 提交与记录

测试资料按批次提交给 `test/test-plan`，以 Git 保存每次报告、样例与证据。36 条正式主用例整体验收待完成。 [测试分支提交历史](https://github.com/why116600/document_agent/commits/test/test-plan/)

可审阅范围为测试文档、样例包、实际 Word 和脱敏证据。已有源码/根配置改动、个人脚本、.runtime、环境、模型、数据库、密钥、原始模型往来及外部备份不纳入本次提交。逐文件审查，用户审核后逐条暂存、提交和推送测试分支，合入develop另行处理。

报告保留正确和错误之处，分清历史/本次执行、结构/页面核对。未验证或边界待确认的事项保持未完成。
