# T100 本人密码重置：工具契约与离线直接验证

> 历史阶段说明。当前统一使用 [四接口与两类测试入口](../../testing.md)，本文旧命令仅作为历史回归背景，不再作为日常运行入口。既有运行资料与审计保留。

日期：2026-09-29。状态：离线候选契约已达 biz-validated；实际测试、自检与制品证据统一记录在 [VALIDATION-reset](../../../VALIDATION-reset.md)。本轮获授权的是本人重置的离线接入；真实写入、密码交付和 GOV 发布均未验收。

决策见 [ADR-006](../../../../../docs/架构决策-006-T100本人密码重置.md)，来源及文件摘要见 [RU-003](../../../../../docs/reuse/RU-003-T100本人密码重置.md)。只读查询继续使用原工厂及其验收，不能用查询成功抵扣写操作验收。

## 1. 独立工具与输入输出

工具名：`reset_my_t100_password`。所在工厂只提供此写工具，不把它加入本人档案查询服务。工具标记为非只读、可能破坏状态、非幂等、访问外部系统；本地去重不等于上游幂等保证。

| 项目 | 契约 |
|---|---|
| 身份 | 原生 AuthProvider 验证令牌；按 IdentityProfile 提取可信 issuer、subject、client_id |
| Scope | 必须具备 `t100:password:reset:self`；查询 scope 不授予写权限 |
| BIZ 许可 | 受控主体启用、客户端获准、`allow_password_reset=true`；该许可默认 false |
| 员工与账号 | 当前只由 issuer+subject 唯一映射给出 employee_id 与显式 t100_account_id；账号不可默认等于员工号 |
| 企业 | 来自固定服务配置；操作库、上游配置须装配为同一企业 |
| 输入 | 恰好 `confirm` 和 `operation_id` 两个字段，禁止额外字段 |
| 确认 | confirm 必须是真正的布尔值 true；false 返回 confirmation_required；字符串或数字不做转换 |
| 操作 ID | operation_id 必须是规范的小写、带连字符 UUID 字符串；每次新意图生成一次，保留它用于关联和调查 |
| 成功输出 | 仅 operation_id 与固定 status=upstream_acknowledged；无密码、员工资料、账号资料或上游原文 |

示例仅演示协议形状，不包含真实身份或操作：

```json
{"confirm":true,"operation_id":"9ea1a024-d36d-49b5-a48a-49c7539347de"}
```

```json
{"operation_id":"9ea1a024-d36d-49b5-a48a-49c7539347de","status":"upstream_acknowledged"}
```

不接受姓名、员工号、账号、新密码、服务 key、上游地址或 reason 参数。普通 Header、邮箱、姓名、MetaMCP Key，以及未选定的已签 employee_no 都不能覆盖受控映射。服务管理员 key 仅表示 BIZ 调用现有接口服务的执行身份，不使调用者成为管理员，也不允许代他人重置。

`confirm=true` 是此次协议调用的明确写意图，不是权限来源，也不是新鲜身份验证、人工二次审核或密码交付的证明。相同 operation_id 再次提交只得到拒重结果，不重新派发，也不返回历史成功响应。

## 2. 公开装配边界

包公开 `create_reset_server` 和 `create_reset_app`。必须显式注入 auth、identity、access、resetter、store；工厂不读取运行配置，不启动监听，不提供真实身份或真实上游默认值。`create_reset_app` 生成原生 Streamable HTTP `/mcp` ASGI 应用。

| 依赖 | 最小接口与责任 |
|---|---|
| PasswordResetAccess | authorize(Caller) → PasswordResetGrant；逐次检查本人许可和绑定，授权后才读取服务 key |
| PasswordResetGrant | employee_id、独立 account_id、binding_version、排除序列化的 credential；读授权对象不可隐式升级为写授权 |
| PasswordResetter | reset(PasswordResetGrant) → ResetReceipt；最多一次请求，不自行重试 |
| ResetStore | reserve → mark_dispatched → finish；状态与对应事件在本地事务内持久化 |
| SQLiteResetStore | 独立重置库，固定 enterprise_id 与工具名；显式 prepare/check_ready，构造本身不产生文件 I/O |

现有文件实现使用 `FileServiceAccountAccess(..., permission="password_reset")`，而查询继续使用 employee_read。受控策略按原始内容摘要形成 binding_version；请求期间员工、账号或策略版本变化即拒绝继续。策略/key 只保存在 BIZ 私有位置，不进入 GOV，不在输出或日志中打印。

重置库与查询 journal 分开，使用独立 schema/application 标记。库的直接父目录要求 0700，库及 SQLite 附属文件要求 0600、当前服务所有且无硬链接；拒绝路径中的软链接和不兼容 schema。准备、健康检查及凭据规则参照 [运行说明](../../operations.md)，但该说明的查询运行入口不因此成为重置入口。

实现分别见 [reset_server](../../../src/biz_t100/reset_server.py)、[reset_client](../../../src/biz_t100/reset_client.py)、[reset_store](../../../src/biz_t100/reset_store.py) 与 [access](../../../src/biz_t100/access.py)。

## 3. 执行顺序与耐久边界

1. 校验输入、身份、scope、本人写许可和两个目标；任何前置拒绝都不得发出上游请求，未授权时不得读取 key 内容。
2. reserve 耐久写入 started 和身份/目标快照，同时占用企业内的员工及账号；写入失败则停止。
3. 重验授权及绑定，耐久记录 dispatched；磁盘等待结束后再次重验授权、绑定、令牌和预算，才进入 resetter。
4. 最多尝试一次候选上游调用，严格校验 ACK；上游返回后再重验授权及绑定。
5. finish 耐久记录 succeeded 后，检查令牌、预算及取消状态，再尝试交付最小 ACK 输出。

finish 前有最后一次策略重验；finish 后检查令牌和预算，不承诺覆盖最后策略检查至响应交付之间的撤权竞争。它不是跨系统授权锁。即使已耐久记录 succeeded，也可能因随后令牌失效、超时、取消或连接中断而未向调用者交付 ACK；不能覆盖已记录的事实，不能据客户端报错自动重发。

默认业务预算 10 秒，可装配为大于 0 且不超过 10 秒。耐久写入及失败收尾可能继续等待；预算不构成“10 秒内一定返回”的硬承诺，超时也不证明上游没有执行。

## 4. 状态、双目标占用与错误处理

| 状态 | 本地证据含义 | 同企业目标占用 |
|---|---|---|
| started | 已登记意图，尚未耐久记录可能派发 | 同时占用 employee_id 与 account_id |
| dispatched | 已记录可能派发；并不证明网络已送达 | 继续占用两个目标 |
| succeeded | 严格 ACK 已被接受并耐久记录；不证明密码生效 | 释放活动占用，operation_id 永久保留用于拒重 |
| rejected | 候选契约中的明确认证/权限拒绝已记录 | 释放活动占用，operation_id 仍拒重 |
| not_sent | 正常流程在派发标记前结束，已耐久记录未派发 | 释放活动占用，operation_id 仍拒重 |
| unknown | 已到可能派发边界，不能确认最终结果 | 两个目标持续占用，无自动到期或解锁 |

同一 operation_id 在任意状态都返回 reset_already_recorded。started/dispatched/unknown 对 `(enterprise_id, employee_id)` 与 `(enterprise_id, account_id)` 分别建立唯一占用；更换操作 ID、调用者或只改其中一个绑定，不能绕过仍占用的同一目标，冲突返回 reset_pending_review。不同企业不共享目标空间。

保证范围是同一企业使用同一持久操作库的调用；为相同目标另建一份数据库会破坏这一去重边界。默认独立写运行单元、单 worker，不以多库部署扩大吞吐。进程重启保留未决状态；不提供自动重放、TTL 解锁、恢复旧库清除占用或直接改表的运维捷径。

| 场景 | 对外及存储要求 |
|---|---|
| 输入/身份/scope/许可拒绝 | 固定安全错误，0 次上游请求；不回显输入、令牌或 key |
| reserve 或派发前耐久步骤失败 | 不调用上游；能确认未派发才记 not_sent，不能确认时保留原状态 |
| 已记录 dispatched 后撤权、取消、超时或异常 | 保守按 unknown 保留占用；实际请求可能为 0 或 1 次 |
| 严格候选 HTTP 401/403 | 分别映射 upstream_unauthenticated/business_forbidden 并记 rejected；真实接口是否保证拒绝且无副作用仍须定标 |
| 网络故障、非明确拒绝、非零业务码或响应契约异常 | 不把失败响应推断为未写入；派发后通常返回 reset_outcome_unknown 并保留占用 |
| ACK 后撤权/改绑 | 不交付成功，保留 unknown；ACK 不可用于跳过最后授权检查 |
| 终态写失败 | 返回 journal_unavailable 或中止响应；保留未决，不据缺失终态推断未派发 |

操作事件绑定 operation_id、issuer、subject、client_id、enterprise_id、employee_id、account_id、tool_name、binding_version、状态/固定错误及时间。日志禁止记录 headers、token、key、上游原文或密码。pending_operation_ids 只供定位调查，不能作为待重放队列。没有查明真实结果的受控处理流程前，未知目标继续停用。

本地事务只把操作状态和本地事件一起提交；T100、SQLite、权限配置与客户端之间没有原子事务，也没有已证明的上游幂等键。operation_id 不发送给旧上游。dispatched 可能先落盘而请求尚未发送，也可能请求已执行但终态尚未落盘；保守锁定是处理这些窗口的策略，不是 exactly-once 承诺。

## 5. 候选 T100 报文与 ACK 限制

当前离线候选仍依据旧接口名 `user.account.create` 构造 envelope，企业使用 datakey.EntId，单条 master 仅包含：

```json
{"gzxa001":"LOGIN-A","gzxa003":"E001","password_reset":"Y"}
```

这些值在运行时分别来自受控账号和员工授权，不来自工具参数；示例仅为合成标识。不得补入旧 builder 默认的 gzxastus=Y，也不提交角色 detail、新密码或其他账号字段。旧说明把状态列为必填，且创建/修改共用接口，因此“只发送三个字段”目前只是待验证候选，不能据此声称服务端支持局部更新或无副作用。

仅接受 `payload.std_data.execution.code` 的字符串 "0" 或整数 0；布尔值、浮点数不接受。std_data.parameter 只能缺省或为空对象，非空、多个记录、null 等均不属于当前 ACK 契约。输出 upstream_acknowledged 只说明候选响应被接受，不说明账号目标已回显、密码已更改、密码已交付或本人已成功登录。

查询工具的“恰好一条且 enterprise_id+employee_no 精确相等”依赖其查询响应；旧重置响应没有得到证明的目标回显 schema，不能移用该查询规则并宣布写入对象已回验。未来若接口方提供明确回显/结果查询契约，需单独修订适配及验收。

`T100PasswordResetClient` 默认 transport=None 即抛 business_adapter_not_ready；本轮只显式注入模拟传输进行离线验证，不提供真实 reset runtime 装配、生产启动命令或网关发布配置。显式依赖注入是工程边界，并非阻止任意代码另行构造网络调用的沙箱。

## 6. 离线直接验证

先按项目依赖锁建立本项目独立环境；从本项目目录运行，无额外参数：

```sh
cd /data/projects/sysmcp_dev/projects/biz-t100
.venv/bin/python scripts/direct_reset_check.py
```

[直接验证脚本](../../../tests/legacy_support/direct_reset_check.py) 使用临时 127.0.0.1 端口、原生 MCP Client、合成签名身份、私有临时策略/key/SQLite 和显式 MockTransport。它只在本机短时监听，结束后关闭服务；不连接真实 IdP、T100 或 MetaMCP，不读取旧环境，不作为生产启动脚本。动态测试密钥不得进入产品包或检查输出。

预期退出码 0 表示该次自检通过，1 表示失败。成功 JSON 摘要明确 scope=synthetic-reset-candidate、real_business_called=false、persistent_service=false；结果说明必须保持 upstream_acknowledged 的含义，不把模拟 ACK 表述为密码生效。具体输出和实际结论以 [VALIDATION-reset](../../../VALIDATION-reset.md) 为准。协议字段记录本次实际协商结果；本机原生直连不构成 MetaMCP 的 2025-06-18 Profile 或两跳验收。

| 必验场景 | 可观察断言 |
|---|---|
| 独立写工具与 A/B 顺序、真实重叠并发 | 只暴露 reset；每人只发送自己的企业、员工与独立账号，每次新操作最多 1 次实际请求 |
| 同操作 ID 重放、未知后换 ID | 重放 0 次新增请求；未知目标换 ID 也不能派发 |
| 员工/账号参数注入、缺失/错误确认、只读 scope | 固定拒绝，0 次新增请求；不能从参数或读权限得到写授权 |
| 合成 401 拒绝 | 一次候选请求，记 rejected；同 operation_id 不重放 |
| 合成超时 | 最多一次候选请求，记 unknown；原操作及同目标新操作均不再派发 |
| 持久事实与隔离 | 操作和事件逐 ID 核对可信身份、客户端、企业、员工、账号、绑定版本及终态；库重开不丢失占用 |

脚本只覆盖直接协议主线。完整验收另见 [链路测试](../../../tests/test_reset_chain.py)、[报文测试](../../../tests/test_reset_client.py)、[操作库测试](../../../tests/test_reset_store.py)，应覆盖授权/改绑竞争、每个耐久失败点、取消、响应异常、重启以及两个目标各自的并发占用。不以脚本通过替代这些故障测试，也不在本文预填通过数。

## 7. 真实启用仍缺的证据

1. 接口维护方提供专用 reset，或证明当前接口局部更新只重置既有账号：不创建、不启用、不改角色/权限；明确账号与员工关系及错误码的无副作用语义。不能用补默认状态字段来解决旧接口必填冲突。
2. 定标可信真实个人身份、两名员工与各自实际账号的唯一映射、启用许可，以及有效/无效服务 key 的真实拒绝契约。
3. 明确密码的安全交付、强制首次更改、本人验收和结果未知调查方式；取得 ACK 不抵扣这些环节，也不从工号推算密码。
4. 在获准环境分别验证直连与 GOV 两跳：本人隔离、前置拒绝 0 次业务请求、写工具停用/错入口 key、审计关联和失败锁定。GOV 仅登记与发布，业务配置/key/操作库仍归 BIZ。

离线 biz-validated、真实 business-validated 和 GOV published 分别记录。当前产品网关运行或只读查询已验证，都不代表重置工具已真实可用。
