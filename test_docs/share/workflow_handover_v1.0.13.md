# document_agent 固定测试工作流修改与交接 v1.0.13

文档版本：v1.0.13。更新日期：2026-10-21。本轮将“结果与建议先行、详细证据可追溯”落实为固定执行步骤、模板和只读交付检查。未修改产品、重跑业务、安装依赖或操作 Git 暂存/发布。

## 1. 这次落实了什么

| 文件 | 作用与改动范围 |
| --- | --- |
| [固定工作流](test_workflow_v1.0.13.md) | 八步动作、逐步产物、完成条件和 README 四块表达顺序 |
| [单项清单](templates/run_checklist_v1.0.13.md) | 运行前、执行、对照与交付的可复用模板；不是已完成记录 |
| [交付检查器](tools/check_test_delivery_v1.0.13.py) | 纯标准库、只读材料，检查结构/关联/归档哈希/链接及有限敏感文本模式 |
| [AGENTS](AGENTS.md)、[README](README.md) | 固定入口升级 v1.0.13，后续按流程执行并指向当前候选 |
| 本交接 | 本轮范围、实测边界和精确文件清单 |

更新前的固定入口完整保存为不共享的历史副本。原[材料包](exploration_v1.0.11/README.md)保留 v1.0.11，其入口说明保留 v1.0.12；原计划/控制/缺陷表 v1.0.9 及所有实际输入/成果未改。

## 2. 实际做过的检查器验证

| 验证场景 | 实际结果 | 能说明什么 |
| --- | --- | --- |
| 现有两项探索材料 | 静态检查返回 0：2 个用例、4 份输入/成果、38 个链接、9 个文本文件，无问题项 | 当前交付结构和已实现的机械检查符合 |
| 缺少一份用例文档 | 隔离副本返回 1，报告缺用例及失效链接 | 能发现本次模拟缺件 |
| 归档材料改变但清单未改 | 隔离副本返回 1，报告字节数/哈希不匹配 | 能发现本次模拟不一致 |
| 清单路径试图越出包范围 | 隔离副本返回 1，拒绝该材料路径 | 能拦截本次模拟越界 |
| 合成敏感值 | 隔离副本返回 1，报告规则和文件，不回显值 | 指定敏感模式检测与输出脱敏有效，不代表全面安全审计 |
| 未识别的报告结构 | 静态返回 0，同时给出需人工核对的提示 | 没有把未知结构宣布为业务通过，人工复核不能跳过 |

上述为检查器本身的 6 个验证场景，不算产品用例执行，不增加正式 36 条用例通过数。负例只改隔离副本，真实材料不变；脚本不创建缺失文件、不修成果、不读取个人环境凭据、不调用模型/数据库或 Git。

输出固定标明 `business_validation=not_performed`、`manual_review_required=true`。按本次交付范围完成人工事实、证据和隐私复核，页面等未验证项目如实保留限制后，再按授权继续具体提交；不能以退出码 0 代替业务或发布验收。

## 3. 当前精确增量候选：19 个文件

本清单包含此前尚未提交的两项探索材料及本轮流程文件，替代旧批次的 15 文件候选。已提交且未改的其他四份设计文档继续保留，不需重复选择。根测试目录私人入口、历史、原始日志、环境/接入、产品差异和旧顶层 v1.0.10 报告都不自动混入。

```text
test_docs/share/AGENTS.md
test_docs/share/README.md
test_docs/share/materials_handover_v1.0.11.md
test_docs/share/exploration_v1.0.11/cases/case_EX-001_v1.0.11.md
test_docs/share/exploration_v1.0.11/cases/case_EX-002_v1.0.11.md
test_docs/share/exploration_v1.0.11/expected/expected_EX-001_v1.0.11.md
test_docs/share/exploration_v1.0.11/expected/expected_EX-002_v1.0.11.md
test_docs/share/exploration_v1.0.11/inputs/EX-001/星桥演示设备借用规则_v1.0.0.txt
test_docs/share/exploration_v1.0.11/inputs/EX-002/设备借用办法_v1.0.0.docx
test_docs/share/exploration_v1.0.11/manifest_v1.0.11.json
test_docs/share/exploration_v1.0.11/outputs/EX-001/设备借用说明_v1.0.0.docx
test_docs/share/exploration_v1.0.11/outputs/EX-002/设备借用办法_v1.0.1.docx
test_docs/share/exploration_v1.0.11/README.md
test_docs/share/exploration_v1.0.11/reports/exploration_report_v1.0.11.md
test_docs/share/exploration_v1.0.11/reports/file_checks_v1.0.11.json
test_docs/share/test_workflow_v1.0.13.md
test_docs/share/templates/run_checklist_v1.0.13.md
test_docs/share/tools/check_test_delivery_v1.0.13.py
test_docs/share/workflow_handover_v1.0.13.md
```

这是可供用户核对的范围，不表示已暂存、提交或推送。不得用整个工作区或整个测试目录的添加代替审查；以后新增文件仍要逐项检查。

## 4. 后续怎样执行

从[工作流](test_workflow_v1.0.13.md)选择一个用例，复制清单，运行前冻结输入和预期；执行后保留真实输出、逐项核对，从报告生成结果与建议摘要，运行只读检查并人工复核，再按用户授权逐文件提交。

既有两项探索的证据限制和两个待确认观察继续保留，缺少的历史基线/退出码不补造。需要修产品时另行确认范围、独立提交并使用原失败样例回归；不为了流程通过修改被测行为。
