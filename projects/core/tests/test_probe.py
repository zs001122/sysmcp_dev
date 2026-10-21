"""Real FastMCP protocol round trips, without external systems or business fixtures."""

from fastmcp import Client

from sysmcp_core.probe import TARGET_PROTOCOL, create_probe


async def test_probe_tool_contract_and_call():
    async with Client(create_probe(), mode=TARGET_PROTOCOL) as client:
        assert client.protocol_version == TARGET_PROTOCOL
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["core_probe"]
        assert tools[0].annotations.read_only_hint is True
        assert tools[0].annotations.open_world_hint is False
        assert tools[0].output_schema is not None
        result = await client.call_tool("core_probe", {})
        assert result.is_error is False
        assert result.structured_content == {
            "component": "sysmcp-core",
            "status": "engineering-probe-only",
            "fastmcp_version": "4.0.10",
            "business_enabled": False,
        }


async def test_unregistered_business_tool_is_not_available():
    async with Client(create_probe(), mode=TARGET_PROTOCOL) as client:
        result = await client.call_tool("purchase_order_list", {}, raise_on_error=False)
        assert result.is_error is True
