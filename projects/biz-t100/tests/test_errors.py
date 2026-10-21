"""面向查询与重置失败边界的安全描述文案。"""

import pytest

from biz_t100.errors import ERROR_MESSAGES, error_message
from biz_t100.reset_server import RESET_ERRORS
from biz_t100.server import ERROR_CODES


def test_every_exposed_business_code_has_a_fixed_description():
    boundary_codes = {"invalid_arguments", "tool_not_available", "confirmation_required"}
    for code in ERROR_CODES | RESET_ERRORS | boundary_codes:
        assert code in ERROR_MESSAGES
        assert error_message(code) == ERROR_MESSAGES[code]
    with pytest.raises(TypeError):
        ERROR_MESSAGES["query_failed"] = "synthetic override"


@pytest.mark.parametrize("unknown", ["synthetic-private-upstream-secret", None, {}, []])
def test_unknown_error_message_never_echoes_untrusted_input(unknown):
    assert error_message(unknown) == "操作失败，请联系维护人员并提供请求或操作编号。"
