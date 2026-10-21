# BIZ-T100 v0.6 验证记录

日期：2026-09-30。本轮交付密码重置、账号禁用的离线测试脚本与受控请求预览。没有执行真实写入，没有修改 AGENTS.md 或开启私有真实写入开关。正式个人身份、真实写效果与 GOV 发布均未验收。

## 实际执行结果

| 检查 | 实际结果 | 范围 |
|---|---|---|
| `bash scripts/test_account_write.sh` | **325 passed，23.93s** | 新账号维护客户端/原生MCP、共享操作库、命令链路及原候选重置回归 |
| `.venv/bin/pytest -q` | **901 passed，64.41s** | 项目全量回归 |
| `.venv/bin/ruff check src scripts tests` | 通过 | 当前源码、脚本与测试 |
| `.venv/bin/ruff format --check src scripts tests` | 通过，47 files already formatted | Ruff 实际纳入检查的文件 |
| `bash -n scripts/test_account_write.sh` | 通过 | Shell 语法 |
| `live_write.py --help` / `plan --help` | 通过 | 命令入口、必填工号和账号、显式状态选项 |

测试使用动态合成密钥、合成员工/账号、原生 JWT/FastMCP 与捕获 HTTP 请求的模拟上游，未访问真实 T100。新命令测试显式拦截默认网络传输；其中 44 项覆盖本机计划/执行链，包含默认关闭与显式关闭的双动作拒绝场景：0 次 key 内容读取、0 次上游请求。

## 契约与行为

- 密码重置：固定 `password_reset=Y`，必须显式指定账号状态 Y/N；没有默认启用。
- 账号禁用：固定 `gzxastus=N`、`password_reset=N`；修改登录账号，不修改员工主档。
- 两者：`gzxa001` 登录账号和 `gzxa003` 员工号分别绑定；`is_reproduce=N`，省略 detail。不发送明文密码、角色或站点修改。
- 工具只接收 confirm、operation_id；工号/账号/状态是本地受控计划输入，不能由远程工具参数或签名中未选定的员工 claim 覆盖。
- 原 reset 工厂和三字段离线候选保留默认行为；新维护工厂按动作暴露独立工具/scope。
- 同企业新两动作共享 SYA1 操作库，双目标占用跨动作生效；重复 UUID、未知结果及改绑不能重复派发。新库不混用旧重置/查询库。
- 首次并发初始化通过短时私有文件锁串行建库，不持锁等待业务上游；两个员工可并发，员工与账号不串。
- 派发后 401/403、超时或授权变化均保留未知状态；ACK 仅记 upstream_acknowledged，不是密码或停用效果证明。
- `upstream_attempted` 表示本次调用；`recorded_possible_dispatch` 表示历史记录，重复提交不把历史派发误报成本次调用。

## 运行边界

用户此前确认了指定测试员工与账号绑定、省略 detail 保留角色/站点。新上传的四份 DOCX 已保存至 `.runtime/reference-docs/2026-09-29/`，摘要及字段差异见 [接口索引](docs/upstream-interfaces.md) 和 [RU-004](../../docs/reuse/RU-004-T100账号禁用与受控写入.md)。没有扩展接入员工新建或员工更新。

用户 2026-09-30 重发 AGENTS.md，保留“临时身份不得真实写入”的限制。因此本轮只交付离线验证与请求预览，`Contract.internal_test_write_enabled` 默认 false；未写入真实执行例外，未修改真实运行配置。未来真实执行需按届时明确生效的准入约定开展，不能把测试夹具开关当作运行授权。

## 制品与状态

版本及 uv.lock 已更新到 0.6.0。构建文件为 `dist/sysmcp_biz_t100-0.6.0-py3-none-any.whl` 和 `dist/sysmcp_biz_t100-0.6.0.tar.gz`，摘要另存 `dist/SHA256SUMS-v0.6.0`。制品检查排除私有运行目录、原始 DOCX、测试签名私钥、真实员工标识；wheel 只含产品包，不含 scripts/tests。

本轮代码/契约证据为 **biz-validated**。不提升 **business-validated** 或 **published**。v0.5 的真实只读事实继续保留在原记录，不在本轮重复请求业务系统或据此推断写入成功。旧制品保留，未提交或推送。
