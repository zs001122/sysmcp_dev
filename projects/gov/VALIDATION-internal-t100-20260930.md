# T100 内部只读网关验证

日期：2026-09-30。BIZ制品：sysmcp-biz-t100 0.9.0。产品：MetaMCP2.4.22／SDK1.16.0。本次使用真实T100读取和临时本地签名身份，没有执行业务写入。

## 实际登记与发现

服务、namespace、private endpoint均为sysmcp-t100-internal-query，实际UUID见 [服务目录](catalog/services/sysmcp-t100-internal-query.toml)。只创建本次新对象；原有对象和产品部署配置未修改。管理登录资料由用户提供，登录Cookie及新入口Key只留私有.runtime。

服务指向Docker可达的BIZ只读8781单元；写8782单元不登记。GOV唯一工具为sysmcp-t100-internal-query__t100_employee_query。发现的inputSchema来自BIZ原生工具；通过受支持refreshTools保存映射，没有手写虚假Schema。

## 查询对照与拒绝

| 场景 | 结果 | 业务调用 |
|---|---|---|
| 直连原生TCP | 显式2025-11-25，唯一精确匹配 | T100读取1次 |
| MetaMCP两跳 | 显式2025-06-18，资料与直连完全相同 | T100读取1次 |
| 缺失API Key | HTTP401 | BIZ业务执行0次 |
| 错误API Key | HTTP401 | BIZ业务执行0次 |
| namespace工具INACTIVE | tool_not_available | BIZ业务执行0次 |
| namespace服务INACTIVE | tool_not_available | BIZ业务执行0次 |

工具及服务停用分别在一次新连接调用中观察到拒绝，耗时1.663／1.645秒；这些是本次观察值，不是承诺最大生效时限。验证后两者恢复ACTIVE。无效Key和启停前后核对BIZ操作库行数，未增加业务操作；不把产品后台工具发现算作T100业务读取。

对照请求编号：直连bf622756-b361-40a3-a076-c6efa15b63a8；两跳9fe3b861-35ec-4ca7-ab18-a002f38334bb。BIZ审计均保存验签主体／客户端、企业、动作、批准目标、请求摘要、派发和终态；真实资料只在BIZ私有证据中。

## 身份与验收边界

MetaMCP2.4.22下游使用登记的Bearer Token，不转发外部Authorization。BIZ验签得到metamcp-internal-query客户端，限制既有批准查询范围；不把X-API-Key或查询参数当员工身份。real_identity_verified=false，caller_identity_forwarded=false。

临时连接JWT最长3600秒，受控签发工具已完成离线验证，实际续签后以mcpServers.update更新本次服务，随后工具发现通过。没有自动刷新或永久测试令牌，续签步骤见 [运行说明](../biz-t100/docs/service-runtime.md)。

当前完成gov-registered与composed。正式个人身份、两名用户隔离、完整真实权限／业务效果、现代协议目标和重新部署的镜像固定仍单独验收；published、business-validated均未通过。BIZ构建和回归证据见 [v0.9.0](../biz-t100/VALIDATION-v0.9.0.md)。
