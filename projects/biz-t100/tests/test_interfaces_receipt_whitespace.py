"""Success receipt whitespace is empty text; employee identifiers stay exact."""

import httpx
import pytest
from pydantic import SecretStr
from test_interfaces import SECRET, request_for, response_for
from test_interfaces import config as config
from test_interfaces import no_network as no_network

from biz_t100.interface_client import InterfaceUpstreamError, T100InterfaceClient
from biz_t100.interfaces import EmployeeUpdateRequest


async def execute(config, rows, *, request=None, observations=None):
    response = response_for("employee_update")
    response["payload"]["std_data"]["parameter"]["employee"] = rows
    captured = observations if observations is not None else []
    client = T100InterfaceClient(
        config,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)),
        on_write_response=captured.append,
    )
    return await client.execute(
        "employee_update", request or request_for("employee_update"), SecretStr(SECRET)
    )


@pytest.mark.parametrize("description", ["", " ", "\t", "\n", "\r\n", " \t\r\n "])
async def test_whitespace_description_is_success_without_changing_private_receipt(
    config, description
):
    observations = []
    rows = [{"source_no": "E001", "erp_no": "E001", "execute_description": description}]
    result = await execute(config, rows, observations=observations)
    assert result == {
        "employee": [{"source_no": "E001", "erp_no": "E001", "execute_description": ""}],
        "acknowledged": True,
        "business_effect_verified": False,
    }
    assert observations[0]["employee"] == rows


@pytest.mark.parametrize("description", ["Rejected", " \tRejected\r\n ", " 执行失败 "])
async def test_nonblank_description_is_still_rejected(config, description):
    with pytest.raises(InterfaceUpstreamError, match="^upstream_rejected$") as caught:
        await execute(
            config, [{"source_no": "E001", "erp_no": "E001", "execute_description": description}]
        )
    assert caught.value.write_rejected is False


@pytest.mark.parametrize(
    "source,erp",
    [
        (" E001", "E001"),
        ("E001 ", "E001"),
        ("E001", " E001"),
        ("E001", "E001 "),
        ("E002", "E001"),
        ("E001", "E002"),
    ],
)
async def test_whitespace_normalization_never_changes_identifiers(config, source, erp):
    with pytest.raises(InterfaceUpstreamError, match="^upstream_contract_error$") as caught:
        await execute(config, [{"source_no": source, "erp_no": erp, "execute_description": " "}])
    assert caught.value.write_rejected is False


async def test_whitespace_success_mixed_with_failure_is_not_batch_success_or_full_rejection(config):
    request = EmployeeUpdateRequest(
        employee=[
            {"employee_no": "E001", "status": "N"},
            {"employee_no": "E002", "status": "N"},
        ]
    )
    rows = [
        {"source_no": "E001", "erp_no": "E001", "execute_description": " \t "},
        {"source_no": "E002", "erp_no": "", "execute_description": "Synthetic rejected"},
    ]
    with pytest.raises(InterfaceUpstreamError, match="^upstream_rejected$") as caught:
        await execute(config, rows, request=request)
    assert caught.value.write_rejected is False
