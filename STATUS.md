# 当前状态

日期：2026-10-21。BIZ-T100 **0.9.2 固定 wheel 已部署**，GOV 登记已同步为 **0.9.2**。按用户确认恢复原 MetaMCP 容器、切换 BIZ 常驻环境并审查本地 Git 基线；本轮 T100 业务读取和写入均为 0，原许可、审计和历史未决记录保留。随后按用户“继续”补齐持续运行、连接令牌维护和远端交付，新增事实见 [运维验证](projects/gov/VALIDATION-operations-20261021.md)。

| 范围 | 当前实现与证据边界 |
|---|---|
| BIZ 核心 | 一个工厂承载员工新建、查询、更新、账号维护四接口；发现和执行按可信主体与许可过滤 |
| 固定部署 | sysmcp-biz-t100.service 使用 .runtime/releases/0.9.2/.venv；BIZ 与 CORE 非 editable，73 个生产包符合锁文件，产品文件与 wheel 一致；live／ready 均 200 |
| MetaMCP | 原应用容器已恢复，镜像 digest 未变、未重建升级；一个 sysmcp-t100 登记，query／account 两端点保留，仅更新下游连接令牌 |
| 连接验证 | 直连 2025-11-25：查询 1 工具、维护 2 工具；网关 2025-06-18：两个端点各 1 个指定工具，未调用 T100 |
| 持续运行 | MetaMCP 原容器 restart=unless-stopped；Compose 固定应用／数据库原镜像摘要；Docker active／enabled、BIZ 用户 Linger=yes；未重启机器演练 |
| 连接令牌维护 | 两段 oneshot 及每 20 分钟 timer 已启用；手动首轮和自动轮均签发／同步成功，令牌摘要不同且均回读核验；最长一小时，不提高主体权限；续签后两个网关端点各发现唯一指定工具 |
| 已有保护 | 请求编号防重、未知写结果占用、显式确认和禁止自动重试；跨请求业务幂等仍未实现 |
| 测试与制品 | 2026-10-21 本地 BIZ 1940／CORE 4／GOV 29 项通过；构建环境 6 包按哈希锁定，CORE 重建摘要相同；BIZ 候选元数据变化、21 份产品源码未变，未覆盖部署包 |
| Git 与审查 | 初始基线 667f8bf 已在 origin/main；[首次远端 CI](https://github.com/zs001122/sysmcp_dev/actions/runs/39552844241) 成功。原本地审查见 [交付审查](docs/本地交付审查-20261021.md)；本轮运维变更的推送、PR 与 CI 状态另见运维验证，不继承基线结果 |
| 正式状态 | 内部临时身份 real_identity_verified=false；business-validated 与完整 published 仍未签收 |

实际部署及检查记录见 [2026-10-21 部署验证](projects/biz-t100/VALIDATION-deployment-20261021.md)，源码与离线验收见 [0.9.2 验证记录](projects/biz-t100/VALIDATION-v0.9.2.md)。pyproject 中构建后端仍为范围约束，CI 已通过独立构建约束固定 6 包及 SHA-256；只有使用约定构建命令才保证这套固定环境。现成部署 wheel 摘要保持不变，跨机器部署、重启恢复与实际回退演练仍待完成。

账号操作使用 user.account.create：gzxastus 管登录账号启停、password_reset 显式控制重置；员工档案 status 不能用于确认账号状态。账号 ACK 与实际登录效果分别取证。

## 使用与后续工作

[开发清单](projects/biz-t100/docs/development-checklist.md) 是唯一待办矩阵。优先完成本轮运维变更的远端 PR／CI 审查，并补实际重启恢复、跨机器部署与回退证据，再按现有证据归档真实业务验收；不为归档重复成功写入。正式发布前仍需解决幂等、真实身份、上游认证和两跳归因。具体命令仅见 [测试说明](projects/biz-t100/docs/testing.md)，固定 wheel 安装、切换、回退及令牌维护见 [运行说明](projects/biz-t100/docs/service-runtime.md)。

本次运维授权与边界见 [DEC-004](docs/项目讨论与决策记录.md#dec-004内部连接自动续签常驻条件与远端交付)，此前恢复部署见 [DEC-003](docs/项目讨论与决策记录.md#dec-003metamcp-恢复与-biz-t100-固定制品部署)。[DEC-002](docs/项目讨论与决策记录.md#dec-002biz-t100-交付收尾与写入幂等边界) 保留 2026-10-08 候选阶段来源；DEC-001 统一维护入口是已确认但未实现的后续需求，不预建新的 IAM 或 AI 平台。

## 历史证据

- 此前一次员工 `status=N` 更新取得总码0、匹配逐条回执，独立回查仍为Y，其它资料未变；纯空白成功描述误判已修正。该员工档案状态效果尚未证实，原记录与 `unknown` 保留；它不能说明登录账号禁用失败。详情见 [v0.8.3](projects/biz-t100/VALIDATION-v0.8.3.md)。用户最新手测反馈不追溯改写这条记录。
- [v0.8.4](projects/biz-t100/VALIDATION-v0.8.4.md)：预填账号用例、空参数提示及业务对象未决隔离；该版本开发验证当时未执行真实账号写入，后续用户手测另记。
- [v0.8.2](projects/biz-t100/VALIDATION-v0.8.2.md)：原资料保留、动作/目标许可与明确拒绝分类；1783项全量离线结果属于该版本。
- [v0.8.1](projects/biz-t100/VALIDATION-v0.8.1.md)：中文摘要与一次明确复核请求，1593项离线结果。
- [v0.8](projects/biz-t100/VALIDATION-v0.8.md)：四接口和两个入口，1502项离线结果。
- [v0.7](projects/biz-t100/VALIDATION-v0.7.md)：历史员工状态更新及未知结果记录，原文保留。
- [v0.6.1](projects/biz-t100/VALIDATION-v0.6.1.md)：历史账号操作回执；其后的员工状态查询不能证明登录账号操作失败或成功。
- [v0.6](projects/biz-t100/VALIDATION-v0.6.md)、[v0.5](projects/biz-t100/VALIDATION-v0.5.md)、[内部模拟](projects/biz-t100/VALIDATION-internal.md)：各时点独立证据，保留原样。

旧test标签不作为数据隔离依据。真实员工、账号、凭据及运行资料不进入源码或制品；实际交付与提交状态以本次部署验证为准；origin 已改为 SSH 且 main 基线已有成功 CI；本轮变更独立留证。
