# BIZ-T100 当前错误与结果说明

适用版本及固定 wheel 部署 **0.9.2**；核对日期 **2026-10-21**。机器码与中文提示以 [固定错误表](../src/biz_t100/errors.py) 和 [live.py](../scripts/live.py) 为准。日常命令统一见 [测试说明](testing.md)，连接与审计排查见 [运维说明](operations.md)。

0.9.2 没有新增业务幂等键或错误码；当前请求编号防重和未知占用的含义保持，不能据错误表判断业务幂等已实现。现行部署见 [2026-10-21 验证](../VALIDATION-deployment-20261021.md)，交付验收见 [开发清单](development-checklist.md)。

## 正常结果

| 状态 | 判定范围 |
|---|---|
| passed（tools） | MCP 连接、验签及工具发现完成；T100 请求数为 0 |
| passed（employee_query） | 查询请求完成；通用检索可合法返回空列表或多条候选，仍核对匹配数和目标 |
| verified | 员工提交字段及保留字段回读一致，business_effect_verified=true |
| acknowledged | 成功回执但尚有业务效果未验证；账号效果通过账号资料或实际登录确认 |

维护检索不把零条结果一律报错；精确本人查询、写入前查和写后回读才要求唯一目标。员工档案 status 不证明登录账号启停。

当前常驻查询主体仅可查询批准工号，`allow_search=false`；“通用检索合法”不表示该主体已获姓名／部门检索许可。员工请求含 `create_date` 或 `modify_date` 时，当前未验证其精确往返语义；其它字段回读通过也可能只返回 `acknowledged`，核查 `verification[].unverified_fields`，不能把它等同所有字段验证通过。账号 ACK 始终不证明密码或登录状态生效。

## 参数和运行配置

| 错误码 | 含义 |
|---|---|
| empty_identifier | 工号或账号为空；常见原因是未赋值的 Shell 变量 |
| invalid_arguments / invalid_request | 参数类型、必填项或选定接口不合约；文件与快捷参数不能混用 |
| confirmation_required | 写操作缺少显式 confirm |
| write_permission_not_configured | 本地真实写入开关尚未开启 |
| permission_denied | 当前主体、客户端、动作、员工／账号目标或请求内容不在许可范围 |
| tool_not_available | 客户端选定工具不在目标服务／端点当前发现列表中；核对实际完整工具名及主体许可 |
| policy_unavailable / runtime_configuration_invalid | 私有运行配置、许可、文件权限或配置绑定不合法 |
| credential_unavailable | 服务凭据不可用 |

上述错误发生于调用前时以 `upstream_attempted=false` 标识；运行期重新读取配置或凭据也可能失败，应以实际调用标记及审计判断。`--confirm` 只表达写入意图，不替代主体、动作和目标许可。维护客户端即使带有员工新建 scope，服务端未授予动作时仍不会发现或执行该工具。

## 身份和唯一目标

| 错误码 | 含义 |
|---|---|
| unauthenticated | 签名、有效期、issuer、audience、客户端或 scope 校验失败 |
| identity_not_enabled / account_not_linked | 原本人服务的身份映射停用／缺失，或未受控绑定账号；与操作员维护许可分开 |
| authorization_changed | 调用期间身份、映射或许可变化，结果不交付 |
| employee_not_found | 精确核对没有员工记录 |
| employee_not_unique | 精确核对返回多条或还有下一页 |
| upstream_employee_mismatch | 精确核对返回工号不同 |
| enterprise_mismatch | 请求企业不等于受控企业 |

## 上游和审计

| 错误码 | 含义 |
|---|---|
| upstream_unauthenticated / business_forbidden | 上游服务认证或执行权限拒绝 |
| upstream_rejected | 上游业务码或逐条回执失败 |
| upstream_contract_error | 上游结构、类型或大小不合约 |
| upstream_verification_failed | 回执后员工字段回读不一致 |
| upstream_unavailable / upstream_timeout | 连接、限流或超时；不自动重复调用 |
| journal_unavailable | 开始或终态审计／证据记录失败，不报告成功 |
| reset_already_recorded | 编号已存在，本次不重复发送 |
| reset_pending_review | 目标存在相关未决写操作，需要核查原结果 |
| operation_failed | 四接口执行发生其它未分类失败；按原请求及调用事实核查 |
| mcp_call_failed / protocol_mismatch | 原生 MCP 返回或协商不符合当前约定 |
| service_unavailable | 服务未就绪或处理容量已满，不代表 T100 已拒绝业务 |

远程 `mcp_call_failed` 若 `mcp_tool_call_attempted=true` 且未取得有效响应，上游字段可能省略，不能据此判为 T100 请求数 0。应查服务端原请求及审计，不自动重发写入。直连默认协议 `2025-11-25`，MetaMCP 为 `2025-06-18`，`protocol_mismatch` 不通过自动回退消除。

已经尝试写入后，完整逐条回执明确全部拒绝才记 `failed`；超时、混合结果、写后回读不一致或审计失败保留未决事实。账号成功 ACK 可以记录 `succeeded`，同时 `business_effect_verified=false`，不因缺少登录证据一律改成 `unknown`。历史员工档案 `unknown` 按核验的业务对象分类处理，不能改写成账号操作，也不能解除账号自己的未决占用。错误诊断只允许固定安全字段，不输出服务 key、令牌或未经筛选的文本。

旧 live_read_* 错误仅适用于退役入口，参见 [历史错误目录](history/20260930/error-codes.md)。历史编号和错误不因本次文档整理改写。
