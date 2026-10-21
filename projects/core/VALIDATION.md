# CORE 首阶段验证记录

日期：2026-09-29。范围：新工作空间内的工程底座，不含MetaMCP或真实业务联调。

## 实际结果

| 验证 | 结果与范围 |
|---|---|
| 独立依赖 | 本项目 `uv sync` 成功，生成自身uv.lock和.venv |
| 本地测试 | 4项通过，无警告；使用真实FastMCP库和本地合成RSA凭据 |
| 现代组件路径 | FastMCP Client显式2026-07-28，tools/list及core_probe调用通过 |
| MetaMCP兼容候选路径 | 本地ASGI HTTP按2025-06-18 initialize，返回该版本；后续list/call通过 |
| 认证边界 | 本地HTTP无Token/错误audience返回401，正确目标audience接受initialize |
| 静态检查 | Ruff检查通过，源码已按Ruff格式化 |
| 打包 | 源码包与wheel构建成功；wheel只含sysmcp_core和包元数据，无tests/scripts依赖 |
| 本机启动 | 独立探针进程绑定loopback随机端口，真实HTTP list/call通过；结束后进程已关闭 |

依赖锁：FastMCP4.0.10、MCP/mcp-types2.2.0、Pydantic2.13.5；Python3.12.3。完整传递依赖见uv.lock。

wheel：`dist/sysmcp_core-0.1.0-py3-none-any.whl`，SHA-256：`1eac4b1b5ed9e8bcf469ec9a92c608e455696db30240ac67af6f29981af3066f`。dist为本地构建产物，不入库。

## 未执行与结论

没有启动MetaMCP、PostgreSQL、Keycloak或旧项目容器；没有登记网关endpoint，也没有真实业务请求。HTTP兼容测试在本地ASGI应用中完成，不等于MetaMCP两跳实际协议已通过。正确audience的合成Token也不证明真实身份委托。

本阶段只接受CORE工程可构建、基本Tool协议可用和显式认证注入边界。MetaMCP组合、用户与客户端来源、业务C、原权限、耐久审计和最终闭环继续分别记录未完成。

## 复现

```sh
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build
```
