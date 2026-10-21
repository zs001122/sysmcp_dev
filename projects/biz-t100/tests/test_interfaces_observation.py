"""Bounded private write observations; synthetic upstream and credentials only."""

import json

import httpx
import pytest
from pydantic import SecretStr
from test_interfaces import SECRET, request_for, response_for
from test_interfaces import config as config
from test_interfaces import no_network as no_network

from biz_t100.interface_client import InterfaceUpstreamError, T100InterfaceClient


def client_for(config, response, observer, calls):
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    return T100InterfaceClient(
        config,
        transport=httpx.MockTransport(upstream),
        on_write_response=observer,
    )


@pytest.mark.parametrize("action", ["employee_create", "employee_update", "account_maintain"])
async def test_successful_writes_observe_once_without_request_or_unselected_fields(config, action):
    calls, observations = [], []
    response = response_for(action)
    std = response["payload"]["std_data"]
    std["execution"].update(sql_code=0, description="Synthetic success", extra="excluded")
    response["key"] = SECRET
    std["parameter"] = {
        "employee": [
            {
                "source_no": "E001",
                "erp_no": "E001",
                "execute_description": "",
                "full_name": "Do not capture",
                "email": "private@example.invalid",
                "key": SECRET,
            }
        ],
        "master": [{"key": SECRET}],
        "host": {"key": SECRET},
    }
    result = await client_for(config, response, observations.append, calls).execute(
        action, request_for(action), SecretStr(SECRET)
    )
    assert result["acknowledged"] is True
    assert len(calls) == len(observations) == 1
    assert observations == [
        {
            "execution": {"code": "0", "sql_code": "0", "description": "Synthetic success"},
            "employee": [{"source_no": "E001", "erp_no": "E001", "execute_description": ""}],
        }
    ]
    assert SECRET not in json.dumps(observations)
    assert "Synthetic success" not in json.dumps(result)


async def test_query_does_not_invoke_observer_or_capture_personal_fields(config):
    calls = []

    def forbidden(_):
        pytest.fail("Query responses must never reach the write observer")

    result = await client_for(config, response_for("employee_query"), forbidden, calls).execute(
        "employee_query", request_for("employee_query"), SecretStr(SECRET)
    )
    assert len(result["employee"]) == len(calls) == 1


@pytest.mark.parametrize("code", ["synthetic-rejection", "0"])
async def test_execution_or_row_rejection_is_saved_before_interpretation(config, code):
    calls, observations = [], []
    response = response_for("employee_update", code)
    std = response["payload"]["std_data"]
    std["execution"]["description"] = "Private execution detail"
    std["parameter"]["employee"][0].update(erp_no=" ", execute_description="Private row rejection")
    with pytest.raises(InterfaceUpstreamError, match="^upstream_rejected$") as caught:
        await client_for(config, response, observations.append, calls).execute(
            "employee_update", request_for("employee_update"), SecretStr(SECRET)
        )
    assert len(calls) == len(observations) == 1
    assert observations[0]["employee"][0]["execute_description"] == "Private row rejection"
    assert "Private" not in json.dumps(caught.value.diagnostics)
    assert caught.value.diagnostics["execution_code"] == code


async def test_observer_receives_bounded_rows_and_strings_only(config):
    calls, observations = [], []
    response = response_for("employee_update", "synthetic-rejection")
    std = response["payload"]["std_data"]
    std["execution"].update(description="X" * 3000, sql_code={"key": SECRET})
    std["parameter"]["employee"] = [
        {
            "source_no": "S" * 3000,
            "erp_no": "E" * 3000,
            "execute_description": "D" * 3000,
            "email": "private@example.invalid",
        }
        for _ in range(105)
    ]
    with pytest.raises(InterfaceUpstreamError, match="^upstream_rejected$"):
        await client_for(config, response, observations.append, calls).execute(
            "employee_update", request_for("employee_update"), SecretStr(SECRET)
        )
    saved = observations[0]
    assert len(calls) == 1
    assert len(saved["execution"]["description"]) == 2048
    assert "sql_code" not in saved["execution"]
    assert len(saved["employee"]) == 100
    assert all(len(value) == 2048 for row in saved["employee"] for value in row.values())
    assert all(
        set(row) == {"source_no", "erp_no", "execute_description"} for row in saved["employee"]
    )


@pytest.mark.parametrize("secret", [SECRET, "redacted"])
async def test_credential_substrings_are_redacted_before_truncation(config, secret):
    calls, observations = [], []
    sensitive = "X" * 2040 + secret + "Y" * 30
    response = response_for("employee_update", sensitive)
    std = response["payload"]["std_data"]
    std["execution"].update(sql_code=sensitive, description=sensitive)
    std["parameter"]["employee"] = [
        {name: sensitive for name in ("source_no", "erp_no", "execute_description")}
    ]
    with pytest.raises(InterfaceUpstreamError, match="^upstream_rejected$"):
        await client_for(config, response, observations.append, calls).execute(
            "employee_update", request_for("employee_update"), SecretStr(secret)
        )
    assert len(calls) == len(observations) == 1
    assert secret not in json.dumps(observations)
    assert all(value in {"[redacted]", ""} for value in observations[0]["execution"].values())
    assert all(value in {"[redacted]", ""} for value in observations[0]["employee"][0].values())


@pytest.mark.parametrize("error", [OSError, ValueError, TimeoutError])
async def test_observer_failure_is_journal_error_with_no_retry_or_private_text(config, error):
    calls, observations = [], []

    def broken(observation):
        observations.append(observation)
        raise error("Synthetic private path or upstream detail")

    with pytest.raises(InterfaceUpstreamError, match="^journal_unavailable$") as caught:
        await client_for(config, response_for("employee_update"), broken, calls).execute(
            "employee_update", request_for("employee_update"), SecretStr(SECRET)
        )
    assert len(calls) == len(observations) == 1
    assert "Synthetic private" not in str(caught.value)
    assert caught.value.diagnostics == {"http_status": 200, "execution_code": "0"}


async def test_observer_cannot_mutate_upstream_rejection_into_success(config):
    calls = []

    def mutate(observation):
        observation["execution"]["code"] = "0"
        observation["employee"] = []

    with pytest.raises(InterfaceUpstreamError, match="^upstream_rejected$"):
        await client_for(
            config, response_for("employee_update", "rejected"), mutate, calls
        ).execute("employee_update", request_for("employee_update"), SecretStr(SECRET))
    assert len(calls) == 1
