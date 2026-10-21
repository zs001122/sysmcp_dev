"""Local engineering probe, deliberately separate from business service creation."""

import argparse
from importlib.metadata import version
from typing import Literal

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict

TARGET_PROTOCOL = "2026-07-28"


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: Literal["sysmcp-core"] = "sysmcp-core"
    status: Literal["engineering-probe-only"] = "engineering-probe-only"
    fastmcp_version: str
    business_enabled: Literal[False] = False


def create_probe() -> FastMCP:
    """Only a harmless no-I/O diagnostic tool; never register business tools here."""
    server = FastMCP(name="sysmcp-core-probe", mask_error_details=True)

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    async def core_probe() -> ProbeResult:
        """Check the development skeleton; no identity or business capability is implied."""
        return ProbeResult(fastmcp_version=version("fastmcp"))

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    create_probe().run(transport="http", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
