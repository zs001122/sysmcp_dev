# BIZ-T100 v0.9.1 账号网关验证

日期：2026-09-30。本版将既有账号禁用、启用和密码重置接入MetaMCP。日常仍只有offline.py／live.py两个脚本，复用现有四接口、操作库及当前运行许可。

## 代码与工程证据

`.venv/bin/python scripts/offline.py --suite interfaces -- -q`：**675项通过，43.37秒**。覆盖四接口、账号profile、常驻发现／调用、连接签发、客户端、审计与历史占用。受影响文件Ruff通过，最终源码复核通过。0.9.0的1909项全量结果是历史证据，不记成本版全量结果。

maintenance模式在同一个FastMCP实例承载四接口；工具发现逐主体过滤。账号网关profile仅接受五字段和N/N、Y/N、Y/Y三个状态／重置组合；复制、detail（包括[]）、N/Y及未知参数前置拒绝，未读取服务key或派发上游。原full能力保持兼容。共享网关JWT仅允许query＋account两动作、status-password、禁止宽泛搜索、非空批准员工范围及账号对的enabled主体；账号员工须属于批准查询范围。只签两scope，最长3600秒。旧read/write签发保持兼容。

发现结果按部署动作、可信JWT scope、主体许可交集过滤；直接调用未获工具scope的动作仍拒绝。合成测试证明account-only连接只见一个工具，full三动作许可仍能见三个；当前真实本机许可只有更新／账号两动作，故本机发现两个工具。

## 实际部署与网关

当前仅sysmcp-biz-t100.service一个常驻单元，172.17.0.1:8781/mcp，mode=maintenance，允许host.docker.internal；active／enabled、ready200。旧读写两单元已stop／disable，8782不再监听。引用原操作库、旧未决库及T100配置，不放宽原本机目标许可。

MetaMCP仅登记sysmcp-t100一个MCP server，原query和新account两个namespace／private endpoint共享该登记，各自只ACTIVE一个工具；开启API Key、关闭OAuth和query参数Key。先预置白名单、再替换namespace服务关系，验证后通过受支持API移除被替代的两条测试服务登记，原登记文件保留。UUID见 [GOV接入](../gov/docs/t100-account-onboarding.md)，不直接改产品数据库。

| 实际检查 | 结果 |
|---|---|
| 账号网关工具发现 | 显式2025-06-18；仅sysmcp-t100__t100_account_maintain |
| 原查询工具发现 | 仍为唯一sysmcp-t100__t100_employee_query |
| 本机写工具发现 | 显式2025-11-25；更新和账号维护，未增加员工建档许可 |
| 三份本人用例许可 | check_authorization全部allowed，不读key、不调用T100、不写审计 |
| 跨端点直接调用 | 查询端点不能调用账号工具，账号端点不能调用查询工具；0次BIZ业务执行 |
| 三份请求跨网关门禁 | 实际tools/call(confirm=false)均confirmation_required；验签客户端metamcp-internal-maintenance，0次上游 |
| 错误／缺失API Key | HTTP401，0次BIZ业务执行 |
| 工具／服务停用 | 发现为空；直接RPC调用仍被拒，未新增BIZ用例或业务审计；随后恢复ACTIVE |

本轮真实T100读取和写入均为0。此前用户手测和成功账号回执保留，本轮没有为登记重放禁用／启用／密码重置。真实写操作的三条命令已就绪，仍需--confirm；ACK须以账号资料或登录确认，员工status不能用于验收账号状态或密码。

## 制品及保存

本版wheel和sdist另行构建，摘要见 [SHA256SUMS](dist/SHA256SUMS-v0.9.1)，检查见 [BUILD-VALIDATION](dist/BUILD-VALIDATION-v0.9.1.json)。制品不含私有.runtime、私钥、真实本人资料、服务key或业务审计；独立wheel安装使用锁定生产依赖，产品不导入tests／scripts。原0.9.0及更早制品不覆盖。

两份操作库从本轮开始到完成整文件摘要不变，原unknown及全部历史记录保持原样。新装配保留原本机动作／目标范围及原functions运行许可；旧签名配置和两单元文件留档，新共享网关主体限定两scope。两个namespace和端点UUID保留，服务引用及工具前缀按用户要求更新。真实员工范围和本轮三份原始请求仅留BIZ私有.runtime。

## 状态与使用

本版组件支持biz-validated，内部两入口为gov-registered／composed；real_identity_verified=false，外部用户JWT不透传，business-validated和published仍未通过。账号连接JWT一小时到期，按 [运行说明](docs/service-runtime.md) 续签并更新MetaMCP Bearer Token。

日常三种操作使用现有 [测试说明](docs/testing.md) 的账号网关预填命令。没有新增测试模式、审批脚本或恢复框架，没有提交或推送。
