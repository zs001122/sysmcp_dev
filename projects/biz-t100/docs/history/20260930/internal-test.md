# BIZ-T100 常驻内部测试服务

> 历史阶段说明。当前统一使用 [四接口与两类测试入口](../../testing.md)，本文旧命令仅作为历史回归背景，不再作为日常运行入口。既有运行资料与审计保留。

当前运行已切换为 [真实T100只读入口](../../live-read.md)。本页模拟/准备记录保留为历史，原模拟监听已停止；8768现由live_read.py连接真实上游。

日期：2026-09-29。用途：本机开发与手工调用。环境标记为 internal-simulated，身份、员工、账号、服务 key 和业务上游全部为合成或模拟。当前是否运行以 status 的现场结果为准；默认端口不是已部署证明，启动与验证结论见 [STATUS](../../../../../STATUS.md) 的本轮记录。

本次与一次性 [查询自检](../../direct-check.md)、[重置自检](../../password-reset.md) 的区别是服务会保持监听，供用户后续连接。决策见 [ADR-007](../../../../../docs/架构决策-007-T100内部测试服务.md)。它不是生产部署，不联系真实 IdP/T100，不登记 MetaMCP。

用户指定真实员工信息的单独只读模拟用例见 [私有员工用例](../../approved-employee-case.md)。它使用独立运行目录与8768入口，不替换本页的固定A/B合成资料。

## 1. 地址与初始化

| 服务 | 默认本机地址 | 唯一工具 |
|---|---|---|
| read | http://127.0.0.1:8766/mcp | get_my_t100_employee_record |
| reset | http://127.0.0.1:8767/mcp | reset_my_t100_password |

两个服务分别使用产品工厂、独立端口、单 worker，绑定地址固定为 127.0.0.1。只有同一主机可连接；“常驻”不代表已开放给内网其他机器或由系统服务管理器托管。

以下命令均使用本项目独立环境，并在项目目录执行：

```sh
cd /data/projects/sysmcp_dev/projects/biz-t100
.venv/bin/python scripts/internal_test.py init
.venv/bin/python scripts/internal_test.py status
```

默认状态目录为 `/data/projects/sysmcp_dev/projects/biz-t100/.runtime/internal-test`。init 首次创建私有合成资料；再次执行验证并复用现有资料，不覆盖私钥、策略、key 或审计。部分初始化、权限错误或不匹配资料应报错，不通过覆盖旧资料“修好”。如果 status 已显示两个服务 ready，直接进行调用，无需重复启动。

全部子命令均支持 `--state-dir /绝对/私有/路径`。使用其他目录时，每个命令都须指向同一个目录；它不是任意业务配置目录，不能放入真实身份、员工或凭据。

## 2. 在两个终端保持服务运行

终端一：

```sh
.venv/bin/python scripts/internal_test.py serve --service read
```

终端二：

```sh
.venv/bin/python scripts/internal_test.py serve --service reset
```

两个终端均须位于项目目录。serve 输出去敏 ready 摘要、对应 MCP URL 和 PID 后继续运行。端口占用时，显式选用 1024–65535 范围的空闲端口，例如 `serve --service read --port 18766`；同一状态目录下同一服务只能运行一个实例。call/status/check 读取本地监听记录，无需再次传端口。

主任务也可以按本次用户授权启动并保留两个后台测试进程；实际 PID 以本轮启动记录及 status 为准，不在文档中固定一个可能过期的 PID。启动失败不能仅凭监听记录或进程存在认定服务可用。

## 3. 日常调用与直接检查

在第三个终端、同一项目目录执行：

```sh
.venv/bin/python scripts/internal_test.py call --service read --user user-a
.venv/bin/python scripts/internal_test.py call --service read --user user-b
.venv/bin/python scripts/internal_test.py call --service reset --user user-a --confirm
.venv/bin/python scripts/internal_test.py status
.venv/bin/python scripts/internal_test.py check
```

call 默认使用 user-a；不传 `--token-file` 时，在本地临时签发正确资源的 token，调用后删除临时 token 文件。reset 必须显式 `--confirm`，未提供时由工具拒绝；未提供 operation-id 时自动生成本次 UUID。read 不允许 confirm 或 operation-id。成功查询可以显示合成档案，重置只返回 operation_id 与 upstream_acknowledged，不返回密码。

status 分别返回 read/reset 的 ready、stopped、unreachable 或 not_ready，ready 时包含本机 URL 和 PID。status 不汇总审计或请求计数，退出码 0 也不意味着两服务均 ready，必须看各服务状态。

check 要求两个服务均 ready 且使用默认 success 场景。它调用正在运行的服务，对照 A/B 并发、工具隔离、重置拒重/确认、参数注入、停用/未映射和跨资源 token 拒绝，再核对捕获报文与 SQLite 审计。成功摘要包含本次新增模拟上游请求数、成功审计调用数、实际协议版本及 services_left_running=true；执行后保留服务运行。为保持计数可比较，运行 check 时不要同时手工调用。它会新增合成业务请求和持久测试记录，不是只读健康探针。

命令成功通常退出 0，调用或检查失败退出 1；参数用法错误由命令解析器拒绝。摘要与错误不打印 token、私钥或服务 key。本机原生协议结果不代表 MetaMCP 两跳或其兼容 Profile 通过。

### 3.1 单独签发 token 给其他本机客户端

```sh
.venv/bin/python scripts/internal_test.py token --service read --user user-a --ttl 300 --output /data/projects/sysmcp_dev/projects/biz-t100/.runtime/internal-test/tokens/user-a-read.jwt
.venv/bin/python scripts/internal_test.py call --service read --token-file /data/projects/sysmcp_dev/projects/biz-t100/.runtime/internal-test/tokens/user-a-read.jwt
```

`--output` 必须是绝对的新文件路径，不能覆盖已有文件；重复签发时换一个新文件名。默认有效期 300 秒，允许 1–3600 秒。输出父目录须已存在且私有；默认状态目录内仅允许写入 tokens 子目录。token 内容只写入 0600 文件，命令仅输出路径和去敏签发摘要，不在聊天粘贴 token。`--user` 与 `--token-file` 互斥；使用 token-file 时由该 token 决定身份。

其他本机 MCP 客户端使用相应 `/mcp` 地址，并通过其安全配置加载该文件的 token 作为 Bearer 凭据；不要把 token 放入命令行、共享配置或日志。read token 不适用于 reset，须另用 `token --service reset` 签发正确资源的令牌。这里没有 HTTP 登录或远程签发接口。

### 3.2 手工观察前置拒绝与重放

```sh
.venv/bin/python scripts/internal_test.py call --service read --user disabled
.venv/bin/python scripts/internal_test.py call --service reset --user unmapped --confirm
.venv/bin/python scripts/internal_test.py call --service reset --user user-a
.venv/bin/python scripts/internal_test.py call --service reset --user user-b --confirm --operation-id 52a15e95-88b0-4fef-a960-2e4962d89567
.venv/bin/python scripts/internal_test.py call --service reset --user user-b --confirm --operation-id 52a15e95-88b0-4fef-a960-2e4962d89567
```

前三项应拒绝且不增加上游调用；最后两项示范同一规范 UUID 首次模拟 ACK、再次 reset_already_recorded。若这个示例 UUID 已留有记录，首次也会拒重，这是正常行为；需要新操作时选择新的 UUID，不删除历史审计。

## 4. 场景与故障测试

`serve --scenario` 选择启动时的模拟上游行为，默认 success；场景不是工具参数，不能由调用者改变。

| 场景 | 可用服务 | 预期观察 |
|---|---|---|
| success | read / reset | 合成档案或严格 ACK |
| unauthenticated | read / reset | 模拟 HTTP 401；查询拒绝，重置记录明确拒绝，不自动重试 |
| timeout | read / reset | 模拟请求超时；重置保留 unknown 和双目标占用 |
| mismatch | 仅 read | 返回另一合成员工；产品查询按契约拒绝，不交付错误档案 |

改场景需要先正常停止该状态目录下的对应服务，再用新 scenario 启动。不要在用于日常 success 检查的重置库上随意制造 unknown：恢复 success 不会自动解锁，新的 operation-id 仍可能被目标占用拒绝。需要独立故障实验时，用另一个全新私有状态目录初始化并选不同空闲端口，后续命令带相同 state-dir；这仅用于合成测试，不是绕过真实业务未知状态的恢复办法。

check 是 success 主线检查，不应拿故障场景或已有未知目标锁下的预期拒绝解释为正常主线通过。完整故障矩阵仍以产品 [离线验收](../../offline-acceptance.md)、[重置契约](../../password-reset.md) 及对应测试为准。

## 5. 身份、文件与审计归属

| 主体 | 合成员工 | 合成账号 | 用途 |
|---|---|---|---|
| user-a | E001 | LOGIN-A | 本人查询与模拟重置 |
| user-b | E002 | LOGIN-B | 第二用户隔离及并发验证 |
| disabled | 无可用许可 | 无可用许可 | 已停用前置拒绝 |
| unmapped | 无映射 | 无映射 | 未映射前置拒绝 |

这些是固定测试夹具，不对应真实员工。本人目标仍来自服务侧受控 issuer+subject 映射，参数、普通 Header、姓名或未选定 claim 不能覆盖。

固定模拟 issuer 为 `https://internal-lab.example.invalid`。read 的 audience/client 为 internal-t100-read/internal-read-client，scope 为 t100:employee:read:self；reset 为 internal-t100-reset/internal-reset-client，scope 为 t100:password:reset:self。两服务还使用各自合成企业、服务 key、策略及数据库。

运行目录及子目录权限 0700，文件 0600，拒绝软硬链接和不匹配状态。主要文件如下，不应将其加入版本提交或制品：

```text
.runtime/internal-test/
  state.json
  issuer-private.pem
  issuer-public.pem
  tokens/
  read/
    access.json
    service.key
    listener.json
    listener.lock
    upstream.jsonl
    audit/query.sqlite3
  reset/
    access.json
    service.key
    listener.json
    listener.lock
    upstream.jsonl
    audit/reset.sqlite3
```

私钥仅在初始化时生成、签发时读取；验证端使用公钥，通过原生 AuthProvider 校验签名、issuer、audience、subject、客户端、有效期及 scope。运行加载可以核查私钥文件元数据，不读取其内容来验签。没有通用 IAM 或共享 API Key 身份替代路径。

上游明确使用 MockTransport，接收产品客户端实际构造的 HTTP 报文，核对企业、员工/账号和服务 key 摘要，不访问 `.invalid` 地址或任何真实接口。upstream.jsonl 只记录计数、时间、场景与固定合成目标，不保存原始请求/响应、key 或 token。产品 SQLite 记录独立保存可信主体、目标和调用终态，不能用模拟捕获文件代替耐久业务审计；库由服务 lifespan 准备，init 不清库。

## 6. 停止、重启与保证边界

前台运行的每个 serve 进程分别按 Ctrl-C 停止；后台实例按本轮记录并核实的对应 PID 正常终止。不要按模糊进程名批量停止其他服务。停止后再次 status，确认对应实例不再 ready；进程退出不删除运行目录。

重新启动沿用原密钥、策略、SQLite 和拒重记录。init 不是轮换密钥、复位账号或清空审计的命令；未决操作只用于调查，不自动重放/解锁，不通过删库消除。只有已核实的合成开发状态才适合另建独立测试目录，不能推广到真实运行恢复。

[身份设计](../../../../../docs/可信个人身份与本人映射设计.md) 禁止模拟身份执行真实写入。本服务的身份、账号、key 和上游全部为合成或模拟，重置只得到模拟 ACK 并更新本地测试记录，不修改任何真实账号，因此不放宽该限制。

本次脚本复用产品的本人授权、显式确认、耐久拒重和未知双目标锁，产品源码不反向导入 scripts/tests，真实重置默认传输仍未开放。不提供真实 URL、外部身份或 `--auth-public-key` 切换参数。未来外部实现就绪后，通过既有 auth/access/reader/resetter 边界另行装配并验收，不需要重写业务身份授权用例。

当前结论最多为 internal-simulated 及有对应证据的 biz-validated，不是实际密码生效、真实个人身份、business-validated 或 GOV published。真实只读测试与真实重置各自仍遵循其已确认范围和验收要求。
