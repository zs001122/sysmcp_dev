# 0.9.2 内部部署与网关恢复记录

日期：2026-10-21，时区 Asia/Shanghai。依据用户对“恢复 MetaMCP、固定制品部署、建立本地 Git 基线”的同意。本次使用已验收的 0.9.2 制品，不修改业务实现，不重复真实业务写入；2026-10-08 的 [候选验证](VALIDATION-v0.9.2.md) 及所有旧制品保留原样。

## 部署结果

| 项目 | 本次实际结果 |
|---|---|
| MetaMCP | 启动原 `metamcp` 容器，健康接口 200；未拉取镜像、重建数据库或升级产品 |
| 恢复前事实 | 应用容器于 2026-10-10 12:10:58 退出，代码 137，`OOMKilled=false`；具体原因未确定。PostgreSQL 原容器健康 |
| 网关产品 | 2.4.22，revision `32b6e9813acfc13d1013eb2452be1899d6654bb9`；镜像 SHA-256 `6d5e0cba4ffc4976069a980723c11042f993a978fe0f3071ba2ec8a91ebc3d41` 保持不变 |
| BIZ 固定制品 | `sysmcp_biz_t100-0.9.2-py3-none-any.whl`，SHA-256 `4f4f2c4eccba27cd853f2e00e5a18ec93b5b89c19e5254d145047f174b392655` |
| CORE 固定制品 | `sysmcp_core-0.1.0-py3-none-any.whl`，SHA-256 `1eac4b1b5ed9e8bcf469ec9a92c608e455696db30240ac67af6f29981af3066f` |
| 运行环境 | Python 3.12.3；新建 `.runtime/releases/0.9.2/.venv`，按锁文件及哈希离线安装 73 个生产包，版本逐项匹配、依赖兼容检查通过 |
| 安装内容 | BIZ 21 个与 CORE 3 个产品 Python 文件逐项匹配 wheel；从 site-packages 导入，非 editable，不依赖开发 `.venv` |
| 常驻服务 | 原 `sysmcp-biz-t100.service` 改用固定安装；2026-10-21 13:50:13 重启，active/running；`/health/live`、`/health/ready` 均 200，ready 仅证明本地条件 |
| 原运行资料 | 仍使用 `.runtime/service/server-maintenance.json`、原许可、原服务 key 引用、原审计和历史未决检查；没有换库、解锁或改写历史终态 |
| GOV 登记 | 保留 `sysmcp-t100` 原 UUID `07ba0212-0cd4-4f19-9196-7dcfbde333f6`，原两个 namespace／private endpoint 不重建；公开目录对齐 0.9.2 wheel |

部署前开发环境的分发元数据为 0.9.1，但通过 editable 指向当前源码，不能将其描述为不可变的 0.9.1 运行制品。原 `.venv` 和服务单元备份均保留，作为本机回退资料；旧环境不是已签收的可复现版本。

## 本次验证范围

| 验证 | 结果 |
|---|---|
| 固定安装的 `check-service` | configuration_valid；不调用 T100 |
| 直连查询主体，2025-11-25 | 仅发现 `t100_employee_query` |
| 直连维护主体，2025-11-25 | 仅发现 `t100_employee_update`、`t100_account_maintain` |
| 网关查询端点，2025-06-18 | 仅发现 `sysmcp-t100__t100_employee_query` |
| 网关账号端点，2025-06-18 | 仅发现 `sysmcp-t100__t100_account_maintain` |
| 账号端点实际下游门禁 | 两次 `confirm=false` 均由 BIZ 返回 `confirmation_required`；可信连接客户端为 `metamcp-internal-maintenance`，每次读写业务请求均为 0 |
| 网关缺失／错误 API Key | 均 HTTP 401 |
| 业务库核对 | 统一操作库和旧账号写库的各表行数前后完全一致；未追加业务操作 |

工具发现与门禁证明当前运行、协议和内部连接可用，不等于真实 T100 业务效果验收。本次真实 T100 读请求 **0**、写请求 **0**，没有重放禁用、启用或密码重置。原 1940 项全量离线结果属于 2026-10-08，本次未重新执行全量测试；业务源码与该制品一致。

## 令牌与维护边界

通过固定版本受支持管理 API 重新登录、读取原服务并仅更新 `bearerToken`，再读取逐项核对，其他服务字段不变。调用前先核对原服务名称、UUID、类型及下游地址，未修改他人服务或直接写网关数据库。临时连接令牌在 **2026-10-21 14:50:53 +08:00** 到期；本次验证发生在有效期内。自动续签尚未实现，文件续签后仍须同步更新 MetaMCP 登记。

MetaMCP 当前重启策略仍为 `no`，既有 Compose 引用仍是浮动标签；本次启动原容器的固定镜像不等于已完成 Compose 固定。BIZ 用户服务已 enabled，但用户 `Linger=no`；无人登录时的开机常驻尚未验收。这些运维缺口继续列在 [开发清单 D13](docs/development-checklist.md)，不把一次恢复记作长期运行保障。

## 证据位置与代码交付

私有现场记录保存在 BIZ `.runtime/deployments/20261021/`：生产依赖清单、固定 wheel 哈希引用、原服务单元、安装核验、直连／网关发现结果、缺确认响应及业务库计数。GOV `.runtime/recovery-20261021/` 保存管理会话恢复、原服务及更新后对象、字段核对摘要；其中包含认证材料，不进入 Git 或制品。令牌私钥、上游凭据及批准目标仍留原 BIZ 私有目录。

首次 Git 基线准备时，218 个公开候选与 0.9.2 最终源码清单完全一致，六项已有制品／证据摘要匹配；独立审查未发现禁止路径、私钥块、JWT 实值或已知本人资料，且产品不导入 scripts／tests。工作期间出现初始提交 `667f8bf22a981e6db498d907e83bf28ba8033086`；本次执行者保留该提交，独立审查核实其中 217 文件仍与原清单一致，唯一差异为本轮已核验的固定安装 ExecStart。本轮修改部署单元和文档变更另做差异核对。该检查不宣称覆盖未知秘密或替代完整安全审计。提交级结果见 [本地交付审查](../../docs/本地交付审查-20261021.md)，远端仓库及远端 CI／PR 审查仍待实际配置与执行。

另有构建可复现缺口：CI 使用 `uv build`，两项目的构建后端 Hatchling 仍是范围版本，未完整锁定隔离构建依赖。既有 wheel 可验证部署；不能因此宣称未来远端重建必定产生同一摘要。

本次状态仍为受控内部组合，`real_identity_verified=false`、`business_validated=false`、`published=false`。后续正式身份、跨请求业务幂等、有效／失效上游凭据、真实业务效果及两跳外部用户归因分别验收。
