# BIZ-T100 v0.4.0：本人密码重置离线验证

日期：2026-09-29。结果：**457 passed in 22.34s**；Ruff check、33 个文件格式检查通过。状态为 **biz-validated（合成身份＋模拟上游＋候选重置契约）**，不等于真实接口、密码生效或发布通过。

## 范围与证据

同一独立 BIZ-T100 项目增加 `reset_my_t100_password(confirm, operation_id)`，使用独立写工厂、写 scope、默认关闭许可和 SQLite 操作库。原查询工厂、工具与查询 Schema v2 保持兼容；没有修改 CORE/GOV 或旧项目。

| 测试范围 | 数量 | 证明内容 |
|---|---:|---|
| 原查询/授权/运行/身份矩阵及新增许可类型负例 | 290 | 查询行为保持；新许可拒绝隐式类型转换 |
| [重置客户端](tests/test_reset_client.py) | 59 | 账号/员工独立、最小三字段报文、ACK-only、边界响应、2MB/时限/取消、零重试、跨企业key隔离 |
| [重置操作库](tests/test_reset_store.py) | 59 | 原子预留、状态与事件共事务、员工/账号双锁、同ID去重、故障/并发/重启、私有文件、拒查询库 |
| [原生重置调用链](tests/test_reset_chain.py) | 49 | 合成签名身份、权限与映射、授权后读key、真实组件捕获报文、审计及写后未知 |
| **合计** | **457** | 全项目回归一次通过 |

完整链路包括：

- A/E001/LOGIN-A 与 B/E002/LOGIN-B 的顺序及真实重叠并发；企业、员工、账号和管理员key分别核对。
- 未映射、停用、缺写许可、缺账号、错误客户端、重复映射、一个账号绑定不同员工：前置拒绝，不读key、不调用上游。
- 缺失/过期/伪造令牌、错误audience、只有read scope、工号/账号/密码等参数注入：零上游。权威未选定的employee claim、Header与MetaMCP key不能改变目标。
- confirm 必须严格true，operation_id必须规范UUID。同ID顺序/并发/重启不重发，不向其他主体返回历史结果。
- 前置耐久记录后撤权/改绑为零请求；派发标记后再次重验。写后撤权/改绑不回成功，保留unknown与双目标占用。
- 401/403模拟明确服务拒绝各1次；超时/取消/5xx/未知业务码/未知响应记unknown或保留dispatched，不自动重试；换ID仍拒绝同目标。
- 开始/派发登记失败零请求；终态失败不回成功且保留待核对状态。成功终态提交后预算到期也不交付成功，不追加矛盾终态。
- 合法只读授权生成的EmployeeGrant不能充当PasswordResetGrant；查询许可不会获得重置能力。

## 本机真实端口验证

两脚本均使用临时127.0.0.1 TCP、原生FastMCP Client、动态内存RSA签名、合成映射/服务key与MockTransport，退出时关闭端口和清理临时材料。

| 入口 | 结果 |
|---|---|
| `scripts/direct_check.py` | 查询8项检查通过、4次模拟上游请求，A/B本人隔离与审计保持 |
| `scripts/direct_reset_check.py` | 重置8项检查通过、6次模拟请求、6操作/18事件；4个ACK、1个明确401拒绝、1个超时unknown；重复/注入/缺确认/read scope前置拒绝，未知换ID仍锁定 |

实际原生客户端协商协议为 `2025-11-25`。此结果不替代MetaMCP兼容Profile `2025-06-18` 的两跳验收；重置ASGI调用链另显式覆盖该Profile下的list/call。查询历史现代协议证据不能自动当作重置现代能力已测。

## 独立制品

- 版本 `sysmcp-biz-t100==0.4.0`；CORE仍通过0.1.0公开wheel安装。
- 本项目 `uv lock --offline` / `uv sync --locked --offline` / `uv build --offline` 完成；项目版本升级，未新增第三方依赖。
- v0.4.0 wheel在 `/tmp/sysmcp-t100-v040-pc90xwn5/venv` 独立安装，通过 `python -I` 从site-packages导入查询/重置工厂及操作库，脱离工作区源码。
- 安装制品默认创建真实重置客户端返回 `business_adapter_not_ready`；无布尔配置绕过、无真实写runtime。
- Wheel仅产品模块和包元数据；源码包保留开发材料。归档检查未含私钥PEM、.key/.env或业务数据库；测试私钥只在运行时生成。
- 当前wheel和lock摘要见 [服务发布描述](docs/service-release.md)，历史v0.3制品与记录未覆盖。

## 尚未通过的真实条件

旧API是 `user.account.create`，字段表要求状态，旧builder默认启用账号。当前三字段password patch只被模拟上游验证，真实接口是否接受、是否无建号/启用/角色副作用尚无证据。裸 `execution.code=0` 仅表示上游应答；没有目标读回、密码交付或登录生效证明。

因此不开放真实写默认路径。需接口维护方提供专用仅重置接口或已验证的等效契约、本人账号归属及许可、有效/失效服务认证、密码交付/强制更改与结果未知核对办法，再独立进行business-validated和GOV准入。没有执行真实查询/重置、迁移旧秘密或个人数据、发布网关对象、提交或推送。

复用、设计与操作规范见 [RU-003](../../docs/reuse/RU-003-T100本人密码重置.md)、[ADR-006](../../docs/架构决策-006-T100本人密码重置.md)、[密码重置契约](docs/password-reset.md)。
