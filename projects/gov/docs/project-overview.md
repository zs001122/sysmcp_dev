# GOV 项目梳理与文档索引

核对日期：2026-10-21；产品基线：**MetaMCP 2.4.22**；当前T100固定部署制品：**BIZ-T100 0.9.2**。本页说明治理目录、当前登记关系、证据和文档归属，不执行配置发布。业务调用命令统一见 [BIZ测试说明](../../biz-t100/docs/testing.md)。

## 责任与管理范围

GOV保存服务登记、环境地址引用、协议Profile、工具白名单、namespace／endpoint关系及去敏组合证据。它不实现T100业务逻辑，不持有员工映射、业务许可、上游服务凭据、完整业务响应或BIZ审计数据库；业务规则与状态继续归各BIZ项目。控制面责任见 [ADR-004](../../../docs/架构决策-004-GOV统一服务注册与发布.md)，当前T100部署决定见 [ADR-009](../../../docs/架构决策-009-T100账号操作网关接入.md)。

本目录管理的当前T100对象是一个MCP server、两个namespace和两个private endpoint。这个数量不能用来判断整个既有MetaMCP实例是否有其它服务。GOV目录是可审查的登记描述，不是MetaMCP声明式导入文件、自动下发器或实时健康状态源。

## 对象层次与当前关系

| 层次 | 当前T100对象 | 职责 |
|---|---|---|
| BIZ开发项目 | `projects/biz-t100` | 独立源码、依赖、契约、测试与制品；不按每项操作建立新项目 |
| FastMCP运行单元 | 一个 `sysmcp-biz-t100.service` | 同一维护工厂承载四工具；实际发现与执行仍受主体许可限制 |
| MetaMCP MCP server | `sysmcp-t100` | 连接同一个BIZ地址，使用受控内部连接令牌 |
| namespace | `sysmcp-t100-internal-query`、`sysmcp-t100-internal-account` | 都引用该server；各自工具白名单只开放一项 |
| private endpoint | 同名query／account入口 | 入口API Key访问控制与MCP聚合地址，不作为个人员工身份 |
| 工具与业务操作 | 员工查询、账号维护 | 账号禁用、启用和密码重置复用同一个维护工具 |

```mermaid
flowchart LR
    C[MCP 客户端] --> Q[query endpoint / namespace]
    C --> A[account endpoint / namespace]
    Q --> M[一个 MetaMCP server: sysmcp-t100]
    A --> M
    M --> B[一个 BIZ-T100 FastMCP 服务]
    B --> T[T100 业务接口]
```

| 入口 | 对外工具 | 目录来源 |
|---|---|---|
| query | `sysmcp-t100__t100_employee_query` | [查询namespace](../catalog/namespaces/sysmcp-t100-internal-query.toml)、[查询endpoint](../catalog/endpoints/sysmcp-t100-internal-query.toml) |
| account | `sysmcp-t100__t100_account_maintain` | [账号namespace](../catalog/namespaces/sysmcp-t100-internal-account.toml)、[账号endpoint](../catalog/endpoints/sysmcp-t100-internal-account.toml) |

下游地址引用是 `http://host.docker.internal:8781/mcp`，BIZ当前绑定 `172.17.0.1:8781/mcp`；两个入口的本机地址引用见 [当前环境](../environments/internal/t100.toml)。这些是内部环境记录，不能据此宣称公网开放。

查询入口只接受BIZ批准工号，当前不开放任意姓名／部门检索。账号入口受BIZ `status-password` profile限制，仅批准账号的禁用、启用、启用状态下密码重置；不开放员工新建／更新、账号复制或权限明细。员工 `status` 属于档案，账号 `gzxastus` 属于登录账号；账号ACK须实际账号资料或登录证据确认效果。字段、目标和授权规则的权威定义留在BIZ，GOV仅引用 [工具契约](../../biz-t100/docs/tool-contracts.md)。

## 目录组织与权威来源

| 位置 | 用途与当前归属 |
|---|---|
| [selection.toml](../selection.toml) | 2026-09-29产品选型基线；其中not-run或待核实注释是当时状态，不覆盖后来T100组合证据 |
| [catalog/services/sysmcp-t100.toml](../catalog/services/sysmcp-t100.toml) | 当前T100服务版本、制品摘要、MetaMCP对象ID、允许工具及证据引用 |
| `catalog/namespaces/` | 两namespace的服务关联、工具暴露与排除清单 |
| `catalog/endpoints/` | 两private endpoint的namespace、入口认证开关及私有Key引用 |
| [environments/internal/t100.toml](../environments/internal/t100.toml) | 当前T100统一运行单元与两个入口的非秘密地址引用 |
| [旧query服务目录](../catalog/services/sysmcp-t100-internal-query.toml)、[旧query环境](../environments/internal/t100-query.toml) | 0.9.0阶段来源；服务已retired并replaced_by当前登记，环境已标为历史 |
| `.runtime/` | GOV 管理账号、会话、入口 Key、实际对象、续签私有配置及去敏状态；真值不进入公开文档或制品 |
| `ops/`、`tests/`、`deploy/` | 专用连接令牌同步工具、独立契约测试、同步 oneshot 与 timer；不是业务测试入口 |
| `pyproject.toml`、`uv.lock`、`.venv` | GOV 独立标准库运维项目，无第三方依赖，不借用 BIZ 环境或内部源码 |
| [部署记录](../部署记录.md) | 分阶段说明运行产品、镜像、网络条件、迁移和证据边界 |
| [历史只读验证](../VALIDATION-internal-t100-20260930.md) | 0.9.0真实只读对照与拒绝证据；原文不追溯改写 |

目录中的9份TOML是产品选型、当前及历史登记描述；没有全量同步它们到MetaMCP的工具。专用连接维护工具只更新原服务的短期 Bearer token，不创建服务、不改变 namespace 或端点白名单。运行变更使用固定版本已验证的受支持管理界面／API并保存对象ID及结果，不直接修改产品数据库。不同角色的来源不得混用：制品字段引用BIZ发布描述，真实网关对象使用管理记录，验证结论引用对应时点证据。

## 身份、凭据与协议边界

MetaMCP2.4.22当前不透传外部用户JWT。两个namespace共用查询＋账号维护scope的临时下游连接主体，BIZ验证该主体并继续检查目标许可；它不代表调用网关的真实员工。现版API Key是用户级访问凭据，不天然绑定一个endpoint；分别配置Key不证明员工权限或跨端点身份隔离。

GOV入口／管理凭据、BIZ资源身份令牌、T100管理员服务凭据是三类不同材料。BIZ私钥、员工／账号映射和上游key留BIZ；MetaMCP只保存连接所需短期Bearer Token。下游连接令牌默认且最长一小时，需要BIZ续签后更新统一server已保存的Bearer Token；仅替换本地文件不会使网关自动更新。已启用 BIZ 签发与 GOV 发布两个 oneshot、每 20 分钟 timer，手动首轮和自动轮均成功；GOV 只读 BIZ 专门发布的 JWT 文件。现场启用、最近结果与故障处理见 [续签说明](connector-renewal.md)，签发边界见 [BIZ运行说明](../../biz-t100/docs/service-runtime.md)。

网关兼容链路显式验收 `2025-06-18` initialize，直连BIZ显式验收 `2025-11-25`；不自动回退。原 `2026-07-28` 现代协议目标不能用CORE单组件探针或旧Profile抵扣，保持未验收。

## 已有证据与未闭环项

| 维度 | 证据来源 | 当前结论 |
|---|---|---|
| 产品身份 | 2026-10-21 原应用及数据库镜像不变，Compose 已固定两个 digest | 原容器 restart=unless-stopped，Docker enabled、用户 Linger=yes；未重启机器演练，旧退出原因未确认 |
| T100内部登记 | 目录已同步 0.9.2 固定 wheel；原 server 与双端点保留，仅更新连接 token | `gov-registered`／`composed`，不提升正式发布状态 |
| 真实只读对照 | GOV0.9.0验证、BIZ0.9.0验证 | 当时直连与两跳返回一致；历史旧server及prefix后来被替代 |
| 当前工具与门禁 | [2026-10-21 部署验证](../../biz-t100/VALIDATION-deployment-20261021.md)；[0.9.1 历史验证](../../biz-t100/VALIDATION-v0.9.1.md) | 本次两端点发现、实际下游缺确认拒绝及错误／缺失 Key 拒绝通过，业务读写 0；历史跨端点和启停证据不冒充本次重测 |
| 各项目逻辑 | 2026-10-21 本地 BIZ 1940／CORE 4／GOV 29 项通过 | 三个项目分别验收；BIZ 原 2026-10-08 结果保留，不能记成 GOV 产品测试 |
| 真实身份与账号效果 | [BIZ集成输入](../../biz-t100/docs/integration-inputs.md) | 真实个人身份／员工隔离、上游服务认证整改与账号效果仍需独立验收，`business-validated`未签收 |
| 完整发布 | 上述内部证据与正式准入条件分别核对 | `published`未签收，端点存在与工具发现不自动提升状态 |

产品及网络隔离的目标基线与实际部署仍需分别核对，不能把README目标要求当作已经生效。部署记录中的短暂启停观察时间是当时测量值，不承诺永久最大生效时限。

后续 [DEC-001统一维护入口](../../../docs/项目讨论与决策记录.md) 已确认需求但尚未实现：可以集中提交链路维护变更，各层权威数据仍分别归身份源、BIZ和GOV。当前目录不自动完成身份绑定、授权变更或跨层下发，不预建新IAM或业务测试脚本。

## 文档导航与本轮订正

| 需要了解的内容 | 文档及本轮订正 |
|---|---|
| GOV总览与当前状态 | [README](../README.md)：补单服务双入口、管理范围限定、目录与状态来源 |
| 通用交付与接入规则 | [service-onboarding](service-onboarding.md)：区分对象层次，运行单元按经验证的风险与信任边界决定 |
| 当前查询／账号接入 | [t100-account-onboarding](t100-account-onboarding.md)：明确各endpoint各一工具、三账号动作及身份／Key限制 |
| 早期只读过程 | [t100-internal-onboarding](t100-internal-onboarding.md)：前置现行 0.9.2 导航，原 0.9.0 过程明确归为历史 |
| 运行与迁移记录 | [部署记录](../部署记录.md)：新增持续运行与续签阶段，保留早期恢复、0.9.1、0.9.0 及初次核实来源 |
| 连接令牌维护 | [connector-renewal](connector-renewal.md)、[运维验证](../VALIDATION-operations-20261021.md)：实现与现场状态分开、签发与发布分开 |
| 业务调用与常驻运维 | [BIZ测试说明](../../biz-t100/docs/testing.md)、[BIZ运行说明](../../biz-t100/docs/service-runtime.md)：只引用唯一指南，不复制出另一套命令 |

2026-10-21 同步 GOV 当前文档和 sysmcp-t100 制品目录，保留单服务双端点关系。原 MetaMCP 容器已恢复，管理 API 仅更新服务连接令牌，其余字段回读保持；历史 VALIDATION 和制品未改写，BIZ 原许可与审计保留，业务读写 0。现场证据见 [部署验证](../../biz-t100/VALIDATION-deployment-20261021.md)。随后补齐固定 Compose 镜像、原容器重启策略、用户 Linger 和两段续签工具；现场运行与发布结果见 [运维验证](../VALIDATION-operations-20261021.md)，当前状态与到期时间须实时核对。

## 2026-09-30 历史文档核验结果

| 检查 | 实际结果 |
|---|---|
| 文档引用 | 10份GOV及关联Markdown，148个本地链接、其中4个标题锚点有效，代码围栏完整 |
| 登记关系 | 9份TOML只读解析通过，19项版本／制品摘要／服务／namespace／endpoint／环境及引用检查通过 |
| 有效与历史区分 | 当前服务composed、旧query服务retired并指向替代服务；两namespace仍关联当前服务、各暴露一工具 |
| 状态与身份 | published／business_validated仍为false，外部用户未透传，临时连接最长3600秒且无自动刷新 |
| GOV保存 | 9份元数据、11份私有资料、1份历史验证，整理前后文件集合与内容摘要一致 |
| BIZ保存 | 实现／测试／配置／部署／依赖及制品148份、私有运行资料332份、文档及历史证据54份，整理前后文件集合与内容摘要一致 |
| 公开资料 | 核对页面不含本轮已知真实员工工号、姓名、邮箱或电话 |
| 操作边界 | 管理API调用0，T100读取0、写入0；未重跑工程测试、未构建制品或增加日常脚本 |

上表只记录 2026-09-30 的文档整理，不表示 2026-10-21 的文件集合、元数据及私有资料仍完全相同；本次运行恢复和连接令牌变化另见部署验证。历史检查未签收业务效果或正式发布。
