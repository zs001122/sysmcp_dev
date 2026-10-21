# 内部连接令牌续签契约

适用：MetaMCP 2.4.22 的现有 `sysmcp-t100` 登记，以及 BIZ-T100 0.9.2 的公开 `issue-gateway-token` CLI。此工具仅维护内部连接，不是统一身份平台，不代表外部员工身份，也不执行 T100 业务请求。

## 职责、输入与状态

| 归属 | 输入 | 动作与输出 |
|---|---|---|
| BIZ-T100 | 自有服务配置、已批准连接主体、签名私钥 | 通过固定制品 CLI 重新检查主体许可，签发最长 3600 秒 JWT，原子发布到 `.runtime/exports/metamcp-connector.jwt` |
| GOV | 明确发布的 JWT、GOV 自有管理登录配置、原服务标识及预期连接字段 | 调用 MetaMCP 受支持管理 API，仅更新原服务的 bearerToken，再读取核对；保存无秘密状态 |
| systemd | BIZ 签发单元、GOV 同步单元及一个定时器 | 启动约一分钟后首次触发，随后每 20 分钟依次触发签发与同步；签发失败则不运行同步，失败不调用任何业务工具 |

JWT 导出文件是双方明确约定的公开交接接口，内容仍是受控凭据，文件 0600、目录 0700。GOV 不读取 BIZ 配置、员工／账号映射、许可、签名私钥、上游 key 或业务审计。两个模块不互相导入源码，也不直接操作 MetaMCP 数据库。

GOV 配置 schema_version=1，模板见 [connector-sync.example.json](../config/connector-sync.example.json)。配置指定管理地址、Origin、原服务 UUID／名称／类型／URL、发布令牌路径、管理登录文件、私有状态路径及预期连接 issuer／audience／subject／client／scope。连接元数据不包含员工目标或业务规则。最低依赖为项目自己的 Python 3.12 环境与标准库，依赖及解释器范围由 pyproject.toml／uv.lock 管理。

CLI 为 `ops/sync_connector.py --config <绝对私有路径>`；成功退出 0，失败退出非 0，只输出固定状态及错误分类。管理地址不走环境代理、不跟随重定向，网络调用有超时及响应大小上限；配置和秘密文件必须属于运行用户、权限受控且不能使用符号链接。状态原子写入 GOV 私有目录，不记录令牌、Cookie、密码或远端自由文本。

## 同步顺序与失败边界

1. 校验配置、文件边界、令牌结构、预期连接字段和有效期。GOV 的结构检查不是验签；信任来自 BIZ 受控发布文件，下游认证仍由 FastMCP 原生验签完成。
2. 使用 GOV 管理凭据登录固定 MetaMCP，读取原登记，精确核对 UUID、名称、类型和 URL。任何不符都拒绝，不创建替代服务或修改 namespace／endpoint。
3. 当前登记已保存同一令牌时不重复写入；否则仅更新 bearerToken，保留其它更新契约字段。
4. 再次读取，核对令牌和非令牌字段一致，才记录本轮同步成功。写响应丢失时只读取核对，不在同一轮盲目重复 POST；无法确认就标失败或结果未确认。
5. 本轮失败保留失败阶段、上次成功时点及旧令牌到期事实；不能以本地签发成功、定时器 active 或工具列表缓存代替同步成功。下一次定时触发仅重复幂等的连接配置同步，绝不重试业务写入。

BIZ 的签发 unit 不设置 RemainAfterExit；GOV unit 明确 Requires／After 签发 unit。单一定时器使用 OnActiveSec=1min、固定日历调度和 Persistent，避免依赖失败后丢失下一次调度。签发单元失败时 GOV 不运行，原状态文件不会刷新，必须结合两个 unit 的 Result 与最近成功时点判断。登记回读一致也不等于每轮已做网关端到端业务验收。

人工修改原服务登记前，先停止 timer，并等待已启动的签发／同步任务结束；完成编辑后再启动 timer。MetaMCP 当前更新接口没有 ETag 或版本条件，文件锁只协调本工具，不能阻止外部管理员同时修改登记。停用自动续签时停止并禁用 GOV timer；已发布令牌自然到期。立即撤销连接访问仍通过 BIZ 的主体许可或 GOV 服务停用机制，不把停用 timer 当即时撤权。

## 验证与兼容

独立测试使用合成令牌和捕获管理报文的替身，覆盖错误对象零更新、过期或扩权令牌拒绝、登录／更新／回读失败、不重复更新、字段保持、锁冲突、状态落盘及日志不泄密。现场先完成一次真实签发／同步，读取实际登记核对，再经过网关验证两个工具范围以及实际下游的缺确认拒绝；T100 请求数必须为 0。

定时配置和 Linger 生效证明已具备用户服务常驻条件；不重启宿主时不能宣称实际开机演练完成。持续续签应记录独立轮次的不同令牌到期时间及同步成功，不能只凭一次运行通过签收长期运行。

MetaMCP 2.4.22 更新服务后异步刷新闲置连接及后续新会话使用的参数；已有活跃 MCP 会话继续使用创建时的 Authorization，不能无缝换成新 JWT。管理 API 回读成功也不代表异步连接刷新已经完成，没有固定等待时长保证。本轮的自动续签范围是**登记更新与后续新建会话**，不签收既有长会话跨令牌到期的连续可用性。

当前 live.py 每次调用独立创建并结束 MCP 会话，适用上述范围。持久客户端应在业务请求完成后，使用客户端 SDK 结束当前会话，再初始化新会话；MetaMCP 支持带原 Mcp-Session-Id 的标准 HTTP DELETE 关闭该会话。不能在写请求进行中强制断连，不能因认证失效自动重发业务操作。固定版本没有单服务活跃连接热更新管理 API；全局 session lifetime 设置不作为本服务的续签修复，本轮不调整它或重启网关。

此限制依据固定源码 commit `32b6e9813acfc13d1013eb2452be1899d6654bb9` 的 `mcp-servers.impl.ts` 更新后的异步失效处理、`mcp-server-pool.ts` 的 activeSessions 命中及 invalidateIdleSession、`client.ts` 的 transport 构造和 `streamable-http.ts` 的 DELETE 处理核对。实际持久客户端的会话结束／新建策略需在其接入阶段单独验证。

此工具依赖固定 MetaMCP 版本的管理 API schema；产品升级时重新核对登录、get／update 以及会话失效行为。BIZ 升级不隐式切换签发制品路径，先校验新制品和导出契约，再修改其自有 unit。日常业务仍只使用 BIZ 的 offline.py／live.py，本工具属于 GOV 运维入口。

## 维护检查

当前安装使用 GOV 自身 `.venv` 解释器及 `.runtime/releases/connector-sync-0.1.0/sync_connector.py` 固定副本；模板位于本项目 deploy 目录，BIZ 签发模板归 BIZ 自身。运行配置按模板在私有目录维护，不提交真实凭据。

```bash
# 查看调度、最近执行结果；不要打印令牌或管理登录文件
systemctl --user list-timers sysmcp-t100-gateway-sync.timer --no-pager
systemctl --user show sysmcp-t100-gateway-token.service sysmcp-t100-gateway-sync.service \
  -p Result -p ExecMainStatus -p ExecMainStartTimestamp
```

计划停机时先停止 timer，等待两个在途维护单元结束，再停止 BIZ 服务；否则后续定时签发会通过 Requires 重新启动 BIZ。停用自动续签使用 `systemctl --user disable --now sysmcp-t100-gateway-sync.timer`；恢复使用 `systemctl --user enable --now sysmcp-t100-gateway-sync.timer`。这些是连接维护命令，不调用 T100。

GOV 独立离线验证在 `/data/projects/sysmcp_dev/projects/gov` 执行：

```bash
uv sync --locked
uv run --no-sync python -m unittest discover -s tests -v
```
