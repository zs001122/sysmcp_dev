# BIZ-T100 脚本与文档收敛验证

日期：2026-09-30；业务版本 **0.8.4**。任务依据：用户反馈“手动测试已通过，未发现问题”，要求收敛相关脚本并同步文档。本次仅整理文档、登记及私有用例索引，不修改业务代码，不重复真实业务调用。

## 当前交付

- scripts/ 日常入口为 offline.py、live.py 两项。旧12份Python及1份Shell已经退役，本次核查其来源清单并补充说明，没有再次搬移或新增测试模式。
- [testing.md](docs/testing.md) 是唯一日常测试指南；四接口必要参数、文件方式、中文结果及员工／账号对象边界统一说明。
- 根README／STATUS／AGENTS、workspace.toml、BIZ说明、上游／工具契约、错误、运维、发布、集成及原本人离线矩阵同步。完整范围与验收标准见 [consolidation.md](docs/consolidation.md)。
- 12份旧文档保存 [历史快照](docs/history/20260930/README.md)，7份旧运行页保留导航；来源及链接适配记录在 [origins.json](docs/history/20260930/origins.json)。旧项目登记也保留快照。
- 原DOCX、源码、两个脚本、既有制品、运行许可与审计保持原样。已有0.8.4 wheel／sdist 不覆盖，本次交付为工作区文档和登记。

## 手动测试与程序回执

| 证据 | 记录与边界 |
|---|---|
| 用户反馈 | 2026-09-30 手动测试通过、未发现问题；反馈未逐项列举四接口全部场景或登录结果 |
| 账号禁用 | 本地保存 gzxastus=N、password_reset=N、is_reproduce=N 的成功回执 |
| 账号启用 | 本地保存 Y／N／N 的成功回执 |
| 启用状态密码重置 | 本地保存 Y／Y／N 的成功回执，包含明确的启用请求 |
| 三项结果 | acknowledged；每项员工前查1次、账号写入1次，审计终态 succeeded；business_effect_verified=false 原值保留 |

请求编号、原始报文、受限回执和结果留私有 .runtime；索引见 .runtime/test-cases/owner/manifest.json。反馈记录与本地成功回执相互补充，未把ACK改写为自动核实账号效果，未用员工查询判断登录账号状态。

## 本次本地验证

| 检查 | 实际结果 |
|---|---|
| 相关离线回归 | test_live_cli.py、test_offline_entry.py：**56 passed，7.18秒** |
| 帮助与参数 | 两入口及五个live子命令，7份帮助成功；4组公开合成示例通过模型 |
| 私有用例 | 4份模型及既有请求摘要一致；3份成功回执与私有索引一致，未读取或输出服务key |
| 文档历史 | 12/12归档哈希一致；逆转链接路径适配后12/12原文哈希一致；历史相对链接存在 |

回归命令：

```bash
cd /data/projects/sysmcp_dev/projects/biz-t100
.venv/bin/python -m pytest tests/test_live_cli.py tests/test_offline_entry.py -q
```

入口清点、当前登记／链接、源码依赖及整理前后文件哈希的最终结果另列于下文及 [机器清单](docs/consolidation-manifest.json)。没有重新执行全量测试；此前1783项属于0.8.2，不作为本次56项结果。

## 最终静态核验

| 项目 | 实际结果 |
|---|---|
| 日常入口 | 恰好2份Python：live.py、offline.py |
| 归档隔离 | 12份Python无自动main启动段；1份Shell为不可执行文本 |
| 源码边界 | 20份源码无scripts／tests导入或旧仓运行路径 |
| 原文件保留 | 85份已存在文件整理前后SHA-256一致，覆盖源码、两入口、旧脚本、制品、DOCX、私有许可、当前及旧审计 |
| 当前登记 | workspace.toml可解析，版本0.8.4一致，12处项目／配置／文档／复用记录路径均存在 |
| 文档链接 | 35份当前及历史Markdown，270个本地文件链接全部存在，0失效 |
| 当前指南 | 旧脚本日常执行命令0项；真实目标信息0项，个人资料仍留私有用例 |
| 真实派发 | 本次新增T100业务请求0次，历史请求和unknown终态没有修改 |

文档链接统计对应本次更新范围，历史VALIDATION原文件未纳入改写。AGENTS保留完整历史授权与来源；其旧路径不作为当前执行登记。清单与核验结果见机器清单，脚本和归档记录可按SHA-256再核对。

## 状态与保留范围

原 biz-validated 的离线证据范围继续保留。正式IdP未接入，内部临时身份仍为 real_identity_verified=false；本次用户反馈不签收 business-validated 或 published。

员工档案历史 unknown、旧账号操作及逐条事件保留；没有删除、解锁或重放。当前同账号自身未决规则不变。真实身份、服务认证整改、账号效果逐项证据及MetaMCP登记／两跳属于后续独立验收。
