# BIZ-T100 v0.3：运行准备与离线代码闭环验证

日期：2026-09-29。结论：**biz-validated（合成身份、模拟上游的离线代码闭环）**。真实个人身份、真实员工/T100业务与MetaMCP发布分别未验收；不依赖其完成本记录中的开发验证。

## 1. 本轮实现

- 严格运行配置、受控文件预检、显式AuthProvider运行装配；构造不监听、不联网、不读key。健康检查只核对本地状态，并发限制为单进程。
- 用户已确认旧HTTP测试入口；新增显式http-test Profile，默认HTTPS保持不变；仅受控RFC1918 IPv4固定目标，不降级、不重定向、不读取代理环境。
- 当前员工号唯一来源为受控issuer+subject映射。工具参数、普通Header、姓名、邮箱、MetaMCP API Key及未启用权威来源模式的签名employee_no不能覆盖映射。
- 审计Schema v2绑定固定enterprise_id/tool_name，记录终态upstream_attempted，能区分派发前拒绝与已进入Reader后的拒绝；不把调用尝试等同远端收到。旧库拒写且不自动迁移。
- 开发和状态规范已同步用户验收要求；来源复用见[RU-002](../../docs/reuse/RU-002-account-mt运行资料.md)，没有搬旧实现或秘密。

## 2. 实际执行结果

| 检查 | 结果 | 证据范围 |
|---|---|---|
| 全量测试 | **287 passed in 13.56s** | 所有请求均为进程内原生FastMCP HTTP及模拟上游，不访问真实T100/IdP |
| 完整身份链矩阵 | 32项通过 | 动态RSA签名→原生验证→真实FileAccess→真实异步客户端/MockTransport→真实临时SQLite |
| 运行装配 | 36项通过 | 默认拒绝、私密配置/路径冲突、构造无I/O、本地健康、并发取消、HTTPS/http-test、配置错误不泄露 |
| 授权组件 | 74项通过 | 唯一映射、默认拒绝、授权后读key、变更/撤权、私密文件边界 |
| T100客户端 | 61项通过 | 请求形状、精确返回、零/多条拒绝、错误和超时；额外HTTP测试Profile在运行测试覆盖 |
| 原生服务 | 36项通过 | 两协议Profile、原生issuer/audience/scope等拒绝、业务时限和故障边界 |
| 查询记录 | 48项通过 | 耐久事实、调用尝试标记、企业绑定、只读检查、无事件prepare、v1拒写及文件字节保留 |
| 静态检查 | Ruff check通过；23个文件format --check通过 | 当前产品与测试代码 |
| 构建 | v0.3 wheel和源码包构建通过 | 独立工程产物 |
| 独立安装 | 临时独立环境安装wheel；python -I导入和CLI负例通过 | 从site-packages导入，未通过源码路径运行；缺配置退出1且不回显路径 |
| 制品内容 | wheel只有产品模块及包元数据；两归档未发现私钥PEM材料/运行key文件/数据库/.env | 测试密钥仅动态生成于内存；合成映射和key只在临时测试文件；没有读取真实员工映射或服务key |

第三方依赖未升级；将已有Starlette 1.7.0明确列为直接依赖，CORE仍为0.1.0。离线锁定先定位到本项目既有缓存后完成；未用手工篡改锁文件替代依赖解析。

## 3. 矩阵闭环证据

完整逐项规则和测试定位见[offline-acceptance.md](docs/offline-acceptance.md)。本轮已经执行其开发矩阵：

- A/E001、B/E002各1次；屏障确保并发重叠，结果、请求ID与审计分别归属本人。
- 两个企业使用相同员工号、不同合成管理员key并发；各模拟上游严格核对enterprise_id+employee_no+本企业key，不能依赖姓名判断隔离。
- 未映射、停用、客户端/工具不允许、重复映射拒绝为0请求，并捕获凭据内容读取次数为0。原生认证拒绝、额外工号参数和入口API Key替代身份同样为0。
- 请求工号来自授权映射；上游返回错工号、零条或多条时固定错误且不返回员工数据，每次1请求。401映射upstream_unauthenticated，每次1请求。
- 耐久开始记录后的撤权/改绑为0请求；上游返回后撤权为1请求，拒绝披露并记录upstream_attempted=true。开始写失败0，终态写失败1且不返回成功。
- HTTP/工具超时固定错误，最多1请求，无自动重试。未决记录只供调查，不推断未发送。

审计按request_id关联issuer/subject/client、enterprise_id、员工号、固定工具、阶段、固定错误和调用尝试。开始行的false只表示开始记录时尚未调用；终态丢失时不能据此推断没有上游请求。认证或参数拒绝发生在工具前，不伪造业务审计；入口审计另由对应边界负责。

## 4. 制品与复现

- 包：`sysmcp-biz-t100==0.3.0`。
- Wheel：`dist/sysmcp_biz_t100-0.3.0-py3-none-any.whl`。
- Wheel SHA-256：`720959de5c0fa6f2ef5712961edb4197c96b1b6419ad5e5ae91320683784c00b`。
- uv.lock SHA-256：`b119adc32a0726a3a565cda4a3f8f2ca31ac034833487aa4e1f02ef113ac7aca`。
- 独立安装核验环境：`/tmp/sysmcp-t100-package-mawa26od/venv`；这是本任务临时验证环境，不是运行部署。

```sh
cd /data/projects/sysmcp_dev/projects/biz-t100
UV_CACHE_DIR=/data/projects/sysmcp_dev/.cache/uv uv sync --locked --offline
uv run --locked pytest -q
uv run --locked ruff check .
uv run --locked ruff format --check .
uv build
```

缓存离线命令适用于当前主机已有依赖；其他环境按锁文件安装并同时提供CORE公开wheel。新版QueryJournal端口增加upstream_attempted，外部实现需同步；v1库保持原样，不由v0.3尝试迁移或回滚写入。

## 5. 状态边界

| 状态 | 当前判定 |
|---|---|
| 单元/契约测试 | 已通过 |
| biz-validated | 已通过；限本记录离线代码闭环 |
| business-validated | 未执行真实个人身份、员工号及上游验收 |
| gov-registered / composed / published | 本T100服务未取得对应登记/组合/发布证据 |

网关产品实例已运行与本服务是否发布分别记录。本轮没有监听进程、真实身份登录、T100请求、旧配置/凭据迁移、生产数据读写、GOV对象修改、提交或推送。历史v0.1/v0.2证据保留，不改成当前真实环境通过。
