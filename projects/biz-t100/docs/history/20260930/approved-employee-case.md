# 获准员工的私有只读模拟用例

> 历史阶段说明。当前统一使用 [四接口与两类测试入口](../../testing.md)，本文旧命令仅作为历史回归背景，不再作为日常运行入口。既有运行资料与审计保留。

当前运行已切换为 [真实T100只读入口](../../live-read.md)。本页模拟/准备记录保留为历史，原模拟监听已停止；8768现由live_read.py连接真实上游。

日期：2026-09-29。用户指定一名真实员工用于当前内部测试。姓名和工号仅保存于私有运行配置，不写入本页、源码、测试夹具或制品。环境标记 `approved-employee-simulated`：**真实员工信息＋临时签名身份＋模拟上游**，不代表已认证该员工或从真实 T100 取得档案。

## 当前入口

在 BIZ-T100 项目目录执行：

```sh
cd /data/projects/sysmcp_dev/projects/biz-t100
.venv/bin/python scripts/employee_case.py status
.venv/bin/python scripts/employee_case.py call
.venv/bin/python scripts/employee_case.py check
```

默认地址 `http://127.0.0.1:8768/mcp`。是否运行以 status 的 ready 为准。call 显示私有配置对应的员工号及展示名，结果同时标注 mock_only 和 real_identity_verified=false。返回的姓名来自用户批准的输入，不是 T100 真实性证明。

原 [A/B 全合成环境](../../internal-test.md) 保留在 8766/8767，新用例不修改其映射、密钥、拒重或审计。

## 初始化与运行

默认目录 `.runtime/approved-read-case` 使用独立的临时签名密钥、模拟服务 key、映射、捕获记录和查询审计。只建 read 目录，不建 reset 目录或账号绑定。父目录须已存在；目录权限 0700、文件 0600，不允许软硬链接。

首次使用由操作者准备私有 JSON 文件，且只包含 `employee_no`、`display_name` 两个字符串字段。它是用户批准的配置输入，不能由 MCP 工具参数提供。使用绝对路径：

```sh
.venv/bin/python scripts/employee_case.py init --input-file /absolute/private/approved-employee.json
.venv/bin/python scripts/employee_case.py serve
```

本次已装配的实例无需重新初始化。init 不覆盖已有用例，输入与已存用例不同会拒绝。后续复验可引用已保存的 `.runtime/approved-read-case/fixture.json`。serve 固定 loopback，默认端口 8768；端口占用可显式指定 `--port`，call/status/check 使用该状态目录的监听记录。

所有命令支持 `--state-dir /absolute/private/case`。换目录必须始终显式携带相同路径。正常停止使用前台 Ctrl-C，或核对 status 中 PID 后向该后台进程发 SIGTERM；重启保留映射、密钥与审计，不删库复位。

## 身份与权限

本地固定模拟 issuer 为 `https://approved-read-case.example.invalid`，audience 为 `approved-t100-read`，client 为 `approved-read-client`。默认主体 `simulated-approved-user` 经 FastMCP 原生公钥验签后，按私有 `issuer+subject` 映射取得工号；姓名仅展示。token 不用工号充当主体，不接受任意 client/scope 或真实上游地址。

只有 `get_my_t100_employee_record` 可用，scope 为 `t100:employee:read:self`。账号为 null、重置许可关闭。工号不自动当作 T100 登录账号；原模拟重置环境仍只处理合成账号。

自动调用临时签发令牌并在使用后删除。给其他本机客户端签发时使用：

```sh
.venv/bin/python scripts/employee_case.py token --output /data/projects/sysmcp_dev/projects/biz-t100/.runtime/approved-read-case/tokens/client-read.jwt
```

目标必须为新的私有文件，不能覆盖，默认有效期 300 秒，范围 1–3600 秒。客户端从受控文件加载 Bearer token；不要将内容放入聊天、命令参数或共享日志。本用例与 A/B 环境的签发密钥和受众隔离，令牌不能互用。

## 验证口径

check 在服务安静且使用 success 场景时执行，增加一条模拟上游请求，核对：

- 请求中的 `INTERNAL-READ + employee_no` 与受控映射、唯一返回记录完全一致。
- 查询开始/成功审计按 request_id 关联到模拟主体、客户端、工具和相同目标。
- 工号参数注入、未映射主体、缺少 token 及调用重置工具都不增加业务请求。

check 返回去敏摘要；单员工用例不能替代原合成 A/B 并发矩阵或两名真实员工验收。服务 key 仍只在业务授权通过后读取。模拟上游使用显式 MockTransport，固定模拟企业与 `.invalid` 地址，捕获实际产品客户端报文，绝不发送真实 T100 请求。错误场景可通过 serve 的 `--scenario mismatch|unauthenticated|timeout` 单独观察，不能作为 success 检查通过。

私有 fixture、映射、签名材料、令牌、捕获及 SQLite 审计都留在 `.runtime`，不进入版本控制、wheel 或 sdist。源码与自动化测试只用合成资料。授权关系需要调整时须按受控配置管理，不得从调用参数、姓名、普通 Header 或 MetaMCP Key 自动改绑。

此用例仅证明指定员工号在模拟身份及模拟上游之间未串线；真实身份、真实档案、真实上游认证、真实重置效果和 MetaMCP 两跳均未据此验收。完整阶段边界见 [身份设计](../../../../../docs/可信个人身份与本人映射设计.md)、[运行输入](../../integration-inputs.md)。
