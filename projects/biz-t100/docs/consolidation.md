# BIZ-T100 历史脚本与文档收敛清单（0.8.4）

本页记录 **0.8.4 阶段**的收敛范围、标准和交付，不是当前版本说明。当前已从固定 wheel 部署 **0.9.2**，见 [2026-10-21 部署验证](../VALIDATION-deployment-20261021.md)；统一常驻服务和限定 MetaMCP 接入的历史证据继续保留，见 [项目梳理](project-overview.md)、[测试说明](testing.md) 和 [常驻运行说明](service-runtime.md)。产品 CLI 已增加配置检查、服务启动和连接令牌签发；日常业务测试仍只有两个脚本。

下文及 [consolidation-manifest.json](consolidation-manifest.json) 中的版本、文件摘要和测试数量均属于当时证据，不用来校验后来版本的脚本。原验证、历史快照和归档摘要保持原样。

原记录日期：2026-09-30；当时业务版本保持 **0.8.4**。依据：用户反馈手动测试通过、未发现问题，要求收敛脚本并同步文档。当时四接口行为保持，真实身份接入与 GOV 发布按后续独立任务推进。

## 1. 脚本范围

| 分类 | 范围与处理 |
|---|---|
| 日常离线入口 | 保留 [scripts/offline.py](../scripts/offline.py)：合成身份、捕获上游的逻辑与契约验证；支持 all / interfaces |
| 日常真实入口 | 保留 [scripts/live.py](../scripts/live.py)：员工查询、新建、更新、账号维护；status 只查看已有请求，不新增派发 |
| 历史 Python | 12 份已退役脚本保留在 tests/legacy_support，供离线回归导入；不恢复自动启动入口 |
| 历史 Shell | test_account_write.sh 保留为 tests/legacy_support/test_account_write.sh.txt，不可执行 |
| 原测试与产品工厂 | 现有测试继续使用必要历史辅助函数；产品公开工厂保持兼容。sysmcp-t100 只作本地配置／审计检查，不作为第三个业务测试入口 |

12 份 Python 为 direct_check、direct_reset_check、employee_case、employee_case_support、internal_client、internal_support、internal_test、internal_upstream、live_employee_call、live_read、live_write、live_write_status。逐项来源与适配见 [归档清单](../tests/legacy_support/origins.json)，职责见 [归档说明](../tests/legacy_support/README.md)。本次确认既有迁移已完成，没有再次搬移或复制脚本。

## 2. 同步文档清单

| 文档 | 更新标准 |
|---|---|
| [根 README](../../../README.md)、[STATUS](../../../STATUS.md)、[AGENTS](../../../AGENTS.md)、[workspace.toml](../../../workspace.toml) | 当前版本、两入口、四接口及真实证据一致；去掉不存在的旧脚本登记；收敛规则写入准则 |
| [BIZ README](../README.md)、[testing.md](testing.md) | 唯一日常导航；先给最短可执行流程，再解释必要参数；不要求反复手工配置身份、摘要、编号 |
| [upstream-interfaces.md](upstream-interfaces.md)、[tool-contracts.md](tool-contracts.md) | 四接口字段、原生认证和业务目标边界一致；员工 status 与账号 gzxastus 区分；账号与员工分别指定 |
| [error-codes.md](error-codes.md)、[operations.md](operations.md) | 中文判断与固定错误码一致；当前运行路径、计数、未决处理准确；不把历史端口说成当前服务 |
| [service-release.md](service-release.md)、[integration-inputs.md](integration-inputs.md)、[offline-acceptance.md](offline-acceptance.md) | 版本与证据归属准确；内部手测、真实身份、GOV 发布分别记录；原本人隔离矩阵保留独立适用范围 |
| 私有 .runtime/test-cases/owner/README.md、manifest.json | 预填用例、最新手测反馈和保存的回执对应；真实员工资料只留私有目录 |
| 7 份旧运行页 | direct-check、internal-test、approved-employee-case、live-read、live-write、password-reset、startup-readiness 改成历史导航，保留旧链接可追溯 |
| [历史文档目录](history/20260930/README.md) | 保存上述 7 页及 operations、tool-contracts、integration-inputs、service-release、error-codes，共 12 份快照；记录来源和哈希 |
| 本收敛清单与 [本次验证记录](../VALIDATION-consolidation-20260930.md) | 明确范围、交付、验收结果与未涵盖事项；机器清单见 [consolidation-manifest.json](consolidation-manifest.json) |

历史 VALIDATION 文件、原 DOCX、审计、请求及回执保持原样；当前页面引用它们时注明时点，不追溯改写历史成功或 unknown。

## 3. 收敛与更新标准

1. scripts/ 中恰好 **2 个 Python 日常入口**，无第二套模拟、计划或写入命令。live 的四个业务子命令及 status 不新增模式。
2. **12 个历史 Python 无自动 main 启动段**；历史 Shell 无执行位。来源清单完整，回归测试仍可使用必要辅助函数。
3. 当前指南中的业务命令只使用 offline.py / live.py；旧命令仅留明确标识的历史快照。项目登记中所有当前 entry 路径存在，无旧常驻端口承诺。
4. 文档与命令帮助、模型、作用域和中文结果一致。快捷参数与 --data-file 不混用；所有写操作显式 --confirm。复制、明细、批量用完整文件表达，不增加新脚本。
5. 查询 passed、员工回读 verified、账号回执 acknowledged 分别解释。用户手测反馈另记，不将 ACK、员工档案 status 或合成身份当作登录验证、正式身份通过。
6. 源码、两个脚本、锁文件、既有制品、运行许可和审计不因文档整理改变；私钥、令牌、真实工号、服务 key 不进入公共文档或产品包。源码不依赖 tests、scripts 或旧仓路径。
7. 当前文档和归档导航的本地文件链接有效；历史内容除路径适配外保留，适配与原始／归档哈希明示。

## 4. 最终交付与验证

交付为当前项目中的两个日常入口、唯一测试指南、同步后的契约／运维／状态文档、历史来源清单及本次验证记录。业务版本保持 0.8.4，已有同版本 wheel／sdist 不覆盖；这是文档与登记整理交付。

| 验证 | 判定方法 |
|---|---|
| 入口与归档 | 清点两个入口、12 份 Python、1 份 Shell；检查 AST 自动启动段及文件权限；比对来源／归档哈希 |
| 必要参数与配置 | 查看两个入口及全部子命令帮助；只读校验四个私有用例模型；解析 workspace.toml 并检查登记路径 |
| 相关离线回归 | 使用现有 test_live_cli.py、test_offline_entry.py，验证参数解析、中文判断、文件模式及离线转发；不新增重复实现的测试 |
| 文档一致性 | 检查本清单所有当前文档与历史快照本地链接、版本、工具名称及入口导航 |
| 历史与秘密保留 | 比对源码、脚本、制品、DOCX、许可及审计整理前后 SHA-256；公共内容不含真实目标或凭据 |
| 业务证据 | 记录用户手测通过与本地禁用、启用、重置三次成功回执；本次不重复调用真实 T100 |

实际数量和检查结果只在执行后记入 [验证记录](../VALIDATION-consolidation-20260930.md)。biz-validated 保留原离线范围；business-validated 与 published 继续独立验收，不由本次整理提升。
