# CORE：FastMCP 开发底座

当前为首阶段工程骨架，提供原生 FastMCP 服务工厂和无业务能力的本地探针。

- Python 3.12，FastMCP 4.0.10；依赖由本项目 `uv.lock` 固定。
- 使用原生 async Tool、类型化输入输出、Streamable HTTP、FastMCP Client。
- 不复制协议栈、Tool注册器、网关、身份平台或数据库框架。
- 本次没有实现 OBO、业务授权、持久审计、关联服务或真实业务连接器。

## 独立开发与验证

在本目录执行：

```sh
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build
```

启动本地工程探针：

```sh
uv run --locked sysmcp-probe --port 8765
```

地址为 `http://127.0.0.1:8765/mcp`；仅含 `core_probe`，无凭据、数据库或外部调用。命令只绑定本机，不提供开放地址参数。结束时按 Ctrl+C。

协议测试分别覆盖FastMCP自身的现代模式与MetaMCP所需的握手兼容模式；网关候选协议为 `2025-06-18`，与现代组件探针的 `2026-07-28` 分开记录。不以 `auto` 回退成功冒称现代协议已通过网关。探针通过仅证明本项目基本构建与协议操作，不代表 GOV、身份委托或业务权限通过。

## BIZ-ADP 如何使用

BIZ-ADP 安装本项目发布的包，调用 `create_service(name, auth=provider)` 得到原生 FastMCP 对象，再用 `@server.tool` 注册自己的工具。`auth` 必须显式提供；鉴权配置和用户委托另行验收，不能把“传入一个Provider”当作安全验收。

业务代码不导入 `probe`。无业务需求之前，不增加通用 Repository、Policy Engine、Task Queue 或新的协议抽象。

规范入口：[BIZ-ADP 开发规范](../../docs/BIZ-ADP开发规范.md)。
