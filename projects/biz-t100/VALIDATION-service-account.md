# BIZ-T100 v0.2 服务账号模式验证

日期：2026-09-29。模式依据：用户明确T100接口服务采用服务级管理员权限，见[ADR-003](../../docs/架构决策-003-T100服务账号授权.md)。本记录是新增授权实现的离线证据，不覆盖[v0.1历史验证](VALIDATION.md)，不声明真实T100或网关验收通过。

## 本次改变

新增FileServiceAccountAccess：显式service-account模式、受控issuer/sub/client到本人工号和工具许可、默认禁用、授权通过后读取私密服务key、每次重读与策略摘要改绑检查。新增配置示例默认禁用；不含真实身份或凭据。原工具输入输出和只读范围不变。

新增policy_unavailable、credential_unavailable固定错误码，分别表示本地策略和凭据不可用；它们不等同于T100已收到请求后的服务认证拒绝。每个系统继续独立决定执行模式，T100事实不扩大OA/CRM权限。

## 实际执行

| 检查 | 结果 | 证据范围 |
|---|---|---|
| 独立环境 | uv sync --offline成功，包版本0.2.0，依赖锁更新 | 第三方依赖未升级；CORE仍为0.1.0 wheel |
| 新增授权组件 | 74项通过 | 显式模式、默认拒绝、主体/客户端隔离、两员工同key但目标独立、策略热更新/撤权、key轮换、SHA-256绑定、私密文件/UID/权限/软硬链接/FIFO/大小/UTF-8、拒绝前不读取key |
| 完整服务 | 36项通过，其中新增4项 | 真实FastMCP和合成签名HTTP → 真实FileAccess及临时SQLite → 模拟T100；两用户本人隔离、未映射零请求、策略/凭据失败、开始记录后撤权 |
| 原T100客户端与查询记录 | 61＋23项通过 | 全部本地HTTP替身和临时文件/SQLite |
| 合并测试 | **194 passed in 7.74s** | 全部离线，无真实身份提供方、T100/OA或网关请求 |
| 静态检查 | Ruff check、format --check通过 | 当前源码、测试及配置的检查通过 |

独立审核：runtime_architecture编写新授权负例并只读复核实现，未发现本地授权阻断；主代理完成跨组件集成。审核结论限当前服务账号＋本人策略，不证明员工ERP ACL继承。

## 准确的结论与剩余工作

权限模型已经明确，FileServiceAccountAccess已实现；不再把“获取员工T100凭据/OBO”或“判断key是否管理员”列为前置条件。业务身份仍必须来自可信入口，服务管理员key不能把未授权MCP调用者变成管理员。

仍待实际运行输入：受控允许清单/已确认本人映射、真实AuthProvider与撤销策略、开发方固定测试API和错误码、服务key私密交付/轮换、MetaMCP后的身份/客户端来源。真实验收分别验证原API服务key的允许/拒绝和BIZ的用户级本人隔离；不要求上游用两个员工凭据产生权限差异。

本轮未读取真实key/旧配置，未启动服务或容器，未向真实业务发请求，未修改account_mt_v2或mcp_auth_demo。正常服务管理员模式不是无权后的回退；失败始终拒绝，不恢复旧key、旧环境或其他系统凭据。

## 复现

```sh
cd /data/projects/sysmcp_dev/projects/biz-t100
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build
```

制品构建与摘要在本文件末尾记录。本轮版本升级未改变工具名称和输入输出；改动为显式授权模式、可配置授权提供方及相关错误码，部署必须使用本轮契约和策略格式。

## 制品验证

源码包与wheel构建通过，无构建警告。v0.2 wheel已安装到工作区之外的独立环境，python -I导入BIZ、CORE和FileServiceAccountAccess成功；来源均为该环境site-packages，不依赖旧仓或当前源码路径。wheel只有产品模块及元数据，不含tests、配置、缓存或凭据。

- `dist/sysmcp_biz_t100-0.2.0-py3-none-any.whl` SHA-256：`a79cfcff37bf52b2f58ce98c75093eb5cfc08d6c3889d577547966310c1858da`。
- `uv.lock` SHA-256：`0d41558778b93fecb9a244a91eaf40cafe802c4240202d9ba72deafc2eb62b3c`。

所依赖CORE仍是0.1.0 wheel，未修改；本轮没有改变其已记录的摘要。
