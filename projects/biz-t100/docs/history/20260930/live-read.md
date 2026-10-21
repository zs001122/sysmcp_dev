# 真实 T100 只读测试入口

> 历史阶段说明。当前统一使用 [四接口与两类测试入口](../../testing.md)，本文旧命令仅作为历史回归背景，不再作为日常运行入口。既有运行资料与审计保留。

日期：2026-09-29。版本0.5.0。用户已确认旧test入口与生产数据一致，test/http-test是历史配置或传输名称，不表示独立测试数据。当前运行模式 `live-t100-read-with-temporary-identity`。业务请求通过当前产品 HTTP 客户端发往用户已确认的 account_mt 旧测试接口；**不使用 MockTransport，不从本地 fixture 回填结果**。个人身份尚未接入正式 IdP，因此仅身份签发与本人映射保留为临时受控配置。

## 当前使用

常驻地址：`http://127.0.0.1:8768/mcp`，仅本机可访问，单进程。该端口已从旧单员工模拟用例切换为真实只读；原8766/8767模拟监听也已停止，历史状态、密钥和审计保留。

```sh
cd /data/projects/sysmcp_dev/projects/biz-t100
.venv/bin/python scripts/live_read.py status
.venv/bin/python scripts/live_read.py call

# 指定工号的本地独立只读测试（E001为合成示例，替换成实际工号）
.venv/bin/python scripts/live_read.py call --employee-no E001
```

两种调用方式均经过原生FastMCP验签、客户端/scope/受控映射、授权后读取服务key和真实T100客户端，不自动重试。

| 调用方式 | 工号来源 | MCP传输与审计 |
|---|---|---|
| `call` | 现有read/access.json默认映射 | 连接常驻8768，审计在read/audit |
| `call --employee-no E001` | 本机操作者为本次创建独立受控映射 | 本进程ASGI HTTP走原生认证/工具链路；真实上游照常调用；每次独立operator-cases目录和审计，不要求8768启动 |

指定工号模式是本机测试配置入口。每次生成不同临时主体，MCP工具入参仍为{}；不会修改默认工号、复制服务key或私钥，也不会把任意工号参数开放给远程本人查询工具。命令不能同时带--user或--token-file。工号使用现有1—128字符Identifier规则，不自动去空格、猜姓名或转换数字。

调用启动前检查本地基础测试许可；建立的案例映射及审计属于该次独立测试，修改默认映射不会串改正在执行的其他案例。每个案例仍执行原有映射重验，案例目录保留供核对。输出requested_employee_no、test_mode、mcp_transport、case_path及audit_path，明确本次目标和证据范围。本地ASGI通过不冒充8768端口或MetaMCP验收。

结果中的工号、姓名、部门与状态来自真实上游。空结果现返回employee_not_found和中文message，多条/错号分别返回employee_not_unique/upstream_employee_mismatch，完整分类见[错误说明](../../error-codes.md)。

已对用户指定员工完成真实查询，收到唯一且工号完全相等的档案，结果和SQLite审计关联成功。详情见 [真实验证记录](../../../VALIDATION-live-read.md)。用户指定的姓名/工号、服务地址/企业号/凭据均只保存在私有运行配置和必要审计中，本页不复制。

status 的 ready 只表示本地配置、验签装配、审计和监听可用，不代表上游健康或认证通过。call 的 remote_response_validated=true 才表示本次返回通过唯一记录＋工号完全相等检查。upstream_attempted 来源于该request_id的终态审计，表示进入客户端；网络失败时它不证明远端收到报文，没有终态时保留null。

## 配置与启动

受控目录 `.runtime/live-read`：

```text
runtime.json
issuer-public.pem
issuer-private.pem
tokens/
source-reference.json
read/
  access.json
  service.key
  listener.json
  listener.lock
  listener.log
  audit/query.sqlite3
operator-cases/
  <case-uuid>/
    case.json
    access.json
    audit/query.sqlite3
```

目录0700、文件0600，禁止链接；不进入Git或wheel/sdist。当前配置按 [RU-002§6](../../../../../docs/reuse/RU-002-account-mt运行资料.md) 静态评估并定向提取test环境，不导入或执行旧程序，不迁移旧共享入口token、另一套prod配置、日志或数据库。key是独立受控文件，服务端验证身份仅用公钥，不读取签名私钥。

重新启动使用：

```sh
.venv/bin/python scripts/live_read.py serve --port 8768
```

本次后台实例已保留运行，不要重复启动。脚本默认空闲测试端口为8769，本次明确选择8768；后续保持上述显式参数。所有命令支持 `--state-dir /absolute/private/live-read`。runtime.json中的策略/key/审计路径必须位于该目录的固定位置。URL、企业、身份profile和公钥改动需重启；策略撤权/停用由每次调用重新校验。

停止前核对status中的PID及脚本身份，再正常SIGTERM；前台启动使用Ctrl-C。停止不会清空审计，不能用删库消除未知请求。

## 身份和外部客户端

临时issuer、audience和唯一client由私有runtime配置确定。固定临时主体 `simulated-live-user` 按受控issuer+subject映射到获准工号；姓名、Header、MetaMCP Key和工具参数都不能选择目标。正式IdP到位后替换身份配置和映射、停用临时密钥，再独立验收真实身份。

给本机其他MCP客户端签发令牌：

```sh
.venv/bin/python scripts/live_read.py token --output /data/projects/sysmcp_dev/projects/biz-t100/.runtime/live-read/tokens/client-read.jwt
```

目标必须为tokens下新的0600文件，不能覆盖。默认300秒，允许1–3600秒。客户端从受控文件加载Bearer token，不在聊天、共享配置或命令参数中粘贴。原模拟用例的令牌不适用于此服务。

未映射拒绝验证可运行 `scripts/live_read.py call --user unmapped`，预期失败且upstream_attempted=false。缺令牌、额外工号参数及重置工具调用同样前置拒绝。当前只暴露 `get_my_t100_employee_record`。

## 已发现的真实接口问题

本轮使用随机错误服务key，对同一获准目标执行一次只读探测，接口仍返回有效档案。因此上游服务key校验**未通过**；旧文档的历史问题已被当前测试再次观察到。真实查询可用不等于上游认证有效。BIZ自己的签名身份和本人授权仍有效，但不能替代上游修复；本入口继续保持loopback受控测试范围，不标记business-validated或published。

## 登录账号与重置

员工资料查询不需要登录账号绑定，因此该部分暂存本地或模拟不影响真实查询。工号不自动当作登录账号。

本轮没有执行真实密码重置：现有上游只有通用user.account.create（新增/修改），未证明省略状态/复制/角色/据点字段时只重置密码，且无账号读回契约。临时账号绑定不能补齐这些接口事实，也不能把模拟账号作为真实写目标。真实重置仍按 [独立契约](../../password-reset.md) 定标；该限制不会阻塞当前已运行的真实只读业务。
