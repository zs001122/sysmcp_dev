# T100账号启停与密码重置接入

核对日期：**2026-10-21**。适用已部署 BIZ-T100 **0.9.2**、MetaMCP **2.4.22**。本工作空间的 T100 当前只登记 `sysmcp-t100` 一个 MCP server；查询和账号操作使用两个 namespace／private endpoint 选择工具，均连接同一个 FastMCP 实例。此数量不涵盖网关中的 CORE 或其它既有服务。本页说明账号端点及其与统一服务的关系，不表示整个网关只提供账号维护。

日常业务命令的唯一指南为 [BIZ 测试说明](../../biz-t100/docs/testing.md)，服务配置、启动与连接令牌维护见 [BIZ 运行说明](../../biz-t100/docs/service-runtime.md)。总体职责及接入规则见 [GOV 说明](../README.md) 和 [服务接入规范](service-onboarding.md)。查询入口与早期证据范围见 [查询接入与历史记录](t100-internal-onboarding.md)。

## 当前登记与状态

| 对象 | 当前登记 | 工具范围／状态 |
|---|---|---|
| MCP server | `sysmcp-t100` | 登记允许查询与账号维护两个工具，`composed` |
| 查询 namespace／private endpoint | `sysmcp-t100-internal-query` | 仅 `sysmcp-t100__t100_employee_query` |
| 账号 namespace／private endpoint | `sysmcp-t100-internal-account` | 仅 `sysmcp-t100__t100_account_maintain` |
| 身份与正式发布 | 临时静态下游连接 | `external_user_forwarded=false`、`business_validated=false`、`published=false` |

实际 UUID、环境地址引用和白名单以 [服务目录](../catalog/services/sysmcp-t100.toml)、[查询 namespace](../catalog/namespaces/sysmcp-t100-internal-query.toml)、[账号 namespace](../catalog/namespaces/sysmcp-t100-internal-account.toml) 及 [内部环境](../environments/internal/t100.toml) 为准。两个端点均处于内部测试范围。2026-10-21 恢复原容器、部署 BIZ 固定 wheel 并更新原服务连接 token 后，两端点工具发现通过，T100 业务读写均为 0，见 [部署验证](../../biz-t100/VALIDATION-deployment-20261021.md)。

## 当前工具与范围

容器通过 `http://host.docker.internal:8781/mcp` 连接 BIZ-T100，共享下游连接客户端为 `metamcp-internal-maintenance`。**账号端点仅发现一个账号维护工具**，查询端点另有一个查询工具；共同服务登记允许两者，不按账号功能另建 MCP server。

下游静态连接 JWT 包含 `t100:employee:query` 与 `t100:account:maintain` 两个 scope，各 namespace 仅将对应工具设为 ACTIVE。BIZ 同时按验签身份、客户端、动作及批准目标执行授权；工具白名单不替代业务许可。员工新建／更新不通过该网关连接暴露。本机维护客户端属于另一主体，其当前可见工具为员工更新及账号维护，不能据此推定网关也可见。

同一个账号维护工具通过五个master字段完成三项操作：

| 操作 | gzxastus | password_reset | is_reproduce |
|---|---|---|---|
| 禁用登录账号 | N | N | N |
| 启用登录账号 | Y | N | N |
| 启用状态下重置密码 | Y | Y | N |

`gzxa003` 为员工号、`gzxa001` 为登录账号，须分别匹配 BIZ 批准的一组配对目标，不能默认相同。BIZ 的 `status-password` Profile 只接受一条 `master` 及这五个字段；不允许附加字段、复制、角色／作业／据点 `detail`（包括空数组）或禁用同时重置。省略 `detail` 保留既有资料的确认属于账号接口，不推广到员工更新或未定义复制规则。本文仅描述接口限制，批准目标、映射及执行许可仍留在 BIZ，GOV 不维护其副本。

## 现场接入与验证

- 使用受支持API完成单一服务登记和两namespace白名单；原查询／账号端点UUID与访问Key保留，更新其服务引用及工具前缀。验证后移除本chat被替代的两条服务登记，原对象文件留档。唯一服务UUID见 [服务目录](../catalog/services/sysmcp-t100.toml)，不改他人服务或产品数据库。
- 通过真实工具发现保存 Schema 映射；明确 `2025-06-18` initialize，不回退。账号端点只发现一个账号维护工具，查询端点只发现一个查询工具；`tools` 不调用 T100，发现通过不等于账号业务效果通过。
- 三份既有本人用例先由BIZ check_authorization核对，全部allowed；该核对不读取T100 key、不写审计、不调用上游。
- 三份请求实际经过MetaMCP调用BIZ，confirm=false均返回confirmation_required，验签客户端为metamcp-internal-maintenance；未调用T100。日常live.py也在缺--confirm时前置拒绝。
- 缺失／错误入口Key均HTTP401。账号namespace工具或服务INACTIVE时发现为空；绕过发现直接tools/call仍被拒，无新BIZ用例或业务审计，完成后恢复ACTIVE。查询端点直接调用账号工具、账号端点直接调用查询工具也被拒。

上述为 0.9.1 的既有接入验证，没有重复已经手测成功的账号写操作。2026-10-21 另验证两端点发现、实际下游 confirm=false 拒绝及缺失／错误 Key 的 401；没有重做 namespace 工具／服务启停，也没有执行真实 T100 业务读写。真实禁用、启用、重置使用 [测试说明](../../biz-t100/docs/testing.md#经-metamcp-执行本人账号操作) 中三条预填命令并显式 `--confirm`。账号 ACK 只表示 T100 成功回执，可记录操作 `succeeded`，同时 `business_effect_verified=false`；实际启停、密码或权限效果仍须账号资料或登录证据。员工档案的 `status` 与登录账号 `gzxastus` 属于不同对象，员工查询不能代替账号效果确认。

## 身份及令牌

私有 `client-gateway-account.json` 与查询 `client-gateway.json` 分别配置，入口 Key 分别维护。MetaMCP 现版 Key 是用户级访问凭据，不天然绑定单一 endpoint；分配两个 Key 不等于实现两种员工业务权限。端点工具范围由 namespace 白名单控制，业务限制由 BIZ 的受控连接主体、scope、目标和 `status-password` 策略实施。

两个端点共享下游连接 JWT，最长 1 小时；只为查询＋账号维护、`status-password`、禁止宽泛检索且目标非空的启用主体签发，不包含员工建档／更新 scope。续签由 BIZ 进行，再通过 MetaMCP 受支持界面／API 更新统一 server 的 Bearer token；只更新私有 JWT 文件不会自动更新网关。完整步骤唯一维护在 [BIZ 运行说明](../../biz-t100/docs/service-runtime.md#短期连接令牌维护)，当前未实现自动刷新。本次已更新令牌的到期时间为 2026-10-21 14:50:53 +08:00，不能据本页或旧发现结果判断未来仍有效。

GOV 入口 Key、BIZ 资源 JWT 和 T100 服务 key 保持分离。签名私钥、T100 凭据、员工／账号映射、目标范围、授权策略和业务审计不进入 GOV；GOV 仅维护已准入服务、环境引用、协议、namespace、endpoint、白名单及组合证据。MetaMCP 2.4.22 不透传外部用户 JWT，下游静态客户端不代表真实员工身份，输出 `real_identity_verified=false`、`caller_identity_forwarded=false`。正式 IdP、`business-validated` 与完整 `published` 尚未验收。

统一维护入口已记录为 [DEC-001](../../../docs/项目讨论与决策记录.md)，尚未实现；该需求不表示当前已能跨层自动同步身份、映射和网关配置。

当前部署见 [2026-10-21 验证](../../biz-t100/VALIDATION-deployment-20261021.md)，历史接入见 [BIZ0.9.1](../../biz-t100/VALIDATION-v0.9.1.md)，边界依据见 [ADR-009](../../../docs/架构决策-009-T100账号操作网关接入.md)。
