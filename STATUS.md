# 当前状态

日期：2026-10-21。BIZ-T100 **0.9.2 固定 wheel 已部署**，GOV 登记已同步为 **0.9.2**。按用户确认恢复原 MetaMCP 容器、切换 BIZ 常驻环境并审查本地 Git 基线；本轮 T100 业务读取和写入均为 0，原许可、审计和历史未决记录保留。

| 范围 | 当前实现与证据边界 |
|---|---|
| BIZ 核心 | 一个工厂承载员工新建、查询、更新、账号维护四接口；发现和执行按可信主体与许可过滤 |
| 固定部署 | sysmcp-biz-t100.service 使用 .runtime/releases/0.9.2/.venv；BIZ 与 CORE 非 editable，73 个生产包符合锁文件，产品文件与 wheel 一致；live／ready 均 200 |
| MetaMCP | 原应用容器已恢复，镜像 digest 未变、未重建升级；一个 sysmcp-t100 登记，query／account 两端点保留，仅更新下游连接令牌 |
| 连接验证 | 直连 2025-11-25：查询 1 工具、维护 2 工具；网关 2025-06-18：两个端点各 1 个指定工具，未调用 T100 |
| 运维缺口 | 网关令牌 2026-10-21 14:50:53 +08:00 到期，无自动续签；MetaMCP restart=no、BIZ 用户 Linger=no；Compose 浮动镜像和跨机器部署／回退仍待验收 |
| 已有保护 | 请求编号防重、未知写结果占用、显式确认和禁止自动重试；跨请求业务幂等仍未实现 |
| 测试与制品 | 1940 项离线通过属于 2026-10-08；本轮校验并部署已有 0.9.2 制品，不覆盖旧制品、不重跑全量离线 |
| Git 与审查 | 本地初始提交 667f8bf 已完成独立审查，本轮文档另作收尾提交；见 [交付审查](docs/本地交付审查-20261021.md)。远端未配置，远端 CI、PR 与正式审查未完成 |
| 正式状态 | 内部临时身份 real_identity_verified=false；business-validated 与完整 published 仍未签收 |

实际部署及检查记录见 [2026-10-21 部署验证](projects/biz-t100/VALIDATION-deployment-20261021.md)，源码与离线验收见 [0.9.2 验证记录](projects/biz-t100/VALIDATION-v0.9.2.md)。构建后端 hatchling 仍为范围约束，现成 wheel 已校验可用，但未来重新构建的可复现性尚未签收。

账号操作使用 user.account.create：gzxastus 管登录账号启停、password_reset 显式控制重置；员工档案 status 不能用于确认账号状态。账号 ACK 与实际登录效果分别取证。

## 使用与后续工作

[开发清单](projects/biz-t100/docs/development-checklist.md) 是唯一待办矩阵。优先补齐远端交付与 CI／审查、短期令牌和常驻运维缺口，再按现有证据归档真实业务验收；不为归档重复成功写入。正式发布前仍需解决幂等、真实身份、上游认证和两跳归因。具体命令仅见 [测试说明](projects/biz-t100/docs/testing.md)，固定 wheel 安装、切换、回退及令牌维护见 [运行说明](projects/biz-t100/docs/service-runtime.md)。

本次授权与边界见 [DEC-003](docs/项目讨论与决策记录.md#dec-003metamcp-恢复与-biz-t100-固定制品部署)。[DEC-002](docs/项目讨论与决策记录.md#dec-002biz-t100-交付收尾与写入幂等边界) 保留 2026-10-08 候选阶段来源；DEC-001 统一维护入口是已确认但未实现的后续需求，不预建新的 IAM 或 AI 平台。

## 历史证据

- 此前一次员工 `status=N` 更新取得总码0、匹配逐条回执，独立回查仍为Y，其它资料未变；纯空白成功描述误判已修正。该员工档案状态效果尚未证实，原记录与 `unknown` 保留；它不能说明登录账号禁用失败。详情见 [v0.8.3](projects/biz-t100/VALIDATION-v0.8.3.md)。用户最新手测反馈不追溯改写这条记录。
- [v0.8.4](projects/biz-t100/VALIDATION-v0.8.4.md)：预填账号用例、空参数提示及业务对象未决隔离；该版本开发验证当时未执行真实账号写入，后续用户手测另记。
- [v0.8.2](projects/biz-t100/VALIDATION-v0.8.2.md)：原资料保留、动作/目标许可与明确拒绝分类；1783项全量离线结果属于该版本。
- [v0.8.1](projects/biz-t100/VALIDATION-v0.8.1.md)：中文摘要与一次明确复核请求，1593项离线结果。
- [v0.8](projects/biz-t100/VALIDATION-v0.8.md)：四接口和两个入口，1502项离线结果。
- [v0.7](projects/biz-t100/VALIDATION-v0.7.md)：历史员工状态更新及未知结果记录，原文保留。
- [v0.6.1](projects/biz-t100/VALIDATION-v0.6.1.md)：历史账号操作回执；其后的员工状态查询不能证明登录账号操作失败或成功。
- [v0.6](projects/biz-t100/VALIDATION-v0.6.md)、[v0.5](projects/biz-t100/VALIDATION-v0.5.md)、[内部模拟](projects/biz-t100/VALIDATION-internal.md)：各时点独立证据，保留原样。

旧test标签不作为数据隔离依据。真实员工、账号、凭据及运行资料不进入源码或制品；实际交付与提交状态以本次部署验证为准，远端未配置。
