"""Use FastMCP directly; do not implement another tool registry or protocol stack."""

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider


def create_service(name: str, *, auth: AuthProvider) -> FastMCP:
    """Build an empty service with explicit authentication and masked internal errors.

    Authentication is a required integration dependency. This factory alone does
    not implement end-user delegation, business authorization or durable audit.
    """
    if not isinstance(auth, AuthProvider):
        raise ValueError("An explicit FastMCP authentication provider is required")
    if not name.strip():
        raise ValueError("Service name must not be empty")
    return FastMCP(name=name, auth=auth, mask_error_details=True)
