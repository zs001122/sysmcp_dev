"""固定且安全的错误描述；绝不拼接身份信息或上游内容。"""

from types import MappingProxyType

EMPLOYEE_RESULT_ERRORS = frozenset(
    {"employee_not_found", "employee_not_unique", "upstream_employee_mismatch"}
)

ERROR_MESSAGES = MappingProxyType(
    {
        "unauthenticated": "身份认证失败，令牌缺失、无效或不符合当前服务要求。",
        "identity_not_enabled": "当前身份未建立有效映射或已停用。",
        "account_not_linked": "当前身份尚未绑定执行此操作所需的账号。",
        "permission_denied": "当前身份、客户端或提交请求不符合工具许可。",
        "policy_unavailable": "授权映射或策略不可用，请检查受控配置。",
        "credential_unavailable": "上游管理员服务凭据不可用。",
        "authorization_changed": "调用期间授权或员工绑定已变化，本次结果不予返回。",
        "business_adapter_not_ready": "业务接口尚未满足启用条件。",
        "upstream_unauthenticated": "上游拒绝了服务认证，请检查管理员服务凭据。",
        "business_forbidden": "上游拒绝执行此操作。",
        "upstream_unavailable": "上游连接失败、限流或服务不可用。",
        "upstream_timeout": "上游调用超时，未自动重试。",
        "upstream_rejected": "上游返回失败状态或非成功业务码。",
        "upstream_contract_error": "上游响应格式、字段类型或大小不符合接口约定。",
        "employee_not_found": "上游接口执行成功，但本次查询未返回员工记录。",
        "employee_not_unique": "上游返回多条员工记录，无法确认唯一目标，未返回员工数据。",
        "upstream_employee_mismatch": "上游返回的工号与授权工号不完全一致，未返回员工数据。",
        "journal_unavailable": "审计或操作记录不可用，本次操作不能确认成功。",
        "query_timeout": "查询总时限已到，未返回员工数据，也未自动重试。",
        "query_cancelled": "查询已取消，结果未交付；请按请求编号核对审计。",
        "query_failed": "查询失败，请按请求编号检查服务与审计记录。",
        "invalid_arguments": "工具参数不符合接口约定。",
        "empty_identifier": "员工工号或登录账号为空。",
        "invalid_request": "业务请求不符合所选接口的参数契约。",
        "enterprise_mismatch": "请求企业与受控运行配置不一致。",
        "upstream_verification_failed": "接口回执成功，但回读资料与提交目标不一致。",
        "operation_failed": "业务操作失败，请通过请求编号检查审计。",
        "write_permission_not_configured": "尚未配置本次动作和目标的真实写许可。",
        "mcp_call_failed": "FastMCP 调用未返回有效结果。",
        "protocol_mismatch": "协商协议不符合本阶段验收基线。",
        "tool_not_available": "此服务未开放所请求的工具。",
        "confirmation_required": "真实写操作必须明确确认后才能执行。",
        "reset_already_recorded": "此操作编号已有记录，不会重复执行。",
        "reset_pending_review": "此员工或账号存在待核对的写操作，暂不允许继续写入。",
        "reset_outcome_unknown": "重置结果尚未确认，目标保持占用；请核对原操作，勿自动重试。",
        "reset_timeout": "重置处理超时，未自动重试；请按操作编号核对记录。",
        "reset_cancelled": "重置处理已取消；请按操作编号核对是否已调用上游。",
        "reset_failed": "重置失败，请按操作编号检查服务与操作记录。",
        "runtime_configuration_invalid": "服务运行配置无效，请检查受控配置。",
        "service_unavailable": "服务尚未就绪或当前处理容量已满。",
    }
)


def error_message(code: str) -> str:
    """只返回白名单内的消息；对畸形或未知的错误码同样返回固定文案。"""
    if type(code) is str and code in ERROR_MESSAGES:
        return ERROR_MESSAGES[code]
    return "操作失败，请联系维护人员并提供请求或操作编号。"
