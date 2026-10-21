# BIZ-T100

交付候选 **0.9.2**；现有部署基线 **0.9.1**；文档核对日期：2026-10-08。一个 FastMCP 服务承载员工建档、员工查询、员工资料修改和登录账号维护四个业务工具。MetaMCP 只登记一个 `sysmcp-t100` MCP server，通过两个 namespace／private endpoint 分别开放查询与账号禁用、启用、密码重置。

| FastMCP 工具 | T100 接口 | 业务功能 |
|---|---|---|
| `t100_employee_create` | `employee.data.create` | 新建员工档案 |
| `t100_employee_query` | `employee.list.data.query.get` | 按工号、姓名、部门等查询员工资料 |
| `t100_employee_update` | `employee.data.update` | 修改员工姓名、部门、联系方式等档案字段；`status` 是此接口的员工资料字段 |
| `t100_account_maintain` | `user.account.create` | 新增或修改登录账号、账号禁用/启用、密码重置、复制及角色/作业/据点维护 |

**员工档案与登录账号是两个对象。** 登录账号维护以员工档案存在为前提；工号和登录账号分别指定。账号禁用/启用使用用户接口的 `gzxastus=N/Y`，CLI 参数为 `--account-status`。员工查询返回的 `status` 属于员工档案，不能用于验证登录账号启停或密码重置。

## 两个日常入口

在本项目目录使用自身环境：

```bash
uv sync --locked
# 离线逻辑验证
.venv/bin/python scripts/offline.py --suite interfaces -- -q
# 查看四个业务命令
.venv/bin/python scripts/live.py --help
```

日常只使用 `offline.py` 和 `live.py`。0.9.2 本机入口固定临时操作员主体 `temporary-local-operator`，签名私钥与令牌仍逐次生成并经过原生验签；它不是正式用户身份。内部本地签名操作员、本人目标许可和审计位置由私有运行目录预配置，操作者选择业务用例，写操作添加 `--confirm`。四接口必要参数和可执行命令统一见 [测试说明](docs/testing.md)。

2026-09-30，用户反馈手动测试通过、未发现问题；本地保留账号禁用、启用及启用状态密码重置的三次成功回执。此前 0.8.4 的脚本与文档收敛见 [收敛清单](docs/consolidation.md) 和 [收敛验证](VALIDATION-consolidation-20260930.md)，记录保持原样。该反馈不扩展为四接口所有场景已经逐项验收。

## 常驻服务

现有 0.9.1 部署基线仅有一个 `sysmcp-biz-t100.service`，使用 `server-maintenance.json` 装配四工具。实际可发现工具由主体许可和签名令牌 scope 过滤：本机查询客户端可见员工查询，本机维护客户端当前可见员工更新和账号维护。网关查询与账号 endpoint 通过工具白名单各开放一个工具，不再按功能拆分 MCP server。0.9.2 是未部署候选；本轮不启停服务、修改运行配置或调用真实业务。

```bash
# 使用查询客户端发现同一个常驻服务的工具，不调用 T100
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-read.json tools

# 通过真实 HTTP 查询已批准的员工
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-read.json employee-query \
  --data-file .runtime/test-cases/owner/employee-query.json
```

统一服务监听 `172.17.0.1:8781/mcp`，MetaMCP 使用 `host.docker.internal:8781/mcp` 连接同一地址。两个网关 endpoint 分别配置客户端和访问 Key，共用一个受控下游连接身份；网关不开放员工建档、员工更新、账号复制或权限明细。三条账号网关命令见 [测试说明](docs/testing.md)，统一服务配置和连接令牌维护见 [常驻运行说明](docs/service-runtime.md)。

此前只读直连与两跳真实查询对照已通过，见 [0.9.0 历史验证](VALIDATION-v0.9.0.md)。此前账号接入结果单独记录在 [0.9.1 验证记录](VALIDATION-v0.9.1.md)，不将工具发现或拒绝测试记为账号业务效果已验证。

`client-read.json` 与 `client-write.json` 连接同一个地址，是不同权限的客户端配置。前者目前发现一个查询工具，后者发现员工更新与账号维护两个工具；员工新建已实现，但当前主体没有建档许可。`tools` 只验证连接、认证和工具发现，长篇 `inputSchema` 是参数说明；执行 `employee-query` 或 `account-maintain` 才会进入业务流程。密码重置仍使用 `account-maintain`，明确指定账号状态及 `password_reset=Y`；最短完整命令见 [测试说明](docs/testing.md)。

## 文档导航

| 需要了解的内容 | 当前入口 |
|---|---|
| 项目结构、模块职责及数据流 | [项目梳理](docs/project-overview.md) |
| 开发完成情况、缺项及下一步优先级 | [开发清单](docs/development-checklist.md)，唯一待办矩阵 |
| 查询、资料修改、账号启停、密码重置怎么测试 | [测试说明](docs/testing.md)，日常命令的唯一指南 |
| 启动、连接配置、网关登记关系与令牌维护 | [常驻运行说明](docs/service-runtime.md)、[启动准备](docs/startup-readiness.md) |
| 工具输入输出、四个上游接口字段 | [工具契约](docs/tool-contracts.md)、[上游接口契约](docs/upstream-interfaces.md) |
| 中文状态、错误判断、审计与未决操作 | [错误码](docs/error-codes.md)、[运维说明](docs/operations.md) |
| 离线验收、真实身份缺口与发布标准 | [离线验收](docs/offline-acceptance.md)、[集成输入](docs/integration-inputs.md)、[交付说明](docs/service-release.md) |
| 候选验证及旧阶段来源 | [0.9.2 验证](VALIDATION-v0.9.2.md)、[0.9.1 历史验证](VALIDATION-v0.9.1.md)、[历史文档索引](docs/history/20260930/README.md) |

当前指南以代码、受控配置和对应版本证据为准；旧 `VALIDATION-*`、历史快照与收敛清单只说明当时状态。后续统一维护入口已记录为 [DEC-001](../../docs/项目讨论与决策记录.md)，尚未实现，不能当作现有管理能力。

## 代码与交付边界

```text
src/biz_t100/
  interfaces.py        四接口参数契约
  interface_client.py  请求封装、HTTP和回执解析
  functions.py         四个工具、授权、业务执行及回读
  function_audit.py    操作记录与未决写入检查
  service_runtime.py   四工具统一常驻装配、主体范围许可
  cli.py               配置检查、服务启动及短期连接令牌签发
scripts/
  offline.py           离线逻辑验证
  live.py              真实业务调用
config/                合成配置和报文示例
deploy/                本机用户级 systemd 服务单元
tests/                 合成身份及接口回归
  legacy_support/      旧入口的离线回归资料
```

公开工厂为 `biz_t100.create_function_server`、`create_function_app`、`create_service_runtime_app`。维护授权绑定调用者、客户端、企业、动作和批准目标；服务凭据在授权后读取。常驻服务沿用现有运行许可、业务审计及历史未决库，不由客户端请求新建许可或解除占用。员工更新先精确查询并保留其它已知字段，再写入与回读；接口完整定义见 [工具契约](docs/tool-contracts.md) 和 [上游接口契约](docs/upstream-interfaces.md)。

员工写入的可核对字段全部通过且没有未验证字段时返回 `verified`；若提交了当前未验证精确回读语义的时间字段，结果为 `acknowledged` 并列出 `unverified_fields`。账号 `acknowledged` 只表示接口接受，实际效果使用账号资料或登录流程确认。当前只有请求编号防重与未知目标占用，尚未实现跨请求业务幂等；相同成功报文再次确认会产生新的调用。未知写结果不自动重试；运行数据、原 DOCX、审计和历史制品保留在其原有位置，真实资料及凭据只留在私有 `.runtime`。

业务实现与审计归 BIZ，CORE 提供 FastMCP 薄封装，GOV 负责登记发布；产品源码不依赖脚本、测试或旧仓路径。直连按 `2025-11-25` 验证，MetaMCP 兼容链路按 `2025-06-18` 验证，不自动回退。当前内部临时身份仍为 `real_identity_verified=false`；MetaMCP 2.4.22 使用受控内部连接主体，不透传外部调用者 JWT。`business-validated` 和完整 `published` 未通过，内部接入证据与正式身份验收分别记录。
