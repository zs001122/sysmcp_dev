# SysMCP：企业业务 MCP 接入

基于现成 MCP 网关提供治理入口，基于 FastMCP 开发业务 MCP 服务，按各系统实际身份与权限模式执行，业务服务控制调用者可用的工具与范围。

持续讨论与新增开发决策统一记录在 [项目讨论与决策记录](docs/项目讨论与决策记录.md)，已收录统一维护入口需求；已确认结论与待讨论方案分别标记，相关开发按记录及引用契约推进。

```text
MCP Client / 既有 Agent
          │
          ▼
GOV：MetaMCP（服务登记、聚合、命名空间、端点和访问控制）
          │ Streamable HTTP
          ▼
BIZ-ADP：独立业务适配项目；按信任与运行边界组成 FastMCP 服务
          │ 复用 CORE 的小型公共能力
          ▼
既有业务 API（逐系统声明用户、委托或服务账号模式）
```

## 首批初始化

| 范围 | 当前产出 |
|---|---|
| [GOV](projects/gov/README.md) | MetaMCP 2.4.22已核对；T100一个服务登记，查询／账号两个端点的工具范围及前置拒绝验证通过；正式发布仍待身份与业务验收 |
| [CORE](projects/core/README.md) | Python 3.12 + FastMCP 4.0.10 独立项目、服务工厂、本地工程探针、测试与锁文件 |
| [BIZ-T100](projects/biz-t100/README.md) | 0.9.2 交付候选收尾；现有部署基线 0.9.1，一个服务承载四接口、两个网关端点；本轮验收与待办见独立清单 |
| [BIZ-ADP 规范](docs/BIZ-ADP开发规范.md) | 后续业务系统沿统一目录、工具、身份与验收规范独立接入 |

短中期优先 MetaMCP；ContextForge 仅列为后续治理需求增长时的评估项，不并行建设。选型依据与能力边界见[架构决策](docs/架构决策-001.md)。旧分析报告保留在 [reports](reports/mcp_auth_demo-20260929/项目架构分析与模块化闭环规划.md)，其中产品版本结论是历史基线，当前选型以本次决策为准。

本工作空间采用多个独立子项目：各自构建、依赖锁、环境和测试。CORE 是供 BIZ-ADP 安装的库，不额外部署成转发服务。根目录不建立共享 Python 依赖环境，也不预建 IAM/REL/AUD/数据库/前端等空模块。

## GOV 统一注册与发布

MetaMCP 及后续已封装 MCP 服务的登记、环境地址引用、协议 Profile、namespace、endpoint、工具暴露范围和组合验证状态统一由 GOV 管理，目录与文档导航见 [GOV项目梳理](projects/gov/docs/project-overview.md)。业务源码、上游凭据、用户映射、授权策略和业务审计仍归各 BIZ-ADP 项目，不能复制到 GOV。

每个业务系统保持独立开发项目；运行单元按凭据、信任域、读写风险、故障隔离和发布节奏划分。详细决策见 [ADR-004](docs/架构决策-004-GOV统一服务注册与发布.md)，接入步骤见 [GOV 服务接入规范](projects/gov/docs/service-onboarding.md)，通用边界见 [模块边界与接口约束](docs/模块边界与接口约束.md)。T100当前使用一个FastMCP常驻单元和一个MetaMCP服务登记，查询／账号维护通过两个namespace及private endpoint选择工具；下游使用临时连接主体，正式用户身份与完整发布状态分别验收，见 [ADR-009](docs/架构决策-009-T100账号操作网关接入.md)。

## 旧资产复用原则

对当前需要的能力，优先评估 `/data/projects/mcp_auth_demo` 及用户指定来源的已有实现，再决定复用、适配、参考重写或采用框架原生能力。不得未经评估直接复制，也不因旧模块可复用就提前建设尚未需要的模块。

规则统一编号 **REUSE-01**，已写入 [AGENTS.md](AGENTS.md)；详细标准、准入条件和禁止事项见 [旧模块复用准则](docs/旧模块复用准则.md)，每个候选按 [评估模板](docs/复用评估模板.md) 留下可追溯记录。历史验收不能直接继承为当前系统通过。

## 从 CORE 开始

```sh
cd projects/core
uv sync --locked
uv run --locked pytest -q
uv run --locked sysmcp-probe
```

探针仅监听 `127.0.0.1:8765/mcp`，只有无业务能力的 `core_probe`。它用于验证框架和工程结构，不是业务系统或治理闭环。

当前状态见 [STATUS.md](STATUS.md)，开发交付与正式集成的唯一待办矩阵见 [BIZ-T100 开发清单](projects/biz-t100/docs/development-checklist.md)。BIZ-T100 的模块、信息和文档入口见 [项目梳理](projects/biz-t100/docs/project-overview.md)。四接口已提供员工新建、查询、更新及登录账号维护，日常仍使用 `scripts/offline.py` 和 `scripts/live.py`。四接口、直连及MetaMCP业务命令统一见 [测试说明](projects/biz-t100/docs/testing.md)，服务配置和连接令牌维护见 [运行说明](projects/biz-t100/docs/service-runtime.md)。0.9.2 候选修正员工状态描述并固定本机临时操作员主体，仍原生验签；编号防重与 unknown 占用已实现，跨请求业务幂等尚未实现。本轮全量离线、静态及依赖检查已通过；不改变现有运行配置、不部署、不追加真实业务调用、不提交或推送。候选验证见 [v0.9.2](projects/biz-t100/VALIDATION-v0.9.2.md)，此前接入见 [v0.9.1](projects/biz-t100/VALIDATION-v0.9.1.md)，之前的 [收敛验证](projects/biz-t100/VALIDATION-consolidation-20260930.md) 保留为历史证据。
