# 2026-09-30 文档收敛前快照

以下文档保留早期契约、部署说明和诊断流程。历史命令原文仍在，不能作为当前操作指南；当前业务命令统一见 [testing.md](../../testing.md)。

| 历史范围 | 快照 |
|---|---|
| 探针及内部模拟 | [direct-check](direct-check.md)、[internal-test](internal-test.md)、[approved-employee-case](approved-employee-case.md)、[startup-readiness](startup-readiness.md) |
| 旧真实入口及本人重置 | [live-read](live-read.md)、[live-write](live-write.md)、[password-reset](password-reset.md) |
| 原契约与运维 | [tool-contracts](tool-contracts.md)、[error-codes](error-codes.md)、[operations](operations.md) |
| 原发布与输入资料 | [service-release](service-release.md)、[integration-inputs](integration-inputs.md) |

[origins.json](origins.json) 保存 12 份来源、原始哈希和归档哈希。相对 Markdown 链接因目录深度重新定位；旧脚本的源码链接指向本项目的离线回归资料，历史命令文本没有替换成归档执行命令。此适配不代表历史部署、接口效果或真实身份状态在当前仍成立。

历史 VALIDATION 记录与私有运行数据保留原路径；本轮收敛范围见 [consolidation.md](../../consolidation.md)。

此前项目登记另保存为 [workspace.toml](workspace.toml)，仅作为旧入口、端口和阶段来源；当前登记以根目录 workspace.toml 为准。
