# results：以前已经跑出的成果

目录说明版：v1.0.18。更新日期：2026-10-24。

[返回上一层](../README.md) · [返回测试总入口](../README.md)

这里保留两项已发生人工探索的原材料、实际Word和对照证据。EX-001是新建，EX-002是修改旧Word；它们不等于正式36条用例已经通过。

| 文件或子目录 | 用途与格式 |
| --- | --- |
| [outputs/](outputs/README.md) | 先看两份实际Word成果；DOCX |
| [inputs/](inputs/README.md) | 再看当时的规则TXT和旧Word，方便前后比较 |
| [reports/](reports/README.md) | 查看详细核对、观察和证据限制；MD与JSON |
| [cases/](cases/README.md) | 当次任务和操作经过的事后整理；MD |
| [expected/](expected/README.md) | 从原任务事后整理的核对标准；MD |
| [manifest_v1.0.11.json](manifest_v1.0.11.json) | 四份原输入/成果的路径、大小和归档SHA-256；JSON |

**先怎么用：**看outputs里的成果，再对照inputs与reports。新建精简/字数口径和替换后空页眉页脚影响仍待确认。

**注意：**本目录是历史归档，新的运行不会自动存入这里。新建成果通常在.runtime/work/data，运行副本在.runtime/test_runs。原预期和哈希为事后整理，不能当成运行前冻结证明。
