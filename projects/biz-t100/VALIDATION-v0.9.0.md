# BIZ-T100 v0.9.0 验证记录

日期：2026-09-30。范围：四接口兼容基础上的常驻读写装配、现有live.py网络客户端及MetaMCP内部只读接入。正式个人身份后续接入；本轮没有执行业务写入。

## 工程验证

- `.venv/bin/python scripts/offline.py --suite all -- -q`：**1909项通过，149.45秒**。包括原本人链路、四接口、历史未决／审计、常驻授权与动态撤权、网络客户端、临时连接令牌签发；使用合成身份及捕获报文的模拟上游。
- 受影响产品代码、两个入口及新增测试的Ruff检查通过；最终只读审查未发现新增阻塞。
- 原生读工厂仅暴露查询，写工厂暴露三个写工具；原默认四工具工厂兼容。客户端不读T100 key或服务许可；资源验签、目标授权、凭据读取、业务派发、终态及交付复核按原顺序执行。
- HTTP客户端禁用环境代理及307／308重定向回放；工具名只能指向选定动作或其namespace前缀。最终结果落盘后再次核对授权，撤权时剥离数据并保留已执行事实。
- `issue-gateway-token`仅允许既有enabled、query-only主体，匹配公私钥，最长3600秒；0600原子输出，禁止覆盖配置、凭据、审计或业务证据，不读取T100 key。

## 实际常驻与网络验证

| 检查 | 结果 |
|---|---|
| 用户级systemd | read／write两单元active、enabled；unit verify通过；未设置linger或改动旧服务 |
| ready健康 | 172.17.0.1:8781、127.0.0.1:8782均HTTP200；健康本身不调用T100 |
| 直连读发现 | 仅t100_employee_query |
| 直连写发现 | t100_employee_create、t100_employee_update、t100_account_maintain |
| 无BIZ令牌 | 原生MCP初始化HTTP401，未派发业务 |
| 直连真实查询 | 显式2025-11-25，唯一精确匹配；读1次、写0次 |
| MetaMCP真实查询 | 显式2025-06-18，返回资料与直连完全一致；读1次、写0次 |
| 网关错误／缺失Key | 均HTTP401，BIZ操作库没有新增业务操作 |
| 网关工具／服务停用 | 均tool_not_available，BIZ操作库没有新增业务操作；完成后恢复ACTIVE |

实际成功查询共2次上游读取、0次写入。直连请求bf622756-b361-40a3-a076-c6efa15b63a8；网关请求9fe3b861-35ec-4ca7-ab18-a002f38334bb。MetaMCP登记UUID和去敏证据见 [GOV验证](../gov/VALIDATION-internal-t100-20260930.md)。续签连接JWT并更新本次服务后，最终工具发现通过；未重复业务读取验证。

下游客户端分别为local-service-read、metamcp-internal-query，审计记录资源令牌验签主体与客户端。MetaMCP2.4.22不转发外部Authorization，caller_identity_forwarded=false、real_identity_verified=false。它证明受控内部连接，不证明真实员工身份。

## 制品与保存核验

已构建0.9.0 wheel／sdist。独立验证环境仅安装锁定的生产依赖及CORE制品，再安装BIZ wheel：隔离Python导入服务装配、四接口和CLI通过，不依赖源码工作树、tests、scripts或pytest；生产依赖完整性检查通过，uvicorn0.54.0与cryptography50.0.1随生产依赖安装。

制品逐项检查不含.runtime、虚拟环境、私钥文件、服务key、DOCX、业务审计或真实本人资料。最终文件摘要见 [SHA256SUMS](dist/SHA256SUMS-v0.9.0)，检查记录见 [BUILD-VALIDATION](dist/BUILD-VALIDATION-v0.9.0.json)。现版本运行制品重新安装不依赖旧仓目录。

原61份历史制品、legacy_support、四份原DOCX与既有运行许可文件逐一摘要一致。旧写库整文件摘要未变，其5条操作／15条事件保持原样；当前库原21条操作／63条事件逐行摘要一致，仅新增2条查询操作／6条事件。没有删除、改写或伪造unknown终态。新服务沿用两库，不通过新入口解除旧占用。

私有业务及验证材料留在.runtime/service/validation与.runtime/functions/service-cases；公开资料只留去敏结论和请求编号。不提交或推送。

## 当前状态与下一步

本版声明范围离线证据支持biz-validated，实际GOV对象支持gov-registered与composed。business-validated和published仍未通过；原现代协议目标亦未通过。正式IdP、真实员工映射及端到端用户权限另行验收。

当前常驻可用；临时MetaMCP连接JWT一小时到期，需要受控续签并更新登记的Bearer Token，没有自动刷新。日常命令统一见 [运行说明](docs/service-runtime.md) 和 [测试说明](docs/testing.md)。下一步选择真实身份来源，完成两名员工隔离、拒绝与撤权／审计证据，再评估正式发布。
