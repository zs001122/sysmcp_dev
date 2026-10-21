# T100 错误分类与排查

适用版本：0.5.0起。错误码描述本次调用发生的事实，不根据配置名`test`推断数据范围。用户已确认旧test入口与生产数据一致；“测试库没有同步”不能作为空结果的默认解释。

## 员工查询结果

| 结果 | 错误码 | 含义 |
|---|---|---|
| 业务码成功、员工列表为空 | `employee_not_found` | 此次接口查询未返回该工号资料；不等于证明业务系统中绝对不存在该员工 |
| 业务码成功、返回多条员工 | `employee_not_unique` | 结果不唯一，不能选第一条或筛选后冒充唯一成功 |
| 只有一条，但返回工号不等于授权工号或工号类型错误 | `upstream_employee_mismatch` | 拒绝交付其他目标数据 |
| JSON、必需结构、字段类型/长度、内容解码或大小不合约 | `upstream_contract_error` | 上游响应不符合当前适配契约 |
| 非成功业务码或未单独分类的HTTP错误 | `upstream_rejected` | 上游拒绝或报错；没有权威对照时不推断为查无人员或权限不足 |

这些结果都不是查询成功，不返回员工档案。原“恰好一条＋工号完全相等”约束保持。v0.4及更早的`upstream_contract_error`包含空结果、多条和错工号；新版本分开报告，旧审计不重写。调用方需要更新对新错误码的处理；未知码继续按失败处理，不自动重试。

工具调用失败正文保留`error`和`request_id`，增加固定安全中文`message`。本地命令也显示中文说明。姓名、真实响应片段、未知上游错误原文、token和key不拼接到错误消息中。

## 固定错误说明

下表与产品`biz_t100.errors.ERROR_MESSAGES`对应；其中重置码只适用于独立重置服务，并不表示已开放真实重置。

| 错误码 | 固定中文说明 |
|---|---|
| `unauthenticated` | 身份认证失败，令牌缺失、无效或不符合当前服务要求。 |
| `identity_not_enabled` | 当前身份未建立有效映射或已停用。 |
| `account_not_linked` | 当前身份尚未绑定执行此操作所需的账号。 |
| `permission_denied` | 当前身份或客户端没有执行此工具的权限。 |
| `policy_unavailable` | 授权映射或策略不可用，请检查受控配置。 |
| `credential_unavailable` | 上游管理员服务凭据不可用。 |
| `authorization_changed` | 调用期间授权或员工绑定已变化，本次结果不予返回。 |
| `business_adapter_not_ready` | 业务接口尚未满足启用条件。 |
| `upstream_unauthenticated` | 上游拒绝了服务认证，请检查管理员服务凭据。 |
| `business_forbidden` | 上游拒绝执行此操作。 |
| `upstream_unavailable` | 上游连接失败、限流或服务不可用。 |
| `upstream_timeout` | 上游调用超时，未自动重试。 |
| `upstream_rejected` | 上游返回失败状态或非成功业务码。 |
| `upstream_contract_error` | 上游响应格式、字段类型或大小不符合接口约定。 |
| `employee_not_found` | 上游接口执行成功，但本次查询未返回员工记录。 |
| `employee_not_unique` | 上游返回多条员工记录，无法确认唯一目标，未返回员工数据。 |
| `upstream_employee_mismatch` | 上游返回的工号与授权工号不完全一致，未返回员工数据。 |
| `journal_unavailable` | 审计或操作记录不可用，本次操作不能确认成功。 |
| `query_timeout` | 查询总时限已到，未返回员工数据，也未自动重试。 |
| `query_cancelled` | 查询已取消，结果未交付；请按请求编号核对审计。 |
| `query_failed` | 查询失败，请按请求编号检查服务与审计记录。 |
| `invalid_arguments` | 工具参数不符合接口约定。 |
| `tool_not_available` | 此服务未开放所请求的工具。 |
| `confirmation_required` | 密码重置必须明确确认后才能执行。 |
| `reset_already_recorded` | 此操作编号已有记录，不会重复执行重置。 |
| `reset_pending_review` | 此员工或账号存在待核对的重置操作，暂不允许再次重置。 |
| `reset_outcome_unknown` | 重置结果尚未确认，目标保持占用；请核对原操作，勿自动重试。 |
| `reset_timeout` | 重置处理超时，未自动重试；请按操作编号核对记录。 |
| `reset_cancelled` | 重置处理已取消；请按操作编号核对是否已调用上游。 |
| `reset_failed` | 重置失败，请按操作编号检查服务与操作记录。 |
| `runtime_configuration_invalid` | 服务运行配置无效，请检查受控配置。 |
| `service_unavailable` | 服务尚未就绪或当前处理容量已满。 |

## 身份与传输层

缺少、伪造、过期、错误受众/issuer或必要scope的令牌可由FastMCP原生认证层返回HTTP401/403，尚未进入业务工具，因此可能没有工具request_id。工具参数错误、未知工具也可能在工具正文执行前被拒绝；本地命令将已知固定码转成安全说明。不能仅根据HTTP200判断工具成功，应同时检查MCP isError及业务结果。

`upstream_attempted=true`表示业务用例进入了T100读取客户端，不证明远端收到请求；`remote_response_validated=true`表示收到并通过唯一目标校验。缺少终态审计时不能从开始记录推断未调用，命令保留null。网络超时或结果未知均不自动重试。

## 本地入口错误

| 错误码 | 说明 |
|---|---|
| `live_read_state_invalid` | 私有配置、映射、公钥、目录或权限不合约 |
| `live_read_token_invalid` | 本地签名材料或令牌文件不合约 |
| `live_read_service_not_ready` | 默认映射的常驻入口未就绪；先查看status |
| `live_read_state_changed` | 运行配置或公钥变动，需重启 |
| `live_read_service_invalid` | 当前入口仅支持只读 |
| `live_read_scenario_invalid` | 真实入口不能切换模拟上游场景 |
| `live_read_failed` | 未分类的本地装配/调用问题，需核对状态与审计 |

`call --employee-no`的非法工号由命令行在调用前拒绝；该命令只能由可读取本机私有配置和签名材料的操作者使用，不是给远程MCP调用者开放任意工号参数。
