# BIZ-T100 运行准备与验证边界

适用版本及固定 wheel 部署 **0.9.2**；核对日期：2026-10-21。本页用于判断准备到哪一层，不另建测试模式。日常命令统一见 [测试说明](testing.md)，服务启动、健康检查、客户端连接和令牌续签见 [常驻运行说明](service-runtime.md)。

0.9.2 在 2026-10-08 的测试与构建见 [版本验证](../VALIDATION-v0.9.2.md)；2026-10-21 的固定 wheel 安装、运行及网关恢复见 [部署验证](../VALIDATION-deployment-20261021.md)。交付待办统一在 [开发清单](development-checklist.md)。

## 当前部署准备事实（0.9.2）

| 层次 | 已有准备与证据 | 成功所证明的范围 |
|---|---|---|
| 独立工程 | 自身 `pyproject.toml`、`uv.lock`、开发 `.venv`；0.9.2 wheel／sdist，2026-10-08 的 1940 项离线通过 | 声明范围的逻辑／接口准备；不是 2026-10-21 重新执行的测试或真实业务效果 |
| BIZ 常驻 | 一个 `sysmcp-biz-t100.service`；`maintenance` 模式在 `172.17.0.1:8781/mcp` 承载四工具 | 独立 .runtime/releases/0.9.2/.venv 非 editable wheel；2026-10-21 active／enabled、live／ready 200，用户 Linger=no |
| 私有运行配置 | `.runtime/service/server-maintenance.json`；原 T100 配置、服务 key 引用、运行许可及审计保留 | 沿用原批准范围，没有因部署自动新增目标许可 |
| 本机客户端 | 查询主体发现一个工具；现有维护主体发现员工更新和账号维护两个工具 | 原生验签与主体工具范围；`tools` 不发 T100 业务请求 |
| GOV 内部组合 | 一个 `sysmcp-t100` 登记，query／account 两个 namespace 及 private endpoint | 2026-10-21 两端点发现及实际下游缺确认拒绝通过；历史跨端点拒绝证据保留，不是正式个人身份发布 |
| 历史业务 | 真实查询、账号三次成功回执及用户手测反馈独立保留 | ACK 和手测反馈分别留证，不从员工 status 推定账号效果 |

以上当前部署事实来自 [2026-10-21 验证](../VALIDATION-deployment-20261021.md)，本次 T100 业务读写为 0；[0.9.1 验证记录](../VALIDATION-v0.9.1.md) 继续作为历史接入来源。服务及上游状态须实际核对，网关连接令牌最长一小时且无自动刷新。旧 read／write 单元已停用，原文件和记录继续留档。

## 使用前的最小检查

1. 核对本项目依赖与版本，使用锁定依赖和自己的环境；不使用根目录共享 Python 环境。
2. 对统一服务配置运行产品 `check-service`。它检查受控配置和私有文件元数据，不读取 T100 key 内容或连接上游。
3. 核对统一用户服务、`/health/live` 和 `/health/ready`；ready 只表示本地配置、许可和审计准备，不证明 T100 可用。
4. 使用所需客户端执行 `tools`，核对实际工具范围。员工新建已实现，但当前维护主体未获建档许可；不能为看到四工具临时放宽范围。
5. 真实查询使用批准工号；写入使用批准用例及 `--confirm`，同一未决账号的写入仍会阻塞，不能换入口或请求号绕过。
6. 经 MetaMCP 时显式选 `2025-06-18` Profile，核对 namespace 白名单与短期连接令牌；令牌续签文件后还须更新 MetaMCP 已保存的 Bearer token。直连使用 `2025-11-25`，不自动回退。

每一步的具体命令只维护在 [测试说明](testing.md) 和 [常驻运行说明](service-runtime.md)。原 DOCX、运行凭据、真实员工与请求／审计资料留私有 `.runtime`；旧脚本仅在 `tests/legacy_support/` 供离线回归。

## 仍未闭合的正式验收

临时签名测试身份均标 `real_identity_verified=false`。MetaMCP 2.4.22 使用共享受控下游连接主体，不透传外部个人 JWT；其用户级 Key 也不天然绑定某个 endpoint。工具白名单不是员工授权，员工查询也不能验证登录账号状态或密码。

当前只有请求编号防重与未知目标占用，跨请求业务幂等尚未实现，也无本轮 schema 迁移；它是正式写发布前置。当前声明的离线范围支持 `biz-validated`；正式 IdP、真实员工隔离、上游认证整改及账号效果证据仍需分别补齐，`business-validated` 与完整 `published` 尚未签收。具体资料和步骤见 [集成输入](integration-inputs.md)，发布边界见 [服务发布](service-release.md)。统一维护入口仅作为 [DEC-001](../../../docs/项目讨论与决策记录.md) 已确认需求记录，尚未实现。

早期运行说明及命令保存在 [历史快照](history/20260930/startup-readiness.md)，仅用于理解旧验证记录；不能作为当前启动入口。
