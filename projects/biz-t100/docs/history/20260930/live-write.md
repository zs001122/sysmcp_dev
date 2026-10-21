# 真实账号维护：启用、禁用与密码重置

> 历史阶段说明。当前统一使用 [四接口与两类测试入口](../../testing.md)，本文旧命令仅作为历史回归背景，不再作为日常运行入口。既有运行资料与审计保留。

当前版本 **0.7.0**。本机入口 `scripts/live_write.py` 直接装配原生 FastMCP 和真实 T100 HTTP 客户端，无需先运行 plan，也不依赖 MetaMCP。每次操作使用本地 ASGI 会话；原 8768 只读监听不增加写工具。身份仍由临时本地签发者签名并原生验签，`real_identity_verified` 保持 false。

用户已确认：员工资料是查询、更新和用户资料新增/修改的前提；`status=Y` 表示启用、N 表示禁用；`employee.data.update` 只传员工号和 status 时，省略的其他员工字段保持。当前用例已有员工资料，无需重复建档，本轮未开放员工创建工具。原批准目标的私有状态开关已开启；启用＋Y 另列独立白名单，重置仍仅允许原批准状态 N，未扩大到其他目标。依据见 [接口索引](../../upstream-interfaces.md) 和 [RU-004](../../../../../docs/reuse/RU-004-T100账号禁用与受控写入.md)，本版本证据见 [v0.7验证](../../../VALIDATION-v0.7.md)。

## 当前真实结果

批准目标的新禁用已执行一次：前查 1 次成功，`employee.data.update` 发写 1 次后记录 `upstream_rejected`，未进入成功回读。返回 `reset_outcome_unknown`、`business_effect_verified=false`，操作库状态 unknown。随后另一次独立只读查询精确匹配员工但仍为 Y。未重试，未执行真实启用；该目标当前被三个写动作共同的未知锁拦截。

此次具体 HTTP 状态或上游业务码未留存，不能推断具体拒绝原因。先由接口维护方关联服务端执行记录核查，不自动解锁或补发。新增失败诊断只保存限定数字元数据、服务阶段等去敏信息，供后续获准调用使用，没有为补诊断重放此次写入。证据保存在私有 `status-results/` 和查询审计，不把真实目标或操作 UUID 写进公开文档。

## 动作与接口

| 命令 | 工具 / scope | Profile 与写入 |
|---|---|---|
| `enable-account` | `enable_my_t100_account` / `t100:account:enable:self` | `employee-status-v1`；`employee.data.update`，状态固定 Y |
| `disable-account` | `disable_my_t100_account` / `t100:account:disable:self` | `employee-status-v1`；`employee.data.update`，状态固定 N |
| `reset-password` | `reset_my_t100_password` / `t100:password:reset:self` | `legacy-account-v1`；`user.account.create`，`password_reset=Y`，账号状态必须显式指定 |

启停共用同一闭环：

1. 原生验证测试用户令牌，按受控映射和动作权限得到唯一员工及独立登录账号，授权后读取管理员 key。
2. 用 `employee.list.data.query.get` 前查员工，要求恰好一条记录、员工号与授权完全相等；未找到、多条或错工号时不发送状态更新。
3. 查询期间如撤权或改绑，写前重新授权并拒绝；异步授权结束还要再次验证令牌有效期。通过后仅发送一次 `employee.data.update`，`employee[]` 内只有 `employee_no`、`status`，公共 `is_digiwin_product=N`；不回填姓名、部门、联系方式或账号字段。
4. 校验总执行成功及唯一逐条回执，`source_no/erp_no` 均等于授权员工，`execute_description` 为空；随后查询同一企业和员工。
5. 回读必须唯一、工号全等、状态等于本次目标 Y/N，并由专用 `EmployeeStatusReceipt` 关联目标和观测；普通 ACK 不能代替回读凭据。再次核验授权并持久记录终态后，才返回 `business_verified`。

登录账号仍用于独立受控绑定、权限和跨动作占用，不发送到员工更新接口；不能因为工号相同、姓名相同或查询 `account_no` 为空就自行建立/改写账号绑定。启用 scope 和许可独立，禁用许可不能用于启用。

重置继续使用独立账号接口，固定 `is_reproduce=N`，显式员工/账号/状态，不传角色或站点 detail，不输出明文密码。用户已确认省略 detail 保留原角色/站点；该确认与员工更新省略字段保持是两条独立契约。`user.account.create` 仍是新增/修改共用接口，没有 API 级 update-only 保障；只允许已确认存在且归属明确的账号。

## 直接真实调用

在本项目目录执行。以下全是合成标识；实际使用只能替换为私有白名单已批准的员工、登录账号及动作。新命令不会自动扩展白名单。

```bash
# 每个新的操作意图单独生成 UUID，并记入本次操作记录
.venv/bin/python -c 'import uuid; print(uuid.uuid4())'

# 禁用：自动前查、写 N、回读 N
.venv/bin/python scripts/live_write.py disable-account \
  --employee-no E001 --account-id LOGIN-A --operation-id UUID_FOR_DISABLE \
  --existing-account-confirmed --confirm

# 启用：自动前查、写 Y、回读 Y；目标必须另有启用许可
.venv/bin/python scripts/live_write.py enable-account \
  --employee-no E001 --account-id LOGIN-A --operation-id UUID_FOR_ENABLE \
  --existing-account-confirmed --confirm

# 重置：明确传批准状态，不隐式启用账号
.venv/bin/python scripts/live_write.py reset-password \
  --employee-no E001 --account-id LOGIN-A --account-status N \
  --operation-id UUID_FOR_RESET --existing-account-confirmed --confirm

# 只查看本地操作库，不请求 T100
.venv/bin/python scripts/live_write.py status --operation-id UUID_FOR_OPERATION
```

`UUID_FOR_*` 须替换为有效 UUID，各新意图使用独立编号。查询结果或重复提交沿用原编号，已记录操作不会重发；结果未知不得换 UUID、切动作或新建操作库绕过。

`--employee-no`、`--account-id`、重置的 `--account-status` 仅是本机操作输入。命令保存受控快照并走原生身份/授权链；远程 MCP 工具仍只接收 `confirm`、`operation_id`，不能传工号、账号、状态、管理员 key、姓名或角色。缺少 `--confirm` 拒绝，`--existing-account-confirmed` 表示明确确认账号已存在且属于该员工。启停固定 Y/N，不接受任意状态。

`plan` / `show` 和 `call --operation-id UUID --confirm` 仍可使用，但不是直接调用的前置步骤。保存的计划带 `upstream_profile`；历史缺省值为 `legacy-account-v1`，不会被新代码自动当成员工更新执行。同一 UUID 不能切换接口 Profile 重放。

## 结果判定

| 结果 | 含义 |
|---|---|
| 启停 `status=business_verified`、`business_effect_verified=true` | 同次前查、更新回执、目标状态回读和终态审计全部通过；只证明本次账号状态闭环 |
| 重置 `status=upstream_acknowledged`、`business_effect_verified=false` | 收到合法 ACK 并完成本地记录；没有证明密码生效、登录成功或密码交付 |
| `status=failed` | 认证、许可、配置、查询、写回执、回读或审计未通过；不能当作已完成启停或密码重置 |

新状态路径额外输出 `write_upstream_attempted`、`read_attempts`；成功一般为一次写、两次读，`observed_account_status` 是已核验回读的目标状态。`upstream_attempted` 表示本次进入适配器，`recorded_possible_dispatch` 是库中保守派发事实；它不等同于已经发出写 HTTP。前查失败或前查时撤权可以出现零次实际写但操作库仍保留 unknown，以免在崩溃/不确定状态下错误释放目标。

操作库 `succeeded` 的含义须结合计划 Profile：历史账号接口只表示 ACK；新状态 Profile 则在完成回读和终态审计后才成功。不能仅看这个数据库字段就推定密码效果或真实身份通过。

回读不符、派发后超时、HTTP 错误、非零业务码、异常响应、授权变更或审计失败均不自动重试；未知结果跨启用、禁用、重置保留员工和账号占用。三个请求共用固定总预算。调查使用原 UUID 和审计事实，不提供自动解锁/换库捷径。

## 保留的独立只读回读

```bash
# 对已有成功操作再次查询当前状态，不发送任何写入
.venv/bin/python scripts/live_write.py verify --operation-id UUID_FOR_OPERATION
```

`status` 子命令只读本地库；`verify` 与下文 `review` 才请求真实 T100。它根据原操作的企业、员工、账号绑定及当前许可，走原生 FastMCP 本人查询；要求唯一且工号全等，再将 `employee_status`（上游 status 的兼容字段名）与保存目标比较。查询 `account_no` 不用于自动绑定。

结果为 `verified` / `mismatch` / `failed`。启用回读 Y 或禁用回读 N 时可记录该次状态验证通过；重置即使状态相符仍不验证密码效果。`mismatch` 和 `failed` 返回退出码 1。回读不改变原操作终态，不重发写入、不释放未知锁；只证明查询时点状态，不证明状态从未被其他操作改变或角色/站点未变。

历史旧禁用曾通过 `user.account.create/gzxastus=N` 获得 ACK，随后真实查询与 `verify` 均回读 Y，与目标 N 不符。该历史结果仍为未验证禁用效果，原 ACK 和回读证据保留在私有运行目录；新员工状态路径的实测独立登记在 [v0.7验证](../../../VALIDATION-v0.7.md)，不追改历史记录。

## 未知结果的核查（当前工作区增量）

`status` 已按操作库输出实际可调用状态：原操作为 started/dispatched/unknown，或同企业的员工、账号被其他未决操作占用时，返回 `status=blocked`、`ready_for_explicit_call=false`、`blocker=reset_pending_review`，并列出 `blocking_operation_ids`。已完成 UUID 也不可重复执行，返回 `completed` 和 `reset_already_recorded`；库不可读或结构无效时失败关闭。`configuration_blocker` 与 `execution_blocker` 分开报告，不能用配置许可代替执行许可。blocked/failed 的退出码为 1。

`recorded_possible_dispatch` 表示原操作的历史派发记录；`upstream_attempted=false` 表示本次 status 没有调用 T100，两者并不矛盾。`show` 仍只预览配置和报文，不应作为当前无占用的证明；实际派发前始终由操作库事务再次校验。

```bash
# 关联原 unknown 操作，重新查询当前资料状态；只读，不重发原写入
.venv/bin/python scripts/live_write.py review --operation-id UUID_FOR_OPERATION
```

`review` 仅接受 `employee-status-v1` 的启用/禁用 unknown 操作，按原企业、员工、账号、调用身份及授权摘要与当前许可精确核对。它共用原生 FastMCP 本人查询链路，查询前后检查授权；一次查询、零次写入，不改操作库。密码重置、旧账号接口、尚在执行或已成功的记录不能使用此核查入口，成功记录继续用 `verify`。

核查成功返回 `status=reviewed`、`observed_account_status=Y/N` 和 `account_state_matches`，同时固定 `business_effect_verified=false`、`pending_review_retained=true`、`operation_state=unknown`。即使读回目标状态，也只证明当前状态，不能证明它由原请求造成或原请求不再可能执行。开始与结束证据保存在私有 `reviews/`，关联原 operation_id、查询 request_id、查询审计路径和观察时间；证据落盘失败不输出核查成功或员工状态。原操作和事件保持原样。

当前“禁用 unknown 后查询仍 Y”表示禁用效果未验收。继续处理需要接口维护方确认原请求的最终执行结果、是否仍有待执行任务及拒绝原因；只读 Y、改 UUID、换动作、删库都不能代替这个结论。本增量没有新增解除占用命令，也没有自动重试。当前工作区脚本增量独立验证，既有 v0.7.0 制品及其历史校验和保持原样。

## 私有配置和迁移

固定目录 `.runtime/live-write/`。CLI 不提供切换操作库目录的开关，三动作必须共用 `audit/account-write.sqlite3`；不得删除、另建或复制库规避未知记录。

| 配置 | 约束 |
|---|---|
| `plans/<UUID>.json` | 保存企业、工号、独立账号、动作、状态、Profile、上游配置摘要和账号存在确认；`enabled=false` 可停用，重复命令不能覆盖撤权 |
| `employee_update_omissions_preserved` | 新 Profile 必须为 true；默认 false。依据是维护方已确认省略员工资料字段保持 |
| `employee_status_updates_enabled` | 新 Profile 的独立运行开关，默认 false |
| `omitted_details_preserved` | 账号接口仍须确认省略角色/站点明细保持，不等同于员工字段保留确认 |
| `internal_test_write_enabled` | 所有写动作的共同开关，默认 false |
| `evidence_reference` | 必须非空，记录用户确认、维护方语义和文档来源，不只留下布尔值 |
| `allowed_targets`、企业与上游摘要 | 精确限定员工/账号、动作、状态及运行环境；新增工具不自动增加许可 |
| `legacy_status_writes_enabled` | 兼容模型默认 true；当前真实私有配置已设 false，禁止旧账号接口启停重发，保留历史查询和密码重置路径 |

`status-results/<operation-id>-<uuid>.json` 独立持久保存每次调用的读次数、写尝试、目标状态、成功时的观测以及原审计关联；取证失败不能宣称业务成功，不能仅凭内存结果报 `business_verified`。

以上契约位于 `contract.json`，源码默认配置不会自动启用真实写。私有签名材料与只读身份隔离；服务 key 在授权后从明确运行配置读取。真实资料、私钥、令牌、key、审计均不进源码、测试包或制品。

当前真实原 SYA1 库已从 v1 原子迁移到 v2，旧 4 条操作及事件逐行比对保留；新禁用后现为 5 条操作、1 条 unknown，未解锁或清理。共享操作库保持原 SYA1 标识。`prepare` 只接受严格匹配的 v1 模式，以原子事务迁移三动作约束；保留所有操作、事件及 unknown 占用。未知或损坏 schema 拒绝，不能删库“修复”。查询库与旧候选重置库仍拒绝混用。

## 离线验证和验收状态

主功能全量 1166 项通过（97.29 秒），Ruff、52 文件格式检查及依赖锁检查通过。之后新增诊断元数据的受影响回归单独登记在 [v0.7验证](../../../VALIDATION-v0.7.md)，不当作前次全量覆盖。真实状态更新仍未通过，代码 `biz-validated` 不改变其 unknown 与业务效果 false。

```bash
bash scripts/test_account_write.sh
# 新资料状态 CLI 的合成测试可单独运行
.venv/bin/python -m pytest -q tests/test_live_employee_status_write.py
```

测试覆盖两种状态、最小报文、前查存在性、逐条回执、精确回读、授权中途变化、独立 scope、默认关闭、三动作未知锁、v1 迁移、历史 Profile 防重放及旧重置兼容。动态合成密钥、合成员工和捕获 HTTP 的模拟上游独立于私有运行资料；不访问真实 T100。

代码/契约通过仅记录 `biz-validated`。真实状态写回即使成功，也不代替真实 IdP、两员工隔离、上游认证、密码效果等 `business-validated` 条件；GOV 登记、白名单、private endpoint 与两跳仍单独验收 `published`。历史 [v0.6.1](../../../VALIDATION-v0.6.1.md) 和 [v0.6](../../../VALIDATION-v0.6.md) 原样保留，不将旧通过数或旧制品作为 v0.7 证据。
