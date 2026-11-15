# Skill 验收与选择对照样本

本目录保存 2026-11-10 在 `f5bde52` 使用的最终 Word 对照样本和脱敏证据。Word 内容按固定规则编写，用于可重复的程序化验收。预期与执行结果在证据中分别记录。 [补测报告](../../results/latest_run/README.md#2026-11-10-skill-离线补测)

| 文件 | 用途与格式 |
| --- | --- |
| [01 合格正例](01_compliant.docx) | 含总标题、两章、五个必需字段和两个真实引用的 Word |
| [02 缺章节](02_missing_section.docx) | 删除“归还与依据”整章的 Word |
| [03 缺字段](03_missing_required_field.docx) | 删除“预约要求”整行的 Word |
| [04 伪来源](04_false_source.docx) | 替换末章一个来源文件名的 Word |
| [机器证据](evidence.json) | UTF-8 JSON，包含冻结预期、初次和复验结果、最终样本哈希、五份选择配置及十个用例输出 |

验收沿用 [Skill 规范](../skill_ab/skill.json)，以 [EX-001 虚构规则 TXT](../../results/inputs/EX-001/星桥演示设备借用规则_v1.0.0.txt) 为唯一已知来源。复跑时将四份 Word 分别传给 `run_verification`，选择测试则把证据中的配置放入独立 Skill 目录并复用已记录的节点输入。
