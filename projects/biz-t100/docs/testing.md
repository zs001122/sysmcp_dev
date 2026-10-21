# BIZ-T100 测试说明

适用版本及当前固定 wheel 部署 **0.9.2**；核对日期 **2026-10-21**。本页是日常测试命令的唯一指南：`scripts/offline.py` 验证离线逻辑，`scripts/live.py` 测试真实 T100。当前一个常驻 FastMCP 服务承载四接口，直连与 MetaMCP 使用同一组业务请求。

## 当前可见工具

| 客户端配置 | 连接位置 | 当前可见工具 |
|---|---|---|
| `.runtime/service/client-read.json` | 统一 FastMCP 服务 | `t100_employee_query` |
| `.runtime/service/client-write.json` | 同一个 FastMCP 服务 | `t100_employee_update`、`t100_account_maintain` |
| `.runtime/service/client-gateway.json` | MetaMCP 查询端点 | `sysmcp-t100__t100_employee_query` |
| `.runtime/service/client-gateway-account.json` | MetaMCP 账号端点 | `sysmcp-t100__t100_account_maintain` |

工具列表按服务装配动作、已验签 scope 和已启用主体许可取交集。员工新建已实现，但当前常驻主体没有建档许可；客户端持有新建 scope 不会自动开放该功能。当前常驻查询主体只可查询批准工号，不开放任意姓名、部门检索。

## 准备与必要参数

进入项目目录，首次使用或依赖变更后安装本项目环境：

```bash
cd /data/projects/sysmcp_dev/projects/biz-t100
uv sync --locked
```

0.9.2 本机测试使用固定签名主体 `temporary-local-operator`，签名私钥和令牌每次临时生成并原生验签；不把它记为正式个人身份。常驻服务已使用独立的 `.runtime/releases/0.9.2/.venv` 固定 wheel 环境；下列日常命令仍使用项目 `.venv`，`uv sync` 不会改动常驻安装。T100 地址、企业号、服务凭据路径、批准目标范围和审计位置已在私有 `.runtime` 配置；日常无需填写身份令牌、许可摘要、操作编号或幂等键。写操作必须加 `--confirm`。

| 命令 | 必要业务参数 | 业务对象 |
|---|---|---|
| `employee-query` | 至少一个筛选条件，如 `--employee-no` 或 `--employee-name`；当前常驻许可要求批准工号 | 员工档案检索，通用接口允许零条或多条候选 |
| `employee-create` | `--employee-no`、`--full-name`、`--department-no`、`--status Y/N` | 新员工档案，不自动创建登录账号 |
| `employee-update` | `--employee-no` 和至少一个修改字段 | 已有员工档案，服务先精确查询并保留其它已知字段 |
| `account-maintain` | `--employee-no`、`--account-id`、`--account-status Y/N`、`--is-reproduce Y/N`、`--password-reset Y/N` | 登录账号；员工档案须先存在 |

员工档案的 `status` 与登录账号的 `gzxastus` 是不同字段。`--account-status` 对应账号 `gzxastus`；员工查询不能确认账号启停、密码或权限效果。

同一次调用选择一种入参方式：快捷参数，或 `--data-file` 完整 `parameter` JSON；不能混用。JSON 文件应为私有文件（权限 `0600`），只包含业务参数，不包含身份或服务凭据。公开示例只使用合成占位资料；工号和账号分别填写，不默认相同，不使用未赋值的 Shell 变量。

## 离线逻辑验证

不访问真实 T100：

```bash
.venv/bin/python scripts/offline.py --suite interfaces -- -q
```

它验证四接口参数、授权顺序、实际请求报文、响应及审计逻辑。离线结果不能证明真实账号效果。

## 选择真实调用入口

下面业务命令不带 `--client-config` 时沿用本机 ASGI 调用。需要验证常驻服务时，在业务子命令前添加客户端配置；业务参数不变，短期签名令牌由入口生成，T100 凭据和许可只由服务端读取。

```bash
# 统一服务的查询客户端：工具发现，不调用 T100
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-read.json tools

# 统一服务的查询客户端：本人预填查询用例
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-read.json employee-query \
  --data-file .runtime/test-cases/owner/employee-query.json

# 同一服务的维护客户端：当前可见员工更新与账号维护，不执行写入
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-write.json tools
```

`tools` 只验证 MCP 连接、验签和工具发现，不调用 T100。发现成功时 `status=passed`、`upstream_attempted=false`、`read_requests=0`、`write_requests=0`。较长的 `inputSchema` 只是参数说明；只看到一个查询工具不代表服务只有一个功能，也不代表已查询到员工。

远程修改及账号维护使用 `client-write.json`，写操作仍须 `--confirm` 和服务端目标许可。以下为当前常驻服务的完整最短账号命令，本人业务参数已预填，按需要一次选择一项：

```bash
# 禁用登录账号：N，不复制、不重置密码
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-write.json account-maintain \
  --data-file .runtime/test-cases/owner/account-disable.json --confirm

# 启用登录账号：Y，不复制、不重置密码
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-write.json account-maintain \
  --data-file .runtime/test-cases/owner/account-enable.json --confirm

# 在启用状态下重置密码：同时请求账号状态 Y 和密码重置 Y
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-write.json account-maintain \
  --data-file .runtime/test-cases/owner/reset-password.json --confirm
```

这三项共用 `t100_account_maintain`，不是三个 MCP server。成功 `acknowledged` 仅证明回执；账号启停及密码效果需账号资料或实际登录验证。重置后的密码规则由 T100 维护方提供，不臆测默认密码。员工新建只有另行批准主体、动作和建档目标后才可见并执行，不能由客户端参数自动开放。

两个本机客户端连接同一常驻地址 `http://172.17.0.1:8781/mcp`，按自身主体和 scope 获得不同工具权限。直连协议默认 `2025-11-25`。MetaMCP 明确指定 `--protocol 2025-06-18`：查询 endpoint 使用 `client-gateway.json`，账号 endpoint 使用 `client-gateway-account.json`，访问 Key 分别配置；现版 Key 为用户级，不宣称天然绑定某一个端点。两个 namespace 引用同一 `sysmcp-t100` MCP server。当前网关调用者身份不透传，不能用该链路验收真实员工身份。启动及连接令牌维护见 [常驻运行说明](service-runtime.md)；两段自动续签的当前状态与故障处理见 [GOV 续签说明](../../gov/docs/connector-renewal.md)，日常业务命令不增加令牌操作步骤。所有全局参数放在 `tools` 或业务子命令前。

## 本机业务参数示例

本节不带 `--client-config` 的命令沿用本机 ASGI 入口，仍调用真实 T100，无需常驻端口。已有日常用例可以继续使用；常驻直连优先使用前节完整命令，不需要新增脚本。

先查询已批准的员工档案：

```bash
.venv/bin/python scripts/live.py employee-query \
  --data-file .runtime/test-cases/owner/employee-query.json
```

检查中文状态、匹配员工数与工号。随后按所需功能选择一项写操作，不把所有写命令作为自动连续流程执行。

### 员工新建与修改

以下 `E_NEW`、`E001`、`D001`、`D002` 和“合成员工”均为合成占位。真实使用时先替换为批准的目标和字段；不要对已有本人档案重复执行新建。

```bash
# 新建员工档案
.venv/bin/python scripts/live.py employee-create \
  --employee-no E_NEW --full-name 合成员工 \
  --department-no D001 --status Y --confirm

# 修改已有员工的部门，其余已知资料由服务保留
.venv/bin/python scripts/live.py employee-update \
  --employee-no E001 --department-no D002 --confirm
```

员工写入后会精确查询工号，检查逐条回执与可查询字段；更新还会在写入前查询唯一员工，按明确白名单保留其它已知资料。`verified` 表示本次可验证字段回读一致；请求中含暂未验证往返语义的日期字段时可能只标 `acknowledged`，不能宣称所有字段生效。需要修改其它文档字段或批量请求时，使用 `--data-file`，字段定义见 [上游接口契约](upstream-interfaces.md)。参数示例不授予实际目标权限，当前没有可直接执行的常驻建档用例。

### 已批准本人登录账号

禁用、启用、密码重置都走同一个 `account-maintain`。完整直连命令已列于 [选择真实调用入口](#选择真实调用入口)；本机 ASGI 调用只需省略其 `--client-config` 参数。预填业务文件分别为 `account-disable.json`、`account-enable.json`、`reset-password.json`，不需要另建脚本。

每次执行一条，核清结果后再进行下一项。账号状态与密码重置是两个独立参数：`password_reset=Y` 不代表自动保留账号状态；若同一报文写了 `gzxastus=Y`，就同时请求启用账号。接口不会返回新密码。

直接输入账号参数时，完整禁用及密码重置示例如下；`E001` 和 `A001` 分别是须替换的合成工号与登录账号，必须在批准目标范围内：

```bash
.venv/bin/python scripts/live.py account-maintain \
  --employee-no E001 --account-id A001 \
  --account-status N --is-reproduce N --password-reset N --confirm

.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-write.json account-maintain \
  --employee-no E001 --account-id A001 \
  --account-status Y --is-reproduce N --password-reset Y --confirm
```

启用只改变账号状态为 `Y`；重置必须明确所需账号状态并把 `--password-reset` 设为 `Y`。复制另须 `--is-reproduce Y --reproduce-acct`，角色／作业／据点明细通过 JSON 提交；这些操作须在批准范围内，省略明细与提交空数组含义不同。

## 经 MetaMCP 查询

```bash
# 只发现查询工具，不调用 T100
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-gateway.json \
  --protocol 2025-06-18 tools

# 经 MetaMCP 查询真实 T100
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-gateway.json \
  --protocol 2025-06-18 \
  --tool-name sysmcp-t100__t100_employee_query employee-query \
  --data-file .runtime/test-cases/owner/employee-query.json
```

工具前缀来自统一服务名 `sysmcp-t100`，不来自 namespace 或 endpoint 名。MetaMCP 2.4.22 不透传外部用户 JWT；输出 `caller_identity_forwarded=false`，下游只代表受控内部连接主体。该链路不能证明外部员工间身份隔离。

## 经 MetaMCP 执行本人账号操作

账号 namespace／endpoint 为 `sysmcp-t100-internal-account`，引用统一的 `sysmcp-t100` MCP server，继续调用同一个 `account-maintain`。先发现工具；下面三项按需选择，不自动连续执行，也不要求为本次登记重复已通过的手测写入。

```bash
# 只发现一个账号维护工具，不调用 T100
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-gateway-account.json \
  --protocol 2025-06-18 tools

# 禁用账号：N，不复制、不重置
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-gateway-account.json \
  --protocol 2025-06-18 \
  --tool-name sysmcp-t100__t100_account_maintain account-maintain \
  --data-file .runtime/test-cases/owner/account-disable.json --confirm

# 启用账号：Y，不复制、不重置
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-gateway-account.json \
  --protocol 2025-06-18 \
  --tool-name sysmcp-t100__t100_account_maintain account-maintain \
  --data-file .runtime/test-cases/owner/account-enable.json --confirm

# 启用状态下重置密码：账号状态 Y，重置开关 Y
.venv/bin/python scripts/live.py \
  --client-config .runtime/service/client-gateway-account.json \
  --protocol 2025-06-18 \
  --tool-name sysmcp-t100__t100_account_maintain account-maintain \
  --data-file .runtime/test-cases/owner/reset-password.json --confirm
```

此网关许可只接受批准员工／账号的一条 `master`，字段固定为 `gzxa003`（工号）、`gzxa001`（账号）、`gzxastus`（Y/N）、`is_reproduce=N`、`password_reset`（Y/N）。不重置时可启用或禁用；重置时必须同时提交启用状态 Y。不允许复制、`reproduce_acct`、角色／作业／据点 `detail`、其它字段或批量目标。完整账号维护能力仍可在本机按独立许可使用，不因网关限定而改写上游契约。

缺少 `--confirm` 会在入口拒绝，不调用 T100。取得 `acknowledged` 后，通过账号资料或实际登录确认启停／密码效果，不能用 `employee-query` 返回的员工 `status` 验证账号状态。此前 0.9.1 接入验证范围与实际执行事实见 [0.9.1 验证记录](../VALIDATION-v0.9.1.md)。

## 结果与证据

| 输出 | 判断 |
|---|---|
| `tools` 的 `passed` | MCP 连接及工具发现完成，T100 请求数为 0 |
| 查询的 `passed` | 查询完成，仍需检查匹配数及目标 |
| `verified` | 员工写入回执及字段回读一致 |
| `acknowledged` | 成功回执，尚有业务效果未验证；账号效果需账号资料或登录证据 |
| `empty_identifier`／`invalid_arguments` | 本地参数未通过，未调用 T100 |
| `permission_denied` | 目标或动作不在本地许可范围，未调用 T100 |
| `reset_pending_review` | 同一业务对象存在未核清写入，未再次调用 T100 |
| `upstream_rejected` | T100 返回拒绝或失败；按请求编号检查本次记录 |

输出保留中文操作／状态、机器码、是否调用 T100 和请求编号。本机调用证据位于 `.runtime/functions/cases/`，常驻服务证据位于 `.runtime/functions/service-cases/`；两者沿用 `.runtime/functions/audit/operations.sqlite3` 和历史未决检查。远程工具响应丢失时，若只见 `mcp_tool_call_attempted=true` 而缺少上游字段，不能推断 T100 未调用，须核查服务端审计。当前未实现跨请求业务幂等；相同成功用例再次添加确认会再次调用上游，不要把重复运行当作结果查询。失败或结果未知时先查记录，程序不自动重复写入；不要更换请求编号或删除审计绕过未决检查。完整排查见 [运维说明](operations.md) 和 [错误与结果说明](error-codes.md)。

需要查看已有请求时，将下面合成编号替换为该次输出的 request_id；此命令只读本机共享审计，不带远程客户端参数，不重新调用 T100：

```bash
.venv/bin/python scripts/live.py status \
  --request-id 11111111-1111-4111-8111-111111111111
```

2026-09-30 用户手测反馈及历史回执见 [收敛验证](../VALIDATION-consolidation-20260930.md)。0.9.2 在 2026-10-08 的 1940 项离线与构建结果见 [版本验证](../VALIDATION-v0.9.2.md)；2026-10-21 的固定部署、网关恢复及工具发现见 [部署验证](../VALIDATION-deployment-20261021.md)，本次 T100 业务读写均为 0；后续为交付完整执行 BIZ 1940、CORE 4、GOV 29 项本地离线并均通过，见 [运维验证](../../gov/VALIDATION-operations-20261021.md)，不改写旧版本记录。实际待办只记录在 [开发清单](development-checklist.md)。此前单服务与两个端点的离线／组合证据见 [0.9.1 验证](../VALIDATION-v0.9.1.md)，675 项相关回归是该次既有结果；本次文档更新没有重放真实业务写入。内部临时身份仍标 `real_identity_verified=false`，正式个人身份、`business-validated` 和完整 `published` 分别取证。
