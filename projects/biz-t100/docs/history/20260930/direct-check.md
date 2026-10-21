# BIZ-T100 一次性直连自检

> 历史阶段说明。当前统一使用 [四接口与两类测试入口](../../testing.md)，本文旧命令仅作为历史回归背景，不再作为日常运行入口。既有运行资料与审计保留。

日期：2026-09-29。用途：先确认BIZ核心服务能通过真正的本机HTTP端口被MCP客户端直接调用，不依赖MetaMCP、真实身份系统或真实T100。

## 执行

在已安装本项目锁定开发依赖的环境中：

```sh
cd /data/projects/sysmcp_dev/projects/biz-t100
.venv/bin/python scripts/direct_check.py
```

自检明确使用开发用合成身份、企业及员工号。它在127.0.0.1随机空闲端口启动临时服务，使用原生FastMCP客户端发现工具并调用，输出JSON结果，然后关闭服务、删除临时策略/key/SQLite。无需指定端口、登录、填真实配置或启动网关。

此文件与脚本是开发验证入口，不是产品serve命令，不作为常驻服务或生产装配。产品代码不导入scripts/tests，现有v0.3制品不因这个开发脚本改变。

## 实际经过的链路

原生MCP客户端 → 本机TCP/Streamable HTTP → FastMCP签名身份验证 → FileServiceAccountAccess本人映射/工具许可 → T100EmployeeClient构造报文 → MockTransport捕获实际报文 → 返回精确员工校验 → SQLite审计。

BIZ应用使用公开create_app工厂，调用链中的身份、授权、业务客户端和审计都是现有产品实现。上游被显式模拟，永不连接真实T100。签名私钥只在内存生成，管理员key也是本次随机合成值；均不打印或写入仓库。

## 本轮实测

脚本退出码0、status=passed：

| 检查 | 结果 | 模拟上游请求数 |
|---|---|---|
| 发现工具 | 仅get_my_t100_employee_record | 0 |
| 用户A | DEMO-ENT / E001 | 1 |
| 用户B | DEMO-ENT / E002 | 1 |
| A/B并发 | 通过屏障确保重叠，各自得到本人结果和独立request_id | 合计2 |
| 指定他人工号参数 | invalid_arguments | 0 |
| 未映射用户 | identity_not_enabled | 0 |
| 缺令牌 | HTTP401/403拒绝 | 0 |
| 审计 | 4次成功调用的主体、客户端、企业、工号、工具、调用尝试和终态一致；无未决 | 不新增 |

合计4次模拟上游请求；无真实业务调用，无常驻服务。

原生客户端本轮legacy协商结果为 **2025-11-25**。这只是当前直连协商记录，不替代既有2025-06-18兼容Profile测试，也不宣称MetaMCP两跳通过。

同时执行既有直连身份链及运行装配测试：**68 passed in 7.90s**。它们使用进程内ASGITransport，完整故障矩阵见[离线验收](../../offline-acceptance.md)。一次性端口自检与完整矩阵互补，不能用少量自检代替全部回归。

```sh
.venv/bin/python -m pytest -q tests/test_identity_chain.py tests/test_runtime.py
```

## 如何判定

退出码0且status=passed，说明本次核心直连检查通过；非0为失败，不将未完成检查算作通过。脚本错误只输出固定检查标识，避免回显令牌和配置。

这是biz-validated范围内的开发验证。它证明本机服务监听、原生MCP发现/调用及BIZ核心链路正常；不证明真实身份来源、真实T100接口或MetaMCP发布可用。真实接入仍按[运行输入](../../integration-inputs.md)和[发布说明](../../service-release.md)分别验收。
