# samples：准备好的测试样例

目录说明版：v1.0.18。更新日期：2026-10-24。

[返回上一层](../README.md) · [返回测试总入口](../README.md)

这里是12项CP用例的运行前材料：13份虚构输入（7份TXT、6份Word），以及中文需求、预期和步骤。它们目前都未执行。

| 文件或子目录 | 用途与格式 |
| --- | --- |
| [cases/](cases/README.md) | 每个用例测什么、选择哪些文件、怎样操作；Markdown说明 |
| [inputs/](inputs/README.md) | 交给程序的原始输入；按CP编号分文件夹 |
| [requests/](requests/README.md) | 原程序提示后要粘贴的中文任务；UTF-8纯文本TXT |
| [expected/](expected/README.md) | 运行后逐项对照的标准；Markdown清单及JSON输入基准 |
| [reports/](reports/README.md) | 准备检查与执行队列；不是已执行的业务成果 |
| [templates/](templates/README.md) | 复制后填写的单次执行记录；Markdown表格 |
| [manifest_v1.0.15.json](manifest_v1.0.15.json) | 机器清单：用例编号、输入路径、字节数和SHA-256；JSON数据 |

**先怎么用：**先看cases的用例列表。建议CP-001、CP-007、CP-008依次进行；也可以从start菜单按编号自动选择输入。

**注意：**这些输入是故意设计的测试条件，不要提前修改。README只作说明，预置菜单不会将它当输入；原件、需求、预期和清单继续保留v1.0.15。
