# T100 内部查询接入与历史记录

核对日期：**2026-09-30**。当前 BIZ-T100 **0.9.1** 使用一个 FastMCP 实例、一个 MetaMCP `sysmcp-t100` 服务登记，查询与账号维护通过两个 namespace／private endpoint 选择工具。数量仅限本工作空间的 T100 管理对象，不推断网关中 CORE 或其它既有服务的数量。本文先说明当前只读入口，再保留 0.9.0 的独立只读接入过程；两个阶段的登记、工具前缀和验证范围分别处理。

## 当前查询入口（0.9.1）

| 对象 | 当前值／范围 |
|---|---|
| MCP server | `sysmcp-t100`，UUID 以 [服务目录](../catalog/services/sysmcp-t100.toml) 为准 |
| 下游地址 | `http://host.docker.internal:8781/mcp`；一个 BIZ 服务承载四接口 |
| 查询 namespace／private endpoint | `sysmcp-t100-internal-query` |
| 查询端点唯一工具 | `sysmcp-t100__t100_employee_query` |
| 另一个账号端点 | `sysmcp-t100-internal-account`，仅暴露 `sysmcp-t100__t100_account_maintain`，引用同一服务 |
| 当前查询客户端 | BIZ 私有 `.runtime/service/client-gateway.json` |
| 网关协议 Profile | `2025-06-18` initialize，不自动回退；直连另按 `2025-11-25` 验收 |

查询 [namespace](../catalog/namespaces/sysmcp-t100-internal-query.toml)、[endpoint](../catalog/endpoints/sysmcp-t100-internal-query.toml) 和 [内部环境](../environments/internal/t100.toml) 保存当前去敏登记；完整单服务双端点关系见 [账号接入说明](t100-account-onboarding.md)。旧只读服务 `sysmcp-t100-internal-query` 已标 `retired`，被 `sysmcp-t100` 替代，旧服务 UUID 和 `sysmcp-t100-internal-query__` 前缀不能再当当前运行入口。查询 namespace／endpoint 名称保留，不表示旧 MCP server 仍在运行。

日常发现及查询命令的唯一指南为 [BIZ 测试说明§经 MetaMCP 查询](../../biz-t100/docs/testing.md#经-metamcp-查询)。`tools` 只做 MCP 连接、验签及工具发现，T100 请求数为 0；真实查询必须执行 `employee-query`。参数模型支持姓名、部门等检索，但当前 BIZ 下游主体只获准查询受控工号，`allow_search=false`，不能通过姓名自动选人写入。

当前共享下游客户端为 `metamcp-internal-maintenance`，静态 JWT 有查询和账号维护两个 scope；查询 namespace 只暴露查询工具，账号 namespace 只暴露账号维护。BIZ 原生验签并继续限定各动作及批准目标，白名单不替代业务授权。MetaMCP 2.4.22 不透传外部用户 JWT；GOV 的用户级 API Key 不天然绑定某一个端点，也不是员工身份。输出 `real_identity_verified=false`、`caller_identity_forwarded=false`，不能验收真实员工间隔离。

当前登记状态为 `composed`，`business_validated=false`、`published=false`。0.9.1 已有两个端点发现及跨端点直接调用拒绝等边界证据，见 [BIZ 0.9.1 验证](../../biz-t100/VALIDATION-v0.9.1.md)；真实只读两跳与直连对照来自下文 0.9.0 的历史证据，不将其描述为本次文档同步重新执行。本文仅核对文件和既有证据，没有连接网关或 T100。

两个端点共享最长一小时的 BIZ 连接 JWT。续签和统一 server 的 Bearer token 更新步骤见 [BIZ 运行说明](../../biz-t100/docs/service-runtime.md#短期连接令牌维护)，当前没有自动刷新。签名私钥、员工／账号映射、目标许可、T100 key 和业务审计仍归 BIZ；GOV 不保存其副本。总体规则见 [GOV 说明](../README.md) 与 [服务接入规范](service-onboarding.md)。

## 历史阶段：0.9.0 独立只读接入

以下段落保留该阶段原始过程，来源为 [0.9.0 去敏验证记录](../VALIDATION-internal-t100-20260930.md) 及 [已退役服务目录](../catalog/services/sysmcp-t100-internal-query.toml)。段落中的“现产品”“当前实际登记”“本次”和只读客户端、UUID、工具前缀，均指 0.9.0 当时情况；“写维护不加入”仅限定当次只读验证，不限制当前账号端点。管理 API 顺序是固定 2.4.22 的历史操作记录，不是要求重新创建现有对象。历史内容不追溯改写成 0.9.1 当前入口。

### 历史运行条件

- 现产品为MetaMCP2.4.22、revision32b6e9813acfc13d1013eb2452be1899d6654bb9，SDK1.16.0。
- 应用镜像digest为sha256:6d5e0cba4ffc4976069a980723c11042f993a978fe0f3071ba2ec8a91ebc3d41；PostgreSQL为sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea。Compose仍用浮动引用，核实当前digest不等于完成部署固定。
- 健康、管理登录页面可达；无管理Cookie的对象查询返回401。用户填写GOV私有admin-login.json后，登录与管理会话已验证，秘密不写本页。
- 容器host.docker.internal指向Docker宿主桥接接口，不能访问仅监听loopback的BIZ。BIZ查询需绑定可达的明确接口并接受正确Host；写维护不加入此网关测试。
- 本地产品源码工作树比运行版本新，不能用工作树forward_headers能力推定容器支持。

### 历史身份与工具范围

2.4.22使用登记的静态headers、OAuth令牌或bearerToken连接下游，不透传原客户端Authorization。本次只允许独立内部连接客户端，BIZ原生验签并限定批准查询目标；它不代表外部用户。GOV入口X-API-Key、BIZ资源令牌、T100管理员key三者分离。

登记只读运行单元，仅发现t100_employee_query；写接口、签名私钥、上游key、员工范围和业务审计不进入GOV。正式用户身份承载另行验证，不能因内部查询成功就签published或business-validated。边界见 [ADR-008](../../../docs/架构决策-008-T100常驻运行与网关测试身份.md)。

### 历史操作与顺序

管理认证使用受控Cookie；以下固定2.4.22产品HTTP tRPC地址接受普通JSON，并非承诺跨版本稳定API。全过程不直接改产品数据库。

| 顺序 | POST路径 | 必要字段／约束 |
|---|---|---|
| 1 | /trpc/frontend.mcpServers.create | 新独立name，type=STREAMABLE_HTTP，真实容器可达url，独立连接bearerToken，headers={}；不关联旧OAuth，不指定他人user_id |
| 2 | /trpc/frontend.namespaces.create | 新独立name及实际mcpServerUuids |
| 3 | /trpc/frontend.endpoints.create | 新独立name、实际namespaceUuid；enableApiKeyAuth=true、enableOauth=false、useQueryParamAuth=false、createMcpServer=false |
| 4 | /trpc/frontend.apiKeys.create | 新独立name；秘密只存私有受控文件，不输出或放URL |
| 5 | private endpoint MCP调用 | X-API-Key访问/metamcp/<endpoint>/mcp；initialize显式2025-06-18，核对实际工具前缀 |

创建前查询同名对象；只创建本次独立对象，不修改原有服务／namespace／公开端点。发生部分失败保存已创建对象ID，不重复造对象；回滚仅停用本次新对象。当前实际登记名称为sysmcp-t100-internal-query，UUID见 [catalog](../catalog/services/sysmcp-t100-internal-query.toml)。

发现后通过namespaces.refreshTools保存真实工具Schema映射；namespaces.updateToolStatus和updateServerStatus用于本次新对象的启停验证，完成后恢复ACTIVE。连接JWT通过现有mcpServers.update更新bearerToken，保持原UUID、名称、类型、URL和headers。续签命令见 [运行说明](../../biz-t100/docs/service-runtime.md)，本版不建立自动刷新机制。

### 历史验收与证据

先确认BIZ常驻直连正常，再登记服务、限定namespace和private endpoint，保存真实对象ID、地址引用、工具集合及协议。比较批准同一目标的直接／网关查询，外部身份与连接身份差异据实标明；错误入口key应让该请求触发的BIZ业务执行为0。服务／工具停用必须实际核对，不能用静态白名单代替。

实际两跳查询通过：唯一匹配资料与直连一致；网关只发现sysmcp-t100-internal-query__t100_employee_query。缺失／错误Key均401；工具或服务停用均tool_not_available，三类拒绝均为0次BIZ业务执行。工具和服务已恢复ACTIVE，续签后工具发现通过。

资源令牌由BIZ独立临时签发，最长1小时，到期须续签并更新本次登记的Bearer Token。GOV的X-API-Key不是员工身份；下游实际客户端是metamcp-internal-query。当前签收gov-registered和composed，保留真实个人身份、business-validated及published未通过。受控配置、认证材料与业务响应留私有.runtime，去敏证据见 [验证记录](../VALIDATION-internal-t100-20260930.md)。
