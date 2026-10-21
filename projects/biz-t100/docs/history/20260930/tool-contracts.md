# T100 工具契约索引与本人查询契约（查询错误契约 v0.5 / 制品 v0.5.0）

开发基线：Python 3.12、FastMCP 4.0.10、CORE 0.1.0 wheel。来源和范围见 [RU-001](../../../../../docs/reuse/RU-001-t100本人档案.md)，当前运行准备结果见 [运行准备验证](../../../VALIDATION-runtime.md)，历史授权结果见 [服务账号验证记录](../../../VALIDATION-service-account.md)。这是已通过离线验证的开发契约，真实业务与网关仍未验收。

## 0. 当前工具与部署边界

v0.5将查询空结果、多条和工号不一致拆分为独立错误码，并增加安全message，详见[错误说明](../../error-codes.md)。v0.4已新增独立写工厂中的 `reset_my_t100_password(confirm, operation_id)`。其输入、授权、重复提交和未知结果规则见 [本人密码重置契约](../../password-reset.md)。同一项目不表示同一进程自动暴露所有工具；`create_server/create_app/create_runtime_app` 仍只有查询，`create_reset_server/create_reset_app` 只有重置。重置没有真实运行入口，原查询工具不获得写权限。

## 1. 查询入口的唯一工具与返回

`get_my_t100_employee_record()` 无业务参数。任意非空参数拒绝为 `invalid_arguments`，不回显参数内容。显式注册一个只读工具，不注册 OA、写入、通用 HTTP 或 SQL 工具。

```json
{
  "request_id": "本次服务生成的UUID",
  "data": {
    "system": "t100",
    "employee_id": "E001",
    "display_name": "示例员工",
    "department": "D001",
    "employee_status": "原接口状态值",
    "observation": "employee_record"
  }
}
```

`employee_id` 来自受信映射，1—128字符，首字符为字母或数字，其余允许字母、数字、下划线、点、冒号、@和连字符。姓名、部门、员工状态可空，长度分别不超过256、128、64。无登录 `account_id/account_status`；状态枚举意义待接口方说明，不自动解释为可登录。未列字段不返回。

这是详情工具，不提供分页和自动遍历。上游列表须恰有一条且工号精确匹配。无记录、多条、错工号或格式不符均拒绝；尚无权威“不存在/无权限”业务码对照，不能猜成空成功。

## 2. 身份与授权接口

原生 `AuthProvider` 负责访问令牌的签名、类型、issuer、audience、有效期与必要scope验证。生产Provider必须确认访问令牌用途及有效性/撤销策略，不能只验签就把任意签名令牌当员工身份。服务再次要求单一资源audience、明确sub/azp、原生subject/client_id与声明一致、未过期、允许的azp，以及 `t100:employee:read:self` scope。

`Caller(issuer, subject, client_id)` 只来自原生已验证令牌上下文。Header、工具参数、`_meta`、姓名和网关API Key都不能设置员工身份。这里的client_id是当前已验证令牌中的客户端，尚不证明跨网关的原始客户端来源。

`EmployeeAccess.authorize(caller) -> EmployeeGrant` 采用 [ADR-003](../../../../../docs/架构决策-003-T100服务账号授权.md) 已明确的 `service-account` 模式。上游是开发人员维护的现有接口服务，使用服务级管理员key；不要求每个员工持有T100凭据或OBO。BIZ负责员工层的工具和本人范围授权，不能把服务管理员权限授予所有调用者，也不宣称继承原员工ERP ACL。

具体实现为 `FileServiceAccountAccess(policy_file, credential_file)`，构造只接收两个显式绝对路径、不执行I/O。每次授权：

1. 重新读取受控策略，要求显式 `auth_mode="service-account"`、非空revision、唯一 `(issuer, subject)` 条目。按可信Caller定位主体，检查enabled、allowed_clients与allow_employee_read；两个开关缺省均false。
2. 仅在上述规则通过后读取服务key；缺映射/停用拒绝为identity_not_enabled，客户端/工具不允许为permission_denied，未授权者不读取key。
3. 返回该条目的employee_id、策略原字节SHA-256作为binding_version，以及不参与repr/dump的SecretStr。相同服务key允许支持不同员工，但每次只能发送各自已授权工号。
4. 策略及key只接受当前进程用户拥有、无组/其他访问权限的普通单链接文件，拒软链接及软链接祖先；策略上限1,000,000字节，key上限16,384字节。配置文件与key分开；建议400/600权限，UTF-8 key非空且无内部控制字符。
5. 配置和凭据由维护方原子替换；每次重读，无永久缓存或旧key回退。策略变化即使revision未改也会改变binding_version，触发在途改绑重验。key轮换不改变本人授权；实际撤销效果仍由原接口验证。

最小策略格式见[禁用状态示例](../../../config/access.example.json)。它不含真实用户或凭据，不能通过复制示例获得权限。首次、耐久登记后、上游返回后分别调用authorize；异步等待后仍检查令牌和总截止时间。AuthProvider负责Token类型、有效性和撤销；本地策略负责业务启用/撤权，两者不能互相替代。

入口Token不发送给T100。原系统继续检查服务账号及接口操作的实际限制；用户级拒绝发生在BIZ。该实现不提供跨系统原子授权或员工原权限等价证明。真实映射、服务key交付、身份有效性和网关来源仍需运行验收，但服务管理员权限模式已知。

## 3. 业务客户端接口

`T100EmployeeClient.read(grant) -> EmployeeRecord`：默认向固定HTTPS URL POST，`service.name=employee.list.data.query.get`，企业号为 `datakey.EntId`，目标为 `payload.std_data.parameter.employee_no`。

保留经评估的 `type=sync`、`host.prod/ip/lang/acct/timestamp`、`service.prod/ip/id`、`is_digiwin_product=N`、`use_has_next=false`。`host.acct`是服务报文字段，不代表员工委托；实际字段、时区及key发放/轮换方式由接口维护方定标。非秘密配置格式见 [示例](../../../config/t100.example.json)，其中没有key或旧环境地址。

客户端构造时重新验证配置。URL禁止userinfo、query、fragment及控制字符；默认HTTPS验证TLS，不跟随重定向，不读取环境代理。用户已确认旧HTTP测试入口，可显式配置 `transport_profile="http-test"`，仅允许RFC1918私网IPv4字面量的固定HTTP目标；无自动降级，不支持hostname、loopback、link-local或公网HTTP。测试传输不提供加密，也不证明原接口认证通过，见 [ADR-005](../../../../../docs/架构决策-005-T100测试接入与运行准备.md)。只使用异步HTTP，不重试，响应上限2,000,000字节。

## 4. 错误规范

| 场景 | 固定结果 | 请求事实 |
|---|---|---|
| 缺Token/签名、issuer、audience、scope错误 | 原生HTTP 401/403 | 上游0次 |
| 员工声明不合约/令牌过期 | `unauthenticated` | 派发前为0次 |
| 未映射、越权、改绑 | `identity_not_enabled`、`permission_denied`、`authorization_changed`等固定码 | 派发前为0次；返回后重验失败不披露数据 |
| 策略损坏/模式错误/不可读取 | `policy_unavailable` | 0次；不读取key |
| 获准调用者的服务key缺失/不可读取 | `credential_unavailable` | 0次；不回退旧key |
| 非空业务参数/未知工具 | `invalid_arguments` / `tool_not_available` | 0次 |
| 前置记录失败 | `journal_unavailable` | 0次 |
| 后置记录失败 | `journal_unavailable` | 已发生调用，不返回业务成功 |
| T100 HTTP 401/403 | `upstream_unauthenticated` / `business_forbidden` | 1次 |
| HTTP429/5xx或连接失败 | `upstream_unavailable` | 尝试至多1次 |
| 其他非2xx、未知非0业务码 | `upstream_rejected` | 不猜T100业务含义 |
| 成功业务码但员工列表为空 | `employee_not_found` | 已发生调用，不返回空成功 |
| 员工列表多于一条 | `employee_not_unique` | 已发生调用，不选第一条 |
| 返回工号不完全相同 | `upstream_employee_mismatch` | 已发生调用，不返回错目标 |
| 响应形状、字段类型、长度、解码或大小错误 | `upstream_contract_error` | 已发生调用 |
| HTTP或工具总预算到期 | `upstream_timeout` / `query_timeout` | 可能已调用，不自动重试 |
| 未分类内部异常 | `query_failed` | 根据记录核对，不回显原文 |

工具错误使用原生ToolError，调用用例的失败正文含固定error、安全message及request_id；前置参数/工具拒绝只含固定码。原始上游消息、Token、凭据和业务响应不放进错误。协议错误交给原生框架处理，不另写JSON-RPC解析器。

## 5. 截止时间与查询记录

连接上限3秒；工具业务预算10秒覆盖授权、记录、HTTP和结果重验，HTTP自身另有10秒总预算。每次等待返回后检查绝对截止时间与令牌，派发和交付前检查取消，预算到期不再开启新业务请求或返回成功数据。取消释放HTTP资源，无后台重试。

`QueryJournal.record(...)` 返回表示耐久提交完成；白名单字段包括request_id、时刻、可信issuer/subject/client、enterprise_id、员工号、固定tool_name、事件、upstream_attempted和固定错误码。不得保存凭据、原始参数、完整响应或未知异常。

进入EmployeeReader前设置 `upstream_attempted=true`，终态持久化该事实；它表示已进入业务客户端调用，不保证上游收到请求。派发前拒绝为false。只有开始而无终态时，调用事实仍需核对，不能从开始事件中的false推断未派发。开始前提交 `query_started`，结果校验及授权重验通过后提交 `query_succeeded`；错误/取消尝试提交 `query_failed/query_cancelled`。超时/取消后设置额外1秒本地清理预算，不重复业务请求。SQLite提交不能中途抛弃工作线程，因此磁盘故障下的清理不是硬实时响应保证。

成功终态说明上游结果在最后一次授权检查后通过校验并已提交记录，不证明客户端收到响应。若提交结束时已过期/超时，仍拒绝交付数据，保留成功终态，不插入矛盾的第二终态。同一次调用最多派发一次；客户端重新调用会产生新request_id，本服务不声称只读请求跨调用去重。

`SQLiteQueryJournal` 使用独占新数据库：0700私有目录、0600文件、独立应用/Schema标识、FULL同步、固定企业/工具上下文和事件表；拒旧库、错误权限及符号链接。构造不做I/O，首次记录或显式 `prepare()` 创建；创建目录和数据库时同步相应目录项。不宣称防篡改、高可用、分布式事务或断电演练通过。

Schema v2新增调用尝试事实及企业/工具上下文，拒写v1库，不静默迁移。新实例绑定配置enterprise_id，不能复用另一个企业的库。当前无真实部署数据迁移；历史v1库保留并用对应历史制品调查。`QueryJournal.record`在v0.3新增 `upstream_attempted` 关键字，外部实现需同步接口；不兼容实现失败关闭，不能当作已适配。

`check_ready()`只读核对既有库及Schema，不创建、不扫描事件；`prepare()`不插入虚构业务事件。`pending_request_ids()`只读列出有开始无终态的请求，供人工核对，不能推断请求未发送，也不会重放。认证拒绝发生在工具前，需要入口侧单独记录；本SQLite不宣称覆盖所有HTTP拒绝。当前BIZ-T100制品与部署配置标注service-account执行模式，单次事件记录真实调用员工和目标；不得将subject字段解释为上游登录账号。

## 6. 运行接口、协议、扩展与修改

v0.3新增 `biz_t100.runtime` 的 RuntimeConfig、load_runtime_config、check_configuration、create_runtime_app；当前具名接口及README列出的工厂、端口和组件属于公开装配契约，内部下划线方法和数据库表不属于公共接口。配置不兼容变化需版本升级和迁移说明；未知工具错误按失败处理，不自动重试或放宽授权。

运行工厂要求显式AuthProvider，启动时检查配置并准备独占审计库，失败拒绝启动。健康 `/health/live` 只表示进程存活，`/health/ready` 仅核对本地配置、凭据元数据和库Schema；均不读取key内容或查询T100。单进程在途MCP请求默认最多4，配置范围1—32；超限、未就绪或关闭时HTTP503，尚未执行工具，不产生业务请求。健康响应不证明个人身份、上游或真实业务通过。部署只使用一个worker；完整运维约定见 [operations.md](../../operations.md)。

`create_app`生成原生 `/mcp` Streamable HTTP、stateless和JSON response应用，不启动监听。`2025-06-18 initialize/list/call`与`2026-07-28 list/call`已单独离线验证；都不代替MetaMCP两跳、员工身份或真实业务验收。

改变员工编号、状态、凭据受众、授权方式或工具契约前，更新本文件、RU-001受影响项、版本及定向测试。不在本服务增加OA/CRM的配置、凭据或业务分支，按 [ADR-002](../../../../../docs/架构决策-002-首个T100服务与扩展.md) 独立接入。

框架依据：[FastMCP原生令牌验证](https://gofastmcp.com/servers/auth/token-verification)、[请求上下文](https://gofastmcp.com/servers/context)；实际接口同时按锁定4.0.10安装包及本轮测试核对。

## 7. 本地指定工号测试

用户已授权`live_read.py call --employee-no`作为本地操作者入口。它在私有目录生成独立临时主体映射及审计，以原生ASGI HTTP认证链路调用相同工具和真实T100客户端；工具参数仍为{}，不改变远程本人查询接口。默认call仍调用常驻8768的默认映射。两者分别记录传输与测试模式，不把ASGI结果当作端口或网关验收。
