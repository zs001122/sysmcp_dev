# BIZ-T100 内部运行与排查

适用版本及固定 wheel 部署 **0.9.2**；核对日期 **2026-10-21**。日常业务与离线命令的唯一指南为 [测试说明](testing.md)，产品 CLI、systemd 和连接令牌运维见 [运行说明](service-runtime.md)。当前一个常驻 FastMCP 服务及一个 MetaMCP 服务登记承载四接口，查询与账号端点区分暴露工具。

2026-10-21 已切换固定 wheel、重启统一服务、恢复原 MetaMCP 容器并更新原登记的连接令牌，见 [部署验证](../VALIDATION-deployment-20261021.md)。沿用原业务许可和审计，本次 T100 业务读写为 0。`live.py --client-config` 连接常驻服务或 MetaMCP；省略客户端配置时沿用本机 FastMCP ASGI 真实入口，不依赖常驻端口。两条调用路径均使用原有 T100 配置、共享审计和历史未决检查，不增加第三种日常脚本。开发 `.venv` 与常驻 `.runtime/releases/0.9.2/.venv` 分离，更新开发源码不会自动更新运行服务。

## 配置归属

| 私有位置 | 用途 |
|---|---|
| .runtime/service/server-maintenance.json | 当前统一常驻服务装配：验签公钥、上游／许可／审计引用及监听地址 |
| .runtime/service/maintenance-permissions.json | 常驻服务的主体、客户端、动作及员工／账号范围许可 |
| .runtime/service/client-read.json、client-write.json | 同一服务的查询与维护客户端，持有各自签名配置，不持有 T100 key |
| .runtime/service/client-gateway.json、client-gateway-account.json | 查询／账号端点客户端及各自网关入口 Key 引用 |
| .runtime/service/gateway-maintenance-token.jwt | 供 MetaMCP 单一登记使用的短期 BIZ 连接令牌，不是网关 API Key 或 T100 key |
| .runtime/live-read/runtime.json | 既有上游配置、企业号和服务凭据路径；目录名称是历史命名 |
| .runtime/functions/permissions.json | 本机内部测试及常驻部署的写动作／目标约束、历史对象分类 |
| .runtime/test-cases/owner/ | 已批准的本人请求文件与私有说明 |
| .runtime/functions/cases/ | 本机请求意图、有效报文、受限回执和结果 |
| .runtime/functions/service-cases/ | 常驻服务请求意图、有效报文、受限回执和结果 |
| .runtime/functions/audit/operations.sqlite3 | 当前四接口耐久审计 |
| .runtime/live-write/audit/account-write.sqlite3 | 旧审计，只读核对，不作为新写入口 |

运行文件由当前服务账号拥有、权限0600；私有目录0700。凭据保留独立文件，不内嵌业务参数、公开配置、GOV登记或制品。既有 .runtime 路径继续使用，收敛不迁移数据库、密钥或调用资料。

## 如何判断是否到达上游

先看中文操作对象、状态说明及 `upstream_attempted`、`write_upstream_attempted`、`read_requests`、`write_requests`。`tools` 仅做 MCP 工具发现，成功也不会发送 T100 请求；查询命令才读取业务资料。账号维护先查询员工档案存在性，再发送账号写入，因此 `upstream_attempted=true` 不一定表示账号已写入，应同时看写入标记及次数。

前置参数、身份或目标许可拒绝不会发送业务请求。远程失败若 `mcp_tool_call_attempted=true` 且缺少上游调用字段，表示工具请求已尝试、有效响应未取得，不能判断 T100 请求数为零。应核查服务端审计和证据；已经尝试写入的失败不能通过新编号试探重放。

request.json 是输入意图；effective-request.json 是实际业务报文；upstream-write-response.json 是经过字段筛选的回执；result.json 是当次结果。账号 acknowledged 是回执级成功。员工回读不能验证账号状态或密码。

查询本地操作记录使用 `live.py status`，格式见主指南，不带 `--client-config`，不重新访问 T100。产品 `sysmcp-t100` 已提供 `check-service`、`serve`、`issue-gateway-token` 等常驻运维命令，不能再描述为仅有早期本人配置检查功能；它不是额外的日常业务测试脚本。

## 工具范围与连接问题

| 现象 | 核对方式 |
|---|---|
| read 客户端只有一个工具 | 正常，只能查询；write 客户端连接同一服务，当前可见员工更新与账号维护两个工具 |
| 维护客户端看不到员工新建 | 当前服务端未授予建档动作；客户端 scope 不能独立开放它 |
| 姓名／部门检索被拒绝 | 通用参数模型支持检索，但当前服务主体 `allow_search=false`，须查询批准工号 |
| 直连失败 | 检查统一 unit、8781 地址、客户端与服务端 issuer／audience／客户端及签名配置；服务不使用环境代理或跟随重定向 |
| 网关发现失败而直连正常 | 检查 namespace／工具启用、入口 Key、MetaMCP 下游连接令牌有效期及其所存配置 |
| 已续签文件但网关仍失败 | 私有 JWT 文件更新不会自动更新 MetaMCP 的 Bearer token，按运行说明通过受支持管理界面／API同步 |

`/health/live` 证明进程存活，`/health/ready` 检查配置、许可及审计准备；两者不调用 T100，也不证明个人身份或账号效果。现有两个网关端点共享一个受控下游主体；MetaMCP 2.4.22 不透传外部 JWT，用户级 API Key 不能替代 BIZ 业务授权或员工映射。

## 请求防重与业务幂等

现有请求编号防重只能识别同一操作，未知目标占用阻止未核查写入的继续派发；二者不等于跨请求业务幂等。当前没有业务幂等键或成功结果复用接口，相同成功请求重新提交仍可能再次调用 T100。不要通过重跑密码重置核查结果，也不新增手工幂等步骤；正式写发布前按 [开发清单](development-checklist.md) 明确上游支持、键作用域、输入冲突、并发和未知结果契约。本轮不迁移审计 schema。

## 失败与未决记录

| 情况 | 处理 |
|---|---|
| 空工号／空账号 | 使用预填请求文件或明确业务值 |
| 配置／范围不匹配 | 核对当前常驻主体许可与部署写约束中的动作、员工／账号配对；本机入口核对本机许可，不将查询许可等同写许可 |
| 上游明确拒绝 | 查固定错误码及受限回执，修正业务输入后按授权范围处理 |
| 超时、回读差异、记录失败 | 核查原请求事实；unknown不自动改为失败或成功 |
| 历史员工更新unknown | 保留档案事实，与账号对象分类处理，不据旧名称推定账号操作 |
| 账号自身unknown | 同账号后续写入仍阻止，不自动重试或解除 |

0.9.2 本机临时签名主体固定 `temporary-local-operator`，密钥及令牌仍每次临时生成；该主体只用于内部测试，`real_identity_verified=false`。实际业务使用上游管理员服务凭据，BIZ 授权通过后才读取 key，不宣称继承员工 ERP 权限。正式身份和 MetaMCP 发布条件见 [集成输入](integration-inputs.md)。旧 read／write unit 已停用；旧端口、旧宿主和恢复步骤留在 [运维历史快照](history/20260930/operations.md)，不能据旧说明断言当前监听状态。

统一维护入口是后续已确认需求，当前未实现；身份、员工／账号映射、BIZ 授权与 GOV 发布关系仍分别归属各层。需求状态及边界见 [DEC-001](../../../docs/项目讨论与决策记录.md)。本页更新不改变运行配置、历史审计或业务数据。
