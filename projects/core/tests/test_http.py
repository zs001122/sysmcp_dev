"""Exercise the actual HTTP/auth boundary, locally through the ASGI app."""

import httpx2
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from sysmcp_core import create_service
from sysmcp_core.probe import create_probe

COMPAT_PROTOCOL = "2025-06-18"
HEADERS = {"Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": COMPAT_PROTOCOL,
        "capabilities": {},
        "clientInfo": {"name": "local-compat-check", "version": "0.1.0"},
    },
}


async def test_http_legacy_initialize_list_and_call():
    app = create_probe().http_app(json_response=True)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post("/mcp", headers=HEADERS, json=INITIALIZE)
            assert response.status_code == 200
            assert response.json()["result"]["protocolVersion"] == COMPAT_PROTOCOL
            headers = {**HEADERS, "MCP-Protocol-Version": COMPAT_PROTOCOL}
            if session := response.headers.get("mcp-session-id"):
                headers["Mcp-Session-Id"] = session
            await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            listed = await client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert listed.status_code == 200
            assert [t["name"] for t in listed.json()["result"]["tools"]] == ["core_probe"]
            called = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "core_probe", "arguments": {}},
                },
            )
            assert called.status_code == 200
            assert called.json()["result"]["structuredContent"]["business_enabled"] is False


async def test_service_requires_correctly_scoped_resource_token():
    key = RSAKeyPair.generate()
    issuer = "https://local-test.invalid"
    verifier = JWTVerifier(public_key=key.public_key, issuer=issuer, audience="test-adapter")
    app = create_service("private-test", auth=verifier).http_app(json_response=True)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            missing = await client.post("/mcp", headers=HEADERS, json=INITIALIZE)
            assert missing.status_code == 401
            wrong = key.create_token(issuer=issuer, audience="gateway-only")
            rejected = await client.post(
                "/mcp", headers={**HEADERS, "Authorization": f"Bearer {wrong}"}, json=INITIALIZE
            )
            assert rejected.status_code == 401
            valid = key.create_token(issuer=issuer, audience="test-adapter")
            accepted = await client.post(
                "/mcp", headers={**HEADERS, "Authorization": f"Bearer {valid}"}, json=INITIALIZE
            )
            assert accepted.status_code == 200
            assert accepted.json()["result"]["protocolVersion"] == COMPAT_PROTOCOL
