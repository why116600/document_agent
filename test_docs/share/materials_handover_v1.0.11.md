# document_agent 测试材料目录修改与交接 v1.0.11

文档版本：v1.0.11。整理日期：2026-10-21。本轮完成两项已执行探索测试的材料归档和入口更新，不修改产品、不重跑模型或业务流程，不执行 Git 暂存/提交/推送。

## 1. 实际交付

新增 [exploration_v1.0.11](exploration_v1.0.11/README.md)，用 cases、inputs、expected、outputs、reports 五类目录，将 EX-001 新建和 EX-002 旧 Word 替换的用例、原需求检查、输入、实际成果与前后对比关联。

四份合成材料均为当前原件的完整副本，字节哈希一致；原件保留，不改成“更正确”的结果。说明/材料包版本为 v1.0.11；TXT/Word 的原 v1.0.0/v1.0.1 文件名与内容版本不变。预期事后整理的性质、当时基线/退出码/哈希不足均明确标注。

EX-001 已观察到生成和打开，事实与指定文字格式有当前文件检查支持，精简程度和长度口径待明确。EX-002 已由用户确认实际执行，三项替换与其他正文/run 属性符合当前对照，同时新增空页眉/页脚部件，留作 OBS-002，未声称完全保真或确认缺陷。两项探索不折算为正式 36 条用例通过。

## 2. 只改哪些部分

- 新增完整材料包 12 文件：2 用例、2 检查清单、2 输入、2 实际成果、2 报告数据以及包 README/manifest。
- 固定 README/AGENTS 入口升级 v1.0.11，仅同步当前材料、归档规则和候选链接；更新前的原版本已完整保留为不共享的历史快照。
- 新增本交接。原 v1.0.9 计划/控制/缺陷表和旧交接未改；顶层 v1.0.10 记录保留历史，本轮不将该旧单份记录另行纳入提交。

当前完整材料从 README 进入，旧批次清单和旧“未执行改写”叙述不覆盖用户已确认的 EX-002。不得据此更改原提交或事后伪造执行证据。

## 3. 本轮精确候选：15 个文件

本清单是本轮增量范围；已提交的其他四份未改设计文档继续保留在仓库，不需重复暂存。只审查并选择下列明确文件，不能将整个工作区或未知新增文件整目录添加。

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
```

本轮未自动暂存或发布。个人环境/接入、源代码差异、根测试目录私人说明、历史、旧单份报告、原始日志和账户数据不在本清单中。素材 ZIP 的模板元数据不作为实际运行时间或个人电脑证据。

## 4. 核对与限制

核对包括：四份副本与当前原件的哈希、输入输出角色、文字/格式/结构对比、观察与确认缺陷分开、文件版本与链接、共享隐私范围及产品/Git 状态不变。机器可读记录为[归档清单](exploration_v1.0.11/manifest_v1.0.11.json)和[文件复核](exploration_v1.0.11/reports/file_checks_v1.0.11.json)。

本轮仅检查当前文件结构和文本，没有新增页面渲染或完整默认链路验证。中文富文档要求宋体黑色；原成果按原字节保存，Markdown 显示字体由平台控制。额外页眉页脚结构的页面影响未独立实看，不能把该限制省略为“所有格式完全没变”。

## 5. 接手

打开[材料包入口](exploration_v1.0.11/README.md)，按用例找到对应 inputs、expected、outputs 和 reports。后续先锁定版本、冻结样例和预期再复测；真实缺陷在对应基线确认后登记，产品修复仍须另获授权、独立提交和原例回归。
