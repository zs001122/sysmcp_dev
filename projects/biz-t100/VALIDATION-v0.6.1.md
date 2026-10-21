# BIZ-T100 v0.6.1：直接真实账号维护

日期：2026-09-30。用户最新要求“我不要plan模式，我需要真实的”。本轮按其此前确认的具体员工与登录账号绑定、detail 省略保留语义，完成限定内部真实调用。业务使用真实 T100；临时部分仍是本机签名身份，不签收真实 IdP、密码生效或账号状态读回。

## 已执行的真实请求

| 动作 | 请求语义 | 次数 | 实际响应与审计 |
|---|---|---|---|
| 账号禁用 | `gzxastus=N`、`password_reset=N`、`is_reproduce=N` | 1 | `upstream_acknowledged`；started → dispatched → succeeded |
| 密码重置 | `gzxastus=N`、`password_reset=Y`、`is_reproduce=N` | 1 | `upstream_acknowledged`；started → dispatched → succeeded |

两次均使用指定员工与独立明确绑定的登录账号；同一企业、当前已批准上游，单条 master 且省略 detail。没有发送账号启用、角色/站点修改或员工档案更新请求，没有自动重试。协议明确协商为 `2025-11-25`。执行后核对两条完整审计链，待核查操作数为 0。

真实目标、具体操作编号、调用者关联及审计事件保存在私有 `.runtime/live-write/validation-direct-20260930.json`；操作库为同目录下的 `audit/account-write.sqlite3`。未将真实员工、账号或凭据写入源码、自动测试及公开制品。

上述结果证明真实接口已返回合法成功 ACK；`business_effect_verified=false`、`real_identity_verified=false`。未获取或推测新密码，没有用员工档案 status 代替登录账号读回，也未执行登录验证。未知业务效果需在 T100 的账号管理或维护方提供的有效回读中核对。

## 直接命令与控制

`live_write.py reset-password` / `disable-account` 接收明确的工号、登录账号和操作 UUID；重置另需显式账号状态，两者均需 `--existing-account-confirmed --confirm`。不要求先运行 plan。使用说明见 [live-write](docs/live-write.md)。

程序内部保存授权快照，再通过原生 JWT/FastMCP、动作 scope、受控授权、管理员 key、真实默认 HTTP 客户端及耐久审计执行；内部快照不是额外用户步骤。相同 UUID 与相同参数只能复用原快照，不能覆盖已撤销许可；已执行编号拒重。同 UUID 改目标、动作或状态返回 operation_conflict，并且不读取 key、不请求上游。结果未知继续跨动作锁定员工与账号。

当前私有 Contract 只允许已确认目标的两个动作与状态 N，并绑定 `enterprise_id` 和当前 T100 配置摘要；其他目标、账号、动作、状态、企业及配置均被前置拒绝。没有改动 AGENTS.md 的全局规则，没有把这次内部授权扩展到其他人员、账号启用、普通 MCP 调用者或 GOV 发布。程序默认写开关仍关闭；此现场限定配置依据最新明确指令开启。

## 验证

- `bash scripts/test_account_write.sh`：**368 passed，31.55s**；其中直接调用新增 43 项，覆盖两动作、实际捕获报文、scope/映射、白名单、企业/配置绑定、重复及并发去重、参数冲突、撤权保持、未知锁与 CLI。
- `ruff check src scripts tests`、`ruff format --check src scripts tests`：通过（48 files already formatted）。
- `bash -n scripts/test_account_write.sh`：通过。
- 所有自动测试使用合成资料和模拟上游，不与上表两次真实调用混算。
- 上一版的 901 项全项目回归保留在 [v0.6](VALIDATION-v0.6.md)，本轮不把它重标成 v0.6.1 新执行结果。

## 制品与验收状态

版本与 uv.lock 为 0.6.1；制品 `dist/sysmcp_biz_t100-0.6.1-py3-none-any.whl`、`dist/sysmcp_biz_t100-0.6.1.tar.gz`，摘要存 `dist/SHA256SUMS-v0.6.1`。检查 wheel 不含 scripts/tests，两个制品不含私有运行目录、上传原始 DOCX、测试私钥或已知真实员工标识。既有制品与历史验证保留。

代码/契约维持 **biz-validated**，新增的是指定账号两动作的真实 ACK 证据；**business-validated** 和 **published** 仍未完成。上游服务 key 的真实拒绝语义、正式身份、业务最终效果及 GOV 两跳须各自验收。本轮未修改网关，未提交或推送。
