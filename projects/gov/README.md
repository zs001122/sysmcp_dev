# GOV：MetaMCP 集成项目

文档核对日期：2026-09-30；当前 T100 制品版本：**0.9.1**。短中期采用 **MetaMCP 2.4.22**，维护现成产品的服务登记、聚合、namespace、endpoint、工具启停和访问控制。ContextForge仅保留后续评估记录，不进入当前实施。

本目录是 MCP 体系的统一控制面，保存产品选择、服务目录、发布关系、环境地址引用和去敏组合证据。本工作空间管理的 **T100 登记为一个 `sysmcp-t100` MCP server、两个 namespace 和两个 private endpoint**，共用同一个 FastMCP 服务。账号禁用、启用和密码重置共用一个账号维护工具。这里的数量只描述本目录管理的 T100 对象，不能推断整个 MetaMCP 实例的服务数量。

当前内部登记与组合验证已有证据，正式个人身份、账号效果及完整发布验收仍待完成。项目组织与状态见 [GOV 项目梳理](docs/project-overview.md)，当前查询／账号接入见 [T100 接入说明](docs/t100-account-onboarding.md)，各阶段运行记录见 [部署记录](部署记录.md)。

控制面与业务负载的正式边界见 [ADR-004](../../docs/架构决策-004-GOV统一服务注册与发布.md)，新增模块的通用约束见 [模块边界与接口约束](../../docs/模块边界与接口约束.md)。

## 部署基线

选型镜像基线为 `ghcr.io/metatool-ai/metamcp:2.4.22`，源码commit见 [selection.toml](selection.toml)。该文件是2026-09-29选型记录，其中待验收状态不覆盖后来取得的 T100 组合证据。运行版本、revision及应用和数据库digest已在2026-09-30记录中核对；Compose引用仍为浮动值，重新部署的制品固定尚未完成。本轮文档整理没有重新部署或现场验收这些条件。

官方部署由MetaMCP应用和PostgreSQL组成，应用包含UI与backend。目标隔离基线为独立端口、网络、数据库、卷和Secret；数据库不发布宿主端口，应用首次只绑定loopback。现有实例的端口发布与此基线存在差异，见部署记录；此处是目标要求，不是已生效声明。上游启动会自动执行数据库迁移，不能把启动视为只读操作。

| 配置项 | 接入前应核对的目标要求 |
|---|---|
| POSTGRES_HOST/PORT/USER/PASSWORD/DB、DATABASE_URL | 独占数据库与凭据，二者保持一致 |
| APP_URL、NEXT_PUBLIC_APP_URL | 一致的实际访问地址 |
| BETTER_AUTH_SECRET | 独立随机密钥，真值不入库 |
| TRANSFORM_LOCALHOST_TO_DOCKER_INTERNAL | 若使用明确容器DNS，显式false，避免地址隐式改写 |
| OIDC_* | 可选；本阶段不要求部署身份平台 |

既有部署位于 `/data/projects/metamcp`，本目录只保留去敏部署记录和引用；本轮不复制、读取或修改其真实.env，也不重建Compose。官方 `example.env` 作为配置语义参考，接入前核对上述必要配置的实际状态。

## 控制面职责

GOV 统一持有已准入服务的逻辑 ID、制品版本、环境地址引用、协议 Profile、健康/启停状态、工具白名单、namespace/endpoint关系及去敏联调证据。当前 T100 服务目录为 [sysmcp-t100.toml](catalog/services/sysmcp-t100.toml)，两个 namespace 及 private endpoint 分别维护工具范围；旧 query 服务目录标记 `retired`，环境以 [t100.toml](environments/internal/t100.toml) 为准。目录是登记描述，不是已经实现的自动下发器或实时健康状态源。

GOV 不保存上游业务密钥、员工映射、业务授权清单、完整业务响应或 BIZ 审计数据库，也不实现 T100/OA/CRM 业务规则。MetaMCP 管理凭据与上游业务凭据分离；配置仓只保存可审查的引用、非秘密期望状态和验证证据。

## 产品接入方式

BIZ 提交去敏服务发布描述 → GOV 校验制品和接口契约 → 登记一个 FastMCP Streamable HTTP 服务 → 加入 namespace → 发布 private endpoint → 完成直连/两跳对照和拒绝验证。详细字段、状态和变更流程见 [服务接入规范](docs/service-onboarding.md)。

当前两个内部端点 `sysmcp-t100-internal-query`、`sysmcp-t100-internal-account` 已设置 `enable_api_key_auth=true`、`enable_oauth=false`、`use_query_param_auth=false`，客户端使用 `X-API-Key`；缺失和错误 Key 实测均返回401。两个端点共用同一个下游MCP服务，BIZ另验登记的临时query＋account连接JWT，其有效期为1小时，按 [运行说明](../biz-t100/docs/service-runtime.md) 受控续签并更新服务 Bearer Token。该连接主体不代表外部员工，当前记录 `composed`，保留 `published` 未通过。

| 当前 T100 入口 | 对外工具 | 对应 BIZ 能力 |
|---|---|---|
| `sysmcp-t100-internal-query` | `sysmcp-t100__t100_employee_query` | 查询批准工号的员工资料 |
| `sysmcp-t100-internal-account` | `sysmcp-t100__t100_account_maintain` | 批准账号禁用、启用及启用状态下密码重置 |

两个入口共享下游 query＋account 连接 scope，各自只开放对应工具；员工新建和更新不通过当前网关连接暴露。MetaMCP 2.4.22 不透传外部调用者 JWT，现版 API Key 是用户级访问凭据，不天然绑定单个 endpoint。工具白名单、BIZ 调用主体和业务目标许可分别执行；Key 不作为员工身份，员工映射和账号配对仍由 BIZ 持有。

业务命令统一使用 BIZ 的 [测试说明](../biz-t100/docs/testing.md)，部署与续签使用 [运行说明](../biz-t100/docs/service-runtime.md)。`tools` 只做发现，不调用 T100。账号 `acknowledged` 是接口回执，员工档案 `status` 不能证明登录账号启停或密码效果。

## 兼容基线

MetaMCP2.4.22固定backend使用TypeScript SDK1.16.0。本阶段兼容验收Profile为 **MCP 2025-06-18 + Streamable HTTP + initialize握手**；CORE的FastMCP4同时支持现代模式和握手模式。

网关已实测 **2025-06-18**，直连BIZ已实测 **2025-11-25**，两者均显式握手。FastMCP自身 `2026-07-28` 探针不能记为MetaMCP现代协议通过；原现代协议目标仍未验收。[ADR-001](../../docs/架构决策-001.md)

## 当前验收与剩余条件

| 检查 | 当前证据与剩余条件 |
|---|---|
| 产品与制品 | 运行版本／digest已核对，T100制品可追溯；Compose固定及部署隔离仍需独立验收 |
| 内部组合 | 一个T100服务、两个namespace／endpoint；本版发现、跨端点直接调用拒绝与确认门禁通过 |
| 业务对照 | 0.9.0真实只读两跳与直连一致；0.9.1接入未重复T100读写，账号ACK与登录效果分别留证 |
| 入口与启停 | 错误Key及namespace工具／服务停用前置拒绝，相关请求的BIZ业务执行为0；历史观察时长不是永久生效时限承诺 |
| 身份与发布 | 临时静态连接不代表真实员工；当前仅 `gov-registered`／`composed`，`business-validated` 与完整 `published` 未签收 |

真实业务开放前仍需按各系统声明的 `per-user`、`delegated` 或 `service-account` 模式完成权限与可追踪验证。T100按服务管理员执行，BIZ区分本人映射与显式维护许可，不要求员工T100凭据、不宣称继承员工ERP权限。统一维护入口已记录为 [DEC-001](../../docs/项目讨论与决策记录.md)，尚未实现；实际身份与集成缺口见 [BIZ集成输入](../biz-t100/docs/integration-inputs.md)。

## 文档导航

| 用途 | 文档 |
|---|---|
| 目录结构、对象关系、当前与历史归属 | [项目梳理](docs/project-overview.md) |
| 新服务交付、登记、变更与验收规则 | [服务接入规范](docs/service-onboarding.md) |
| 当前T100查询和账号接入 | [T100当前接入](docs/t100-account-onboarding.md) |
| 0.9.0只读接入过程 | [历史只读接入](docs/t100-internal-onboarding.md)、[历史验证](VALIDATION-internal-t100-20260930.md) |
| 部署版本、条件与阶段事实 | [部署记录](部署记录.md) |
| 查询、启停、密码重置如何调用 | [BIZ统一测试说明](../biz-t100/docs/testing.md) |

历史验证、原登记及私有认证材料保留。文档整理不改目录配置、实际网关对象或运行状态，不新增业务测试脚本。

官方依据：[版本](https://github.com/metatool-ai/metamcp/releases/tag/v2.4.22)、[固定Compose](https://github.com/metatool-ai/metamcp/blob/32b6e9813acfc13d1013eb2452be1899d6654bb9/docker-compose.yml)、[环境模板](https://github.com/metatool-ai/metamcp/blob/32b6e9813acfc13d1013eb2452be1899d6654bb9/example.env)、[端点默认](https://github.com/metatool-ai/metamcp/blob/32b6e9813acfc13d1013eb2452be1899d6654bb9/packages/zod-types/src/endpoints.zod.ts#L137)、[认证中间件](https://github.com/metatool-ai/metamcp/blob/32b6e9813acfc13d1013eb2452be1899d6654bb9/apps/backend/src/middleware/api-key-oauth.middleware.ts#L116)。
