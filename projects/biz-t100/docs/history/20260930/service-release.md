# BIZ-T100 v0.5.0 服务发布说明

当前版本为 v0.6.1：限定目标的直接真实账号维护已取得重置/禁用各一次 ACK；写链回归 368 项通过。具体边界见 [VALIDATION-v0.6.1](../../../VALIDATION-v0.6.1.md)。GOV 发布状态不提升，以下 v0.6/v0.5 描述保留为历史记录。

当前补充：v0.6.0 交付账号维护离线测试脚本及请求预览，901 项项目测试通过。真实写入关闭，GOV 发布状态不提升；验证和制品记录见 [VALIDATION-v0.6](../../../VALIDATION-v0.6.md)。以下旧版本记录作为历史证据保留。

日期：2026-09-29。当前制品sysmcp-biz-t100==0.5.0；743项自动测试通过，真实正向和空结果参数测试完成。正式个人身份、上游key校验及GOV发布仍未通过，不能据此直接对外发布。

| 项目 | 当前交付 |
|---|---|
| Wheel | dist/sysmcp_biz_t100-0.5.0-py3-none-any.whl |
| 源码包 | dist/sysmcp_biz_t100-0.5.0.tar.gz |
| 摘要清单 | dist/SHA256SUMS-v0.5.0 |
| 工具输入 | 本人查询仍无业务参数；重置独立候选保持原契约 |
| 错误兼容变化 | 空结果employee_not_found、多条employee_not_unique、错工号upstream_employee_mismatch；安全message新增；旧审计不迁移 |
| 开发命令 | live_read.py call --employee-no；每次独立受控策略/审计，本地原生ASGI链路与真实上游 |
| 常驻入口 | 原8768只读服务，版本0.5.0，临时签名身份 |
| 数据口径 | 用户确认旧test数据与生产一致，保留既有test配置标签及http-test传输Profile |

详细[错误目录](../../error-codes.md)、[操作](../../live-read.md)、[版本验证](../../../VALIDATION-v0.5.md)。产品wheel不包含scripts/tests，开发脚本随源码包交付；真实配置、员工数据和密钥只留在私有.runtime，不进入任一制品。部署使用版本固定的产品包，开发CLI使用本项目独立环境；GOV组合还须遵守登记/白名单/private endpoint与两跳规则。

以下为保留的v0.4历史快照及当时证据，不用其中旧版本号、摘要或“尚未装配”说明覆盖本页当前状态。

# BIZ-T100 服务发布描述

日期：2026-09-29。制品 **v0.4.0**。查询与密码重置候选离线代码均已验证；真实业务和GOV发布尚未验收。本文只交付去敏契约，不包含运行配置。

## 服务与工具边界

| 能力/建议逻辑单元 | 工具及输入 | 运行状态 |
|---|---|---|
| T100本人查询 / biz-t100-read | `get_my_t100_employee_record()`，空对象 | 原只读工厂和运行准备已完成；真实输入未齐 |
| T100本人重置 / biz-t100-reset | `reset_my_t100_password(confirm, operation_id)` | 独立写工厂和操作库；候选API仅离线，真实执行未就绪 |

两个逻辑单元尚未取得本项目MetaMCP登记ID。默认独立进程与受控配置/凭据/存储；同项目不表示查询进程自动暴露写能力。OA、代查/代重置、禁用、建号和通用API工具未接入。

T100执行模式为service-account，BIZ独立执行本人许可；不要求员工T100凭据，不授予调用者管理员权限。员工来源为受控issuer+subject映射，重置另需独立t100_account_id，不能默认账号等于员工号。业务、接口、运行维护责任人须在真实准入前落实。

## 制品与安装

| 项目 | 当前值 |
|---|---|
| 包 | sysmcp-biz-t100==0.4.0 |
| Wheel | dist/sysmcp_biz_t100-0.4.0-py3-none-any.whl |
| Wheel SHA-256 | ff969ca532d577fe255d73af54323640a04f56d036cd180b4d8b4b4889bffa19 |
| 初次v0.4构建锁 SHA-256 | 91f61fd95cd82c10a89c3a2a2113bfec3cd1a75442e777d6078754d4edc6867e |
| 底座 | CORE0.1.0公开wheel；Python3.12 / FastMCP4.0.10 |
| 查询运行入口 | create_runtime_app(config, *, auth)，显式AuthProvider；构造不监听 |
| 重置开发入口 | create_reset_server / create_reset_app；auth、identity、reset access、resetter、store显式注入 |
| 重置真实入口 | 未提供；T100PasswordResetClient无注入transport时固定business_adapter_not_ready |
| 回滚与状态 | 无已验收真实运行版本；保持停用，保留原操作库，不删除未知记录或重放 |

构建、隔离安装、打包边界及457项测试见 [VALIDATION-reset.md](../../../VALIDATION-reset.md)。v0.3历史查询运行证据与制品保留。

## 身份、协议与网络

每个部署单元要求受信AuthProvider、独立资源audience、明确issuer/subject/client、令牌用途/有效期/撤销策略。查询scope为 `t100:employee:read:self`，重置scope为 `t100:password:reset:self`，并分别核对默认关闭的业务许可。读取权限、MetaMCP API Key和管理员服务key均不能代替写许可。

原生Streamable HTTP `/mcp`，stateless/json_response。查询兼容Profile `2025-06-18 initialize/list/call`、现代 `2026-07-28` 保留原离线证据；重置ASGI矩阵按 `2025-06-18` list/call，两套原生loopback脚本实测协商 `2025-11-25`。均不是MetaMCP两跳证据，也不声称重置现代能力全量通过。

查询本地健康、单worker限流、HTTPS/http-test及10秒业务预算按 [operations.md](../../operations.md)；这些运行约定不自动成为重置真实装配。重置默认真实网络不可用，所有地址、上游字段和真实接口副作用须单独定标。

## 发布白名单与失败语义

查询与写工具必须分别登记、评审白名单和private endpoint。发布查询不能附带重置，也不因wheel包含新模块自动增加工具。GOV只接收制品、地址引用、协议、允许工具、身份契约和去敏证据；映射、key、授权策略、配置及审计留在BIZ。

- 查询契约及Schema v2见 [tool-contracts.md](../../tool-contracts.md)；终态upstream_attempted仅证明进入Reader，不证明远端收到。
- 写契约及独立操作Schema v1见 [password-reset.md](../../password-reset.md)；同operation ID不重发，未知结果占用员工和账号，禁止自动重试/解锁。
- 写ACK只返回 `upstream_acknowledged`，不代表密码生效、目标读回或密码交付。确认参数只是协议写意图，不证明人工审批。
- 开始记录失败不调用；派发后终态失败不交付成功，保留待核对事实；取消/超时不能当作业务已回滚。

## 真实准入剩余条件

1. 查询：真实身份、两员工本人映射及拒绝对象，固定环境与服务认证、精确唯一记录、权限/撤权/审计验收。
2. 重置：接口必须只重置现存绑定账号，不创建、启用或改角色；确认请求/响应、服务拒绝、结果核对、密码交付/强制更改规则；真实写许可与账号归属独立确认。
3. 运行：专用运行用户、实际地址/TLS、受控配置/key、各自私有数据库、停用与未决调查、首发失败处置。
4. GOV：核对登记资料，限定namespace及private endpoint，直连/两跳结果一致；错误key或工具/服务停用时BIZ业务请求0次。

当前 **biz-validated** 仅代表离线；**business-validated / published** 均未通过。MetaMCP容器已运行不改变这些状态。输入与接入流程见 [integration-inputs.md](../../integration-inputs.md)、[GOV规范](../../../../gov/docs/service-onboarding.md)。

## 内部开发装配补充

当前新增的常驻测试脚本、临时本地签发与模拟上游不进入产品wheel；业务产品代码保持v0.4契约，既有dist制品快照不覆盖。内部测试说明与576项当前结果见 [internal-test.md](../../internal-test.md) 和 [VALIDATION-internal.md](../../../VALIDATION-internal.md)。开发锁已显式声明已有uvicorn/cryptography开发依赖，当前工作区lock SHA-256为 `62a82f023fb29f56b5ae84c525ce2321907ebdcba762b30ed9bb5fabb5f7f662`，与上表初次制品锁分开记录。
