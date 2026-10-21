# 当前状态

日期：2026-10-08。BIZ-T100 **0.9.2 交付候选**；现有常驻与 GOV 登记的部署基线仍为 **0.9.1**。本轮仅做代码说明修正、本机测试主体对齐、验证与交付整理，不部署、不改运行配置、不调用真实 T100、不自动提交或推送。

| 范围 | 当前实现与证据边界 |
|---|---|
| BIZ 核心 | 一个工厂承载员工新建、查询、更新、账号维护四接口；发现和执行按可信主体与许可过滤 |
| 0.9.2 候选 | 更正旧查询工具的员工 status 描述；live.py 本机主体固定 temporary-local-operator，签名材料仍临时生成，原生验签及目标许可不变 |
| 现有部署基线 | 一个 sysmcp-biz-t100.service、一个 sysmcp-t100 登记，query／account 两个 namespace 与 private endpoint；运行状态及连接令牌需实际使用时检查 |
| 已有保护 | 请求编号防重、未知写结果占用、显式确认和禁止自动重试；跨请求业务幂等仍未实现 |
| 本轮验证与制品 | 候选全量 1940 项离线、静态与依赖检查通过；制品、独立安装及去敏基线准备完成。实际范围见 [0.9.2 验证记录](projects/biz-t100/VALIDATION-v0.9.2.md)；旧版证据不继承、不覆盖 |
| Git 与审查 | 此前检查无提交或远端；本轮准备去敏交付基线与本地审查，不能当作已经提交或完成提交级审查 |
| 正式状态 | 内部临时身份 real_identity_verified=false；business-validated 与完整 published 仍未签收 |

账号操作使用 user.account.create：gzxastus 管登录账号启停、password_reset 显式控制重置；员工档案 status 不能用于确认账号状态。账号 ACK 与实际登录效果分别取证。

## 使用与后续工作

[开发清单](projects/biz-t100/docs/development-checklist.md) 是唯一待办矩阵，区分 P0 本轮交付收尾、P1 真实验收及正式发布前置、P2 统一维护入口。具体命令仅见 [测试说明](projects/biz-t100/docs/testing.md)，配置及令牌维护见 [运行说明](projects/biz-t100/docs/service-runtime.md)。

本轮范围及幂等未决方案见 [DEC-002](docs/项目讨论与决策记录.md#dec-002biz-t100-交付收尾与写入幂等边界)。统一维护入口 DEC-001 是已确认但未实现的后续需求；不预建新的 IAM 或 AI 平台。候选版本不自动改变 GOV 登记版本或运行服务，也不增加日常测试入口。

## 历史证据

- 此前一次员工 `status=N` 更新取得总码0、匹配逐条回执，独立回查仍为Y，其它资料未变；纯空白成功描述误判已修正。该员工档案状态效果尚未证实，原记录与 `unknown` 保留；它不能说明登录账号禁用失败。详情见 [v0.8.3](projects/biz-t100/VALIDATION-v0.8.3.md)。用户最新手测反馈不追溯改写这条记录。
- [v0.8.4](projects/biz-t100/VALIDATION-v0.8.4.md)：预填账号用例、空参数提示及业务对象未决隔离；该版本开发验证当时未执行真实账号写入，后续用户手测另记。
- [v0.8.2](projects/biz-t100/VALIDATION-v0.8.2.md)：原资料保留、动作/目标许可与明确拒绝分类；1783项全量离线结果属于该版本。
- [v0.8.1](projects/biz-t100/VALIDATION-v0.8.1.md)：中文摘要与一次明确复核请求，1593项离线结果。
- [v0.8](projects/biz-t100/VALIDATION-v0.8.md)：四接口和两个入口，1502项离线结果。
- [v0.7](projects/biz-t100/VALIDATION-v0.7.md)：历史员工状态更新及未知结果记录，原文保留。
- [v0.6.1](projects/biz-t100/VALIDATION-v0.6.1.md)：历史账号操作回执；其后的员工状态查询不能证明登录账号操作失败或成功。
- [v0.6](projects/biz-t100/VALIDATION-v0.6.md)、[v0.5](projects/biz-t100/VALIDATION-v0.5.md)、[内部模拟](projects/biz-t100/VALIDATION-internal.md)：各时点独立证据，保留原样。

旧test标签不作为数据隔离依据。真实员工、账号、凭据及运行资料不进入源码或制品；没有提交或推送。
