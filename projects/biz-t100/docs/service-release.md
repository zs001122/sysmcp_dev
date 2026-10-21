# BIZ-T100 当前交付与发布边界

当前固定 wheel 部署与 GOV 登记：**0.9.2**。日期：2026-10-21。四工具收敛到一个 FastMCP 进程，MetaMCP 只登记一个 `sysmcp-t100` MCP server；查询和账号操作通过两个 namespace／endpoint 白名单区分。现行部署见 [2026-10-21 验证](../VALIDATION-deployment-20261021.md)，原 0.9.2 构建、旧制品、审计和验证记录原样保留。

## 当前交付

| 范围 | 内容 |
|---|---|
| 产品包 | `sysmcp-biz-t100==0.9.2`；独立 pyproject.toml、uv.lock；按既有 wheel SHA-256 安装 |
| 已部署版本 | 0.9.2；常驻环境 .runtime/releases/0.9.2/.venv，BIZ／CORE 非 editable，73 包锁定且产品文件匹配制品；GOV 目录同步版本及摘要 |
| 0.9.2 产品修正 | 更正员工 status 的旧工具描述；本机 live 主体固定 temporary-local-operator，动态临时签名、原生验签及目标许可保持 |
| 四接口 | 员工新建、查询、更新及登录账号维护；业务对象和回执规则不变 |
| 运行单元 | `sysmcp-biz-t100.service`；统一 `maintenance` 工厂承载四工具，发现按部署动作／签名 scope／启用主体动作交集过滤，执行另检查目标和许可 |
| 产品 CLI | `check-service`、`serve`、`issue-gateway-token`；网关连接令牌限查询＋限定账号维护，不增加日常测试脚本 |
| 日常入口 | `scripts/offline.py` 与 `scripts/live.py`；后者增加 HTTP 服务地址、客户端配置、工具发现及明确协议选择 |
| 部署文件 | `deploy/sysmcp-biz-t100.service`；旧 read／write 单元停用，不再作为当前启动入口 |
| 配置 | 现行私有 `server-maintenance.json`；`config/` 保存合成接口及许可示例 |
| 私有配置与数据 | `.runtime/service/`，现有运行许可、共享业务审计与历史未决库；不进入 wheel 或 sdist |
| 旧脚本 | `tests/legacy_support/`；仅供离线回归，来源与历史内容保留 |
| 当前指南 | [测试说明](testing.md)、[常驻运行说明](service-runtime.md)、[工具契约](tool-contracts.md) |

版本制品的实际文件与摘要以 `dist/` 中本版本构建记录为准；不得覆盖旧版制品、摘要或构建验证。2026-10-08 的构建及离线验收见 [0.9.2 验证](../VALIDATION-v0.9.2.md)，本次部署另见 [2026-10-21 验证](../VALIDATION-deployment-20261021.md)；[0.9.1 验证](../VALIDATION-v0.9.1.md) 保留历史接入证据；[0.9.0 验证](../VALIDATION-v0.9.0.md) 保留为此前常驻与只读组合证据，不继承旧版本测试计数。

0.9.1 `offline.py --suite interfaces` 的 **675 项相关回归**和 0.9.0 的 1909 项全量结果均是历史证据，不继承为 0.9.2 测试通过。0.9.2 的 1940 项离线、制品摘要与独立安装见其版本验证，本次未重跑全量；交付、Git 和正式集成的缺项只维护在 [开发清单](development-checklist.md)。当前 0.9.2 wheel／sdist 的摘要、包含文件检查及独立安装证据分别见 [摘要文件](../dist/SHA256SUMS-v0.9.2)、[构建验证](../dist/BUILD-VALIDATION-v0.9.2.json)。日常命令见 [测试说明](testing.md)，发布前检查清单见 [运行准备](startup-readiness.md)。

## 服务登记描述

| 连接 | 工具白名单 | 当前地址 | 协议 Profile | 当前 GOV 范围 |
|---|---|---|---|---|
| 本机查询 | `t100_employee_query` | `http://172.17.0.1:8781/mcp` | 直连 `2025-11-25` | 与本机维护访问同一个 FastMCP 服务 |
| 本机维护 | 当前主体：`t100_employee_update`、`t100_account_maintain` | `http://172.17.0.1:8781/mcp` | 直连 `2025-11-25` | 员工更新不进入网关白名单 |
| 网关查询 | 仅 `t100_employee_query` | 下游 `http://host.docker.internal:8781/mcp` | MetaMCP `2025-06-18` | MCP server `sysmcp-t100`；namespace／endpoint `sysmcp-t100-internal-query` |
| 网关账号 | 仅 `t100_account_maintain` | 同一地址 `http://host.docker.internal:8781/mcp` | MetaMCP `2025-06-18` | 同一 MCP server；namespace／endpoint `sysmcp-t100-internal-account` |

地址只是当前内部环境记录，不表示可公网访问。同一服务根据可信主体、客户端、scope、企业、动作及目标检查授权，并沿用原有真实写开关和范围许可；不按一个功能一个服务划分。工具完整名称使用统一前缀 `sysmcp-t100__`。员工新建已实现，但当前本机主体没有建档许可。启动、健康检查和工具发现均不调用 T100，也不授权真实建档或写入。

账号网关只接受批准目标的五字段最小单条报文，不复制、不附带权限明细。不重置时允许账号 Y/N；重置时必须同时启用 Y。员工新建及完整账号维护接口仍保留，须通过各自独立的主体、scope 和目标许可使用，不扩大当前网关入口。

GOV 保存上述服务 metadata、环境引用、协议 Profile、工具白名单、namespace、endpoint 和组合验证状态。T100 配置、凭据、员工／账号范围、签名私钥、业务请求与审计仍归 BIZ。GOV 的 API Key、下游 BIZ 连接令牌和 T100 服务 key 是三类不同凭据。

## 证据与正式状态

此前用户手测反馈与本地三次账号成功回执继续保留；账号 `acknowledged` 不能自动改成 `business_effect_verified=true`。本轮已切换固定 wheel 常驻服务并恢复网关，没有再次执行真实 T100 查询或写入。

当前身份仍为 `real_identity_verified=false`。MetaMCP 2.4.22 不透传外部调用者 JWT；统一 MCP server 使用 `metamcp-internal-maintenance` 连接身份和一个查询＋账号维护的短期 JWT。查询／账号客户端配置和入口 Key 分开维护，namespace 工具白名单分别控制暴露范围。连接令牌最多一小时，续签并更新统一 server 的 Bearer token 后才能继续使用，不建设新的身份平台。

现版 MetaMCP Key 是用户级凭据，不天然绑定某个 endpoint；分开配置 Key 不构成端点级身份或员工权限隔离。签名连接主体与 BIZ 目标许可仍是独立控制层，正式个人身份与客户端归因契约尚待接入。

此前直连与网关两跳真实查询已对照一致；只读缺失／错误 API Key、服务／工具停用均无 BIZ 业务执行，属于 [0.9.0 历史验证](../VALIDATION-v0.9.0.md)。0.9.1 接入时账号工具发现、确认门槛、Key 与工具停用的实际结果见 [0.9.1 验证记录](../VALIDATION-v0.9.1.md)，不把这些检查记为真实密码或账号状态已生效。协议不自动回退。内部登记与组合验证不能替代正式 IdP、真实员工身份隔离或完整 `published` 验收，`business-validated` 与 `published` 尚未签收。

| 验收维度 | 当前签收范围 |
|---|---|
| `biz-validated` | 0.9.2 在 2026-10-08 的全量离线及制品核验通过；2026-10-21 固定部署及连接验证另记，不含真实个人身份或账号效果 |
| `business-validated` | 未签收；需要真实身份、唯一员工来源、真实上游、分层授权与审计证据 |
| `published` | 未签收；当前只有内部 `gov-registered`／`composed`，仍缺正式身份链及完整发布验收 |

`business-validated` 与 `published` 分别取证，不能互相推导。后续正式输入见 [集成输入](integration-inputs.md)；[统一维护入口 DEC-001](../../../docs/项目讨论与决策记录.md) 仅已确认需求，尚未实现，不作为当前发布能力。

当前写保护是请求编号防重与 unknown 目标占用，跨请求业务幂等尚未实现；内部测试的显式授权不替代正式写发布前置。本轮不增加幂等键步骤、不迁移审计 schema。本地初始提交 667f8bf 的独立审查见 [交付审查](../../../docs/本地交付审查-20261021.md)，本轮文档另作收尾提交；远端未配置，远端 CI／PR 未完成。固定 wheel 部署不等于未来构建可复现：hatchling 仍为范围版本，构建依赖固定及跨机器部署／回退仍待验收。

当前单服务部署依据为 [ADR-009](../../../docs/架构决策-009-T100账号操作网关接入.md)，此前常驻决策见 [ADR-008](../../../docs/架构决策-008-T100常驻运行与网关测试身份.md)。GOV 当前组合登记见 [账号与查询接入记录](../../gov/docs/t100-account-onboarding.md)；[早期只读记录](../../gov/docs/t100-internal-onboarding.md) 保留为历史。历史发布描述见 [2026-09-30 快照](history/20260930/service-release.md)，先前入口整理见 [收敛清单](consolidation.md)。
