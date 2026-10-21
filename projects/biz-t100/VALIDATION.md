# BIZ-T100 v0.1 历史验证记录

当前v0.2服务账号模式与新增实现见[服务账号验证记录](VALIDATION-service-account.md)。下文保留前轮116项离线验收及当时的待定假设；其中“key权限模型未知/尚无Access实现”的状态已被后续用户澄清及实现更新，不作为当前阻塞。

日期：2026-09-29。状态：**离线开发包验收通过；真实部署、员工委托和业务权限未验收。** 关联 [RU-001](../../docs/reuse/RU-001-t100本人档案.md)。本记录只描述本次实际执行，不继承源项目历史测试结果。

## 已执行

| 检查 | 实际结果 | 边界 |
|---|---|---|
| 独立环境与锁文件 | Python 3.12.3；uv sync/locked运行成功 | 项目自己的 .venv/uv.lock；首次缓存缺HTTP依赖后下载公开包 |
| 异步 T100 客户端 | 61项通过 | 全部 MockTransport/合成凭据；报文、精确目标、类型/字段、URL、业务码、重定向、2MB上限、网络失败、超时、取消/关闭 |
| 查询持久记录 | 23项通过 | 临时SQLite；重开耐久、旧库/目录/软链接/权限拒绝、提交失败、未决请求、取消不抛弃提交线程、目录同步顺序 |
| 完整服务边界 | 32项通过 | 真实原生FastMCP与签名验证，本地ASGI；单一工具、Schema、身份隔离、零上游拒绝、审计、授权重验及去敏输出 |
| 合并执行 | **116 passed in 6.47s** | 没有业务、IdP或网关请求 |
| MetaMCP兼容组件Profile | 2025-06-18 initialize/list/call通过 | 原生HTTP组件测试，未启动MetaMCP |
| 现代组件Profile | 2026-07-28 list/call通过 | 使用该协议原生元信息；不代表网关端到端现代协议通过 |
| 完整本地链 | 合成JWT → 本人映射 → 耐久记录 → T100模拟HTTP → 精确目标 → 返回通过 | 一次上游请求，输出无原始敏感字段/凭据，未决列表为空 |
| Ruff | check及format --check通过 | 11个Python文件 |
| 构建 | sdist和wheel成功 | 初次出现pathspec警告；收尾修正根忽略规则后重新构建核对 |
| 独立制品安装 | 临时独立环境按锁导出版本安装两个wheel成功 | `python -I` 导入BIZ与CORE均来自临时site-packages，sys.path无/data/projects源码 |
| 包内容检查 | wheel仅6个产品Python文件与包元数据 | 无tests/scripts/缓存/旧源码/配置/凭据 |

锁定核心版本：FastMCP4.0.10、MCP/mcp-types2.2.0、Pydantic2.13.5、httpx0.28.1、httpx2/SDK传输2.13.1、anyio4.15.1。完整版本以uv.lock为准。

制品摘要：

- `dist/sysmcp_biz_t100-0.1.0-py3-none-any.whl`：`73839326c2a02bf6fb504207755c54f2cecc477c56caaf29395211e69f79005a`。
- `uv.lock`：`345c50cf4aa6a0e1c0c69b23c93cc7c87c7cdca33ab2505a70e38e0e37a0f788`。
- 所依赖 `sysmcp_core-0.1.0-py3-none-any.whl`：`1eac4b1b5ed9e8bcf469ec9a92c608e455696db30240ac67af6f29981af3066f`。

制品在dist中由本地构建产生，不提交二进制缓存。独立安装检查环境为 `/tmp/sysmcp-t100-package-mawa26od`，临时路径不是运行依赖；可按README重新构建复验。

## 独立审核及修复

runtime_architecture独立检查服务/授权/审计，storage_contracts实现并核对持久记录，主代理完成集成验证。审核中发现并修复：

1. 线程内持久提交可能超过取消预算：增加绝对截止时间和派发/输出检查；过期不能再查询或交付数据；已提交成功终态不再插入矛盾终态。
2. 异步授权等待期间令牌可能过期：等待后再次检查，再决定派发/返回；另覆盖上游返回后撤权。
3. 新建日志目录需要同步其父目录：补逐级父目录同步及失败拒绝验证；不宣称完成断电/灾备演练。
4. Pydantic model_copy可绕过URL构造校验：客户端构造时重验配置，回归覆盖HTTP降级拒绝。

以上修复均有定向测试。业务预算到期不会开启新业务查询或返回成功数据；由于本地磁盘提交不可中途抛弃，故障清理不承诺硬实时响应。

## 未执行与下一步

- 未修改或运行 account_mt_v2/mcp_auth_demo 的服务与脚本；来源文件摘要核对一致。没有读取或复制真实凭据、目录配置、业务数据和历史环境。
- 未启动本项目监听进程，未部署MetaMCP或数据库容器；当前只提供显式依赖的服务/ASGI工厂，没有默认真实启动装配。
- 未完成真实EmployeeAccess：T100 key是否按员工/委托范围授权、原系统权限保证、有效性/撤销策略仍待核实。合成提供方只在tests中，不打包。
- 未完成网关到业务服务的员工身份及原客户端归因，也没有原API两类用户权限对照；API Key访问成功不能替代。
- OA真实接口、写入接口、原一期一读一低风险写入尚未完成。

下一工作包先明确T100授权与错误码契约，复核已有员工映射、测试环境和责任人；条件成立后实现真实Access并直连做允许/拒绝对照，再通过MetaMCP验证同一只读工具。网关纯工程连通可独立推进，但不能据此开放真实业务。

## 复现

```sh
cd /data/projects/sysmcp_dev/projects/biz-t100
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build
```

离线验证不要求真实 .env、Token、T100/OA地址或旧项目路径。首次安装第三方依赖需正常包源；之后缓存齐备时可离线安装。
