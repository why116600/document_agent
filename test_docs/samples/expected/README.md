# expected：应该达到什么结果

目录说明版：v1.0.18。更新日期：2026-10-24。

[返回上一层](../README.md) · [返回测试总入口](../../README.md)

MD清单写运行后要查的内容、范围、格式和原件保护；JSON描述输入Word的原始结构，给程序比对用。

| 文件或子目录 | 用途与格式 |
| --- | --- |
| [expected_CP-001_v1.0.15.md](expected_CP-001_v1.0.15.md) | CP-001：单份规则生成短文；逐项核对清单 |
| [expected_CP-002_v1.0.15.md](expected_CP-002_v1.0.15.md) | CP-002：长资料定位三个分散事实；逐项核对清单 |
| [expected_CP-003_v1.0.15.md](expected_CP-003_v1.0.15.md) | CP-003：明确新版优先的规则冲突；逐项核对清单 |
| [expected_CP-004_v1.0.15.md](expected_CP-004_v1.0.15.md) | CP-004：资料夹带指令时遵守用户要求；逐项核对清单 |
| [expected_CP-005_v1.0.15.md](expected_CP-005_v1.0.15.md) | CP-005：资料未说明时不补造规则；逐项核对清单 |
| [expected_CP-006_v1.0.15.md](expected_CP-006_v1.0.15.md) | CP-006：空资料的不足信息处理；逐项核对清单 |
| [expected_CP-007_v1.0.15.md](expected_CP-007_v1.0.15.md) | CP-007：跨片段和多位置全文替换；逐项核对清单 |
| [expected_CP-008_v1.0.15.md](expected_CP-008_v1.0.15.md) | CP-008：只改指定章节正文；逐项核对清单 |
| [expected_CP-009_v1.0.15.md](expected_CP-009_v1.0.15.md) | CP-009：只改表格一个单元格；逐项核对清单 |
| [expected_CP-010_v1.0.15.md](expected_CP-010_v1.0.15.md) | CP-010：只调整字号和段距；逐项核对清单 |
| [expected_CP-011_v1.0.15.md](expected_CP-011_v1.0.15.md) | CP-011：润色时保留事实和否定条件；逐项核对清单 |
| [expected_CP-012_v1.0.15.md](expected_CP-012_v1.0.15.md) | CP-012：不存在章节时拒绝扩大修改；逐项核对清单 |
| [baselines_v1.0.15.json](baselines_v1.0.15.json) | 输入Word的文字/Run/表格/页眉等结构快照；JSON，不是实际输出 |

**先怎么用：**运行前先看本例预期，任务完成后逐项对照Word。未核对的项保持未核对，不预先勾通过。

**注意：**不要在看到结果后改标准来迎合它。JSON不是可直接粘贴的中文任务，也不是已经生成的成果；缺少页面证据时不能只凭XML判断版式通过。
