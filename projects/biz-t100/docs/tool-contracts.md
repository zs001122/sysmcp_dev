# BIZ-T100 四工具公开契约

适用版本及当前固定 wheel 部署：**0.9.2**；核对日期：2026-10-21。本次部署未改变公开契约。日常命令统一见 [测试说明](testing.md)，上游字段见 [四接口契约](upstream-interfaces.md)，部署及连接令牌维护见 [常驻运行说明](service-runtime.md)。本页描述产品公开能力，实际调用者只能使用已批准的子集。

## 工具、输入与权限

| FastMCP 工具 | request 类型 | scope | 必要输入 |
|---|---|---|---|
| t100_employee_query | EmployeeQueryRequest | t100:employee:query | 工号、姓名或其它明确查询条件；至少一项 |
| t100_employee_create | EmployeeCreateRequest | t100:employee:create | employee[] 中工号、全名、部门、员工档案 status |
| t100_employee_update | EmployeeUpdateRequest | t100:employee:update | employee[] 中工号及至少一个修改字段 |
| t100_account_maintain | AccountMaintainRequest | t100:account:maintain | master[] 中账号、员工号、gzxastus、is_reproduce、password_reset |

每个工具均接收 request；三个写工具另接收 confirm，默认 false。额外字段拒绝；字段文件与快捷命令参数互斥。没有工具调用参数可以指定上游地址、服务凭据或调用者身份。账号复制和角色／据点明细通过现有 account_maintain 请求表达，不增加工具。

EmployeeQueryRequest 的产品契约允许零条或多条候选；当前常驻查询许可禁止宽泛搜索，要求显式查询批准范围内的工号，并核对返回工号。写入前查及员工写后回读必须恰好一条、没有下一页且工号全等。企业和员工号一起标识对象。员工 status 属于档案；登录账号启停使用 gzxastus。账号与工号独立指定，姓名只作检索或展示。

## 一个服务中的工具范围

当前 `maintenance` 模式在一个 FastMCP 实例装配四工具，GOV 登记一个 `sysmcp-t100` MCP server。工具发现取**部署动作、可信 JWT scope、服务端已启用主体动作**的交集；执行时还检查企业、目标范围、账号 profile 及既有部署许可。直接指定工具名不会绕过这些检查。

| 当前连接 | 可发现／调用的工具 | 额外边界 |
|---|---|---|
| 本机查询 `client-read.json` | `t100_employee_query` | 仅批准员工的显式工号查询 |
| 本机维护 `client-write.json` | `t100_employee_update`、`t100_account_maintain` | 沿用既有批准目标；当前没有员工建档许可 |
| 网关查询 endpoint | `sysmcp-t100__t100_employee_query` | 查询 namespace 白名单 |
| 网关账号 endpoint | `sysmcp-t100__t100_account_maintain` | 账号 namespace 白名单及 BIZ `status-password` profile |

`tools` 只是 MCP 工具发现，不读取员工资料、不调用 T100；看到一个工具不表示服务仅实现一个接口。员工新建及完整账号维护已实现，但不因源码存在就对当前客户端开放。

## 同一个账号工具的三种内部网关操作

当前网关账号主体只允许批准配对目标的五字段请求：`gzxa003`（员工号）、`gzxa001`（登录账号）、`gzxastus`、`is_reproduce`、`password_reset`。

| 操作 | gzxastus | is_reproduce | password_reset |
|---|---|---|---|
| 禁用登录账号 | N | N | N |
| 启用登录账号 | Y | N | N |
| 在启用状态下重置密码 | Y | N | Y |

这三个动作共用 `t100_account_maintain`／`user.account.create`。网关 profile 拒绝复制来源、`detail`（包括空数组）、附加字段和禁用同时重置；当前配对目标范围仅支持已批准单条请求。本机 `full` profile 保留文档定义的批量、复制及角色／作业／据点明细能力，仍须独立许可。密码重置参数不是新密码，接口不返回新密码，也不在本地猜测重置后的密码规则。

## 执行边界

FastMCP 原生 AuthProvider 验证令牌签名、issuer、audience 与有效期；BIZ 的 `current_caller` 从验签上下文进一步核对 subject、客户端、有效期及所需 scope。FunctionService 在业务执行前核对企业、动作、目标和本次意图许可；管理员服务凭据在许可通过后读取。本机开发入口使用固定签名主体 `temporary-local-operator`，不是员工号或普通 Header；每次生成临时签名材料，继续原生验签并按既有目标许可执行。执行期间重新核对身份与许可，变更后不交付成功数据。

员工更新输入表达修改意图。服务精确前查并按白名单保留其它已知字段，生成有效报文、留证、调用上游，再回读修改和保留字段。显式空字符串表示清空；没有取得原值时不猜测。白名单和不能自动保留的字段见 [上游契约](upstream-interfaces.md)。

账号维护先确认员工档案存在，再发送一次账号请求。gzxastus、is_reproduce、password_reset 均显式提供；不隐式启用、复制或重置。省略 detail 的保留约定不等于空数组会保留。

## 结果与审计

| 字段／状态 | 含义 |
|---|---|
| request_id / action / enterprise_id | 本次请求与业务动作 |
| business_object / upstream_service | 工具业务结果中的 employee_record 或 user_account，以及实际 T100 服务名；认证或参数前置拒绝可能没有这些字段 |
| upstream_attempted / read_requests / write_requests | 实际请求尝试及计数 |
| passed | 检索请求完成，允许空结果或候选列表 |
| verified / business_effect_verified=true | 员工提交及应保留字段回读一致 |
| acknowledged / business_effect_verified=false | 成功回执；账号效果没有自动核实，或员工提交包含未验证日期字段，具体缺口见 verification |
| failed / error | 固定错误码；失败不包含未经筛选的上游自由文本 |

写前开始记录失败，业务请求为0；写入后终态记录失败，不交付成功。请求编号用于防止同一次操作再次派发，不是跨请求业务幂等键；当前同一成功业务报文再次提交仍是新操作。未知占用与编号防重已实现，跨请求幂等方案未实现，正式写发布前按 [开发清单](development-checklist.md) 闭合。当前审计库使用 SYF1，保存可信调用者、企业、动作、目标、请求摘要和事件；业务报文、返回资料及凭据不放在审计表中。历史库只读，原记录和事件不改写。

同账号自身的未决写入仍阻止后续账号维护，不自动重试。当前库纯员工更新 unknown 属于员工档案，账号维护不因员工号相同而误占；在途员工写入和建档未决仍阻塞。旧记录只能通过明确企业、目标、编号及完整事件指纹分类，不能按旧工具名称猜测。错误目录见 [错误说明](error-codes.md)。

## 公开装配与兼容范围

公开四接口工厂为 `create_function_server`／`create_function_app`，显式注入 AuthProvider、IdentityProfile 和 FunctionService。当前常驻装配为 `create_service_runtime_app(ServiceRuntimeConfig)`，使用 FastMCP 原生 JWTVerifier、服务端许可过滤、既有审计及 HTTP Host／Origin 检查。源码位于 [functions.py](../src/biz_t100/functions.py)、[service_runtime.py](../src/biz_t100/service_runtime.py)，参数类型位于 [interfaces.py](../src/biz_t100/interfaces.py)。产品包不导入 scripts 或 tests。

`live.py` 不带客户端配置时保留本机 ASGI 装配；带 `--client-config` 时经 Streamable HTTP 调用常驻服务或 MetaMCP。两者使用临时签名测试身份，`real_identity_verified=false`；直连明确使用 `2025-11-25`，MetaMCP 兼容 Profile 为 `2025-06-18`，不自动回退。

MetaMCP 2.4.22 不透传外部调用者 JWT。两个 namespace 共用仅查询＋限定账号维护 scope 的受控静态连接主体，不能代表外部个人。网关 API Key 属于用户级访问凭据，不天然绑定某个 endpoint；各 endpoint 工具白名单和 BIZ 授权分别执行，不能把配置了不同 Key 当成真实员工权限隔离。

原本人查询／重置工厂继续兼容并参与对应离线回归，不是当前四接口常驻服务的日常入口；授权来源仍是可信主体映射，不能与操作员维护身份互换。其旧契约见 [历史快照](history/20260930/tool-contracts.md) 和 [原本人重置说明](password-reset.md)。当前离线及制品见 [0.9.2 验证](../VALIDATION-v0.9.2.md)，现行部署见 [2026-10-21 验证](../VALIDATION-deployment-20261021.md)；[0.9.1 验证](../VALIDATION-v0.9.1.md) 保留为历史接入证据；相关离线通过支持 `biz-validated`，真实身份缺口仍使 `business-validated` 与完整 `published` 未签收。新增系统按独立 BIZ 项目接入。
