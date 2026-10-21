# 2026-10-21 内部连接与持续运行验证

本记录承接 [固定部署验证](../biz-t100/VALIDATION-deployment-20261021.md)，对应用户“继续”后的运维交付；不覆盖历史结论。时区为 Asia/Shanghai。实际 T100 业务读取、写入均为 **0**；business-validated／完整 published 仍未签收。

## 运行与制品

| 检查 | 实际结果 |
|---|---|
| BIZ 固定部署 | 保持 0.9.2 原 wheel，SHA-256 `4f4f2c4eccba27cd853f2e00e5a18ec93b5b89c19e5254d145047f174b392655`，本轮不切换业务制品 |
| GOV 同步制品 | 0.1.0 单脚本，SHA-256 `cce0e33601a374fe29292224e8a3477ff1416bf856dc8c90f59832fe7c0d2823`；私有固定 release 中运行，与受审源码相同，未包含测试材料或秘密 |
| 用户常驻条件 | `Linger=yes`；既有 BIZ service enabled／active；新增同步 timer enabled／active；Docker 系统服务 enabled／active |
| MetaMCP | 仅将原应用容器 restart 改为 unless-stopped；数据库原策略保留，没有重建容器或数据库 |
| Compose | 应用固定原镜像摘要 `6d5e0cba4ffc4976069a980723c11042f993a978fe0f3071ba2ec8a91ebc3d41`，数据库固定 `721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea`，应用 pull_policy=if_not_present；配置解析通过 |
| 开机实测 | 未重启宿主，不将配置就绪记为实际恢复演练通过 |

原 Compose 的私有备份、候选及变化摘要保存在 GOV `.runtime/operations-20261021/`，不进入 Git。固定镜像不包含升级；两个镜像引用保持当前实际运行内容。为符合私有文件祖先目录不可组写约束，仅将工作空间及 projects、gov、biz-t100 四个自有目录从 0775 改为 0755；私有目录 0700／文件 0600，未递归改权限。

## 自动续签与真实连接

签发由 BIZ 固定 CLI 完成，同步由 GOV 自有独立工具通过 MetaMCP 管理 API 完成。两者仅通过显式私有导出文件交接。单个 timer 启动一分钟后触发，随后每二十分钟执行；每次先重新检查许可再签发最长一小时 JWT，不自动执行任何业务工具。

| 轮次／检查 | 结果 |
|---|---|
| 人工触发完整链路 | 16:25:11 同步成功；本轮到期 17:25:10，登记回读一致 |
| timer 自动触发 | LastTrigger=16:26:57；16:26:59 同步成功，本轮到期 17:26:58；令牌摘要与首轮不同，两段 unit Result=success／exit 0 |
| 查询端点 | 显式协议 2025-06-18，唯一工具 `sysmcp-t100__t100_employee_query`，业务请求 0 |
| 账号端点 | 显式协议 2025-06-18，唯一工具 `sysmcp-t100__t100_account_maintain`，业务请求 0 |
| 实际下游调用 | 经账号端点发送 confirm=false，BIZ 返回 confirmation_required；upstream_attempted=false，read_requests=0、write_requests=0；证明续签后可到达原生认证后的业务入口 |
| 业务记录保持 | 当前 functions 与历史 account-write 库的操作、事件及上下文行数前后完全一致；未创建业务写操作或修改历史终态 |

上述到期时间只记录这两个历史轮次，不代表长期当前到期时间。运行情况以 GOV 私有 `.runtime/connector-sync/state.json` 的最近成功和实际过期时点、两个 unit 的执行结果为准；一次或两次成功不签收长期稳定性。签发失败时 GOV 不运行，原状态文件不会刷新，不能只看其旧 success 字段。

固定版本源码复核发现：MetaMCP 更新登记后异步刷新闲置连接，活跃会话仍持有原 Authorization。因此现场结果覆盖登记续签和新建客户端调用，不证明原长会话能跨旧令牌到期继续调用。live.py 每次独立创建／结束会话；持久客户端须在请求完成后结束旧会话并重新初始化，不能自动重试业务请求。此限制及代码依据见续签契约；未修改全局 session lifetime，也未重启网关来强制清理其它服务会话。

实际登记仍为原 sysmcp-t100，query／account 的 namespace、private endpoint、Key 及业务许可保持。没有创建替代服务、修改产品数据库或复制业务秘密进 GOV。契约、无条件更新的并发限制及停用方式见 [续签说明](docs/connector-renewal.md)。

## 离线、构建与审查

| 范围 | 本轮结果 |
|---|---|
| BIZ 全量离线 | 1940 passed，约 155 秒；独立固定依赖、非 editable wheel 环境 |
| CORE | 4 passed；两个隔离源码目录重新构建均匹配原 wheel SHA-256 `1eac4b1b5ed9e8bcf469ec9a92c608e455696db30240ac67af6f29981af3066f` |
| GOV | 29 项标准库单元／契约测试通过；错误目标、令牌及管理接口失败、字段保持、锁及原子状态等覆盖 |
| 构建工具链 | hatchling 及五个传递依赖固定版本和哈希；错误哈希确实被拒绝；约定命令使用 --build-constraints 和 --require-hashes |
| 工程检查 | Ruff、CORE 格式、独立依赖一致性、工作流 YAML／shell、制品边界及 JUnit 去敏检查通过；原项目依赖锁和既有 dist 不变 |
| 本地独立审查 | 另一代理复核同步实现、真实 MetaMCP API 契约、systemd 依赖和 CI，最终 33 项公开变更未发现阻塞问题；代码及工作流摘要绑定当前版本；本验证记录另经公开材料检查 |

本地结构化报告在根 `.cache/ci-reproducibility-20261021/CI-REPRODUCIBILITY-VALIDATION.json`；该临时证据不作为产品运行依赖。BIZ README 的既有变化会改变新构建 METADATA／RECORD，21 份产品 Python 源码未变；新 CI 包与现有部署包分别留存，不覆盖同版本历史制品。离线测试不连接真实 IdP 或 T100。

## Git 与远端证据

origin 为现有 `zs001122/sysmcp_dev`，原 main 初始提交 `667f8bf` 的 [首次 CI](https://github.com/zs001122/sysmcp_dev/actions/runs/39552844241) 已成功。该结果不代替本次运维变更的提交级检查。本轮实现提交 `e2ea075a70cdecb142b114e4bef747654e4a6a56` 已推送至 `codex/t100-v092-deployment-20261021`，建立 [PR #1](https://github.com/zs001122/sysmcp_dev/pull/1)；本地独立审查无阻塞发现，正式合并评审和分支保护仍分别待办，不自动合并。后续文档收尾提交的自动检查以 [PR 当前检查](https://github.com/zs001122/sysmcp_dev/pull/1/checks) 为准，不把先前提交结果套用到后续变更。

实现提交 e2ea075 的 [远端 CI](https://github.com/zs001122/sysmcp_dev/actions/runs/39569822959) 已成功：GOV 29 项、CORE 4 项、BIZ 1940 项全部通过，CORE 重建哈希匹配、制品边界和去敏报告检查通过，制品已作为该次 CI 产物上传。BIZ 远端用时 126.34 秒；不覆盖当前部署制品。

提交前核验 233 个公开文件，未发现私有路径、私钥、完整 JWT 或已知真实员工资料；438 个受影响本地文档链接均有效，三份 systemd 模板校验通过。

## 仍未验收

宿主实际重启、跨机器部署／回退及长期运维监测尚未演练；正式 PR 合并评审独立进行。正式 IdP／真实员工归因、上游服务认证和受控传输、跨请求业务幂等及账号登录效果仍按 [唯一待办矩阵](../biz-t100/docs/development-checklist.md) 推进。自动续签只解决当前受控内部连接的持续可用条件，不提升上述业务验收状态。
