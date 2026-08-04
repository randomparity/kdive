"""Authentication helpers shared by MCP tests."""

from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from tests.mcp.conftest import AUDIENCE, ISSUER


def verifier() -> JWTVerifier:
    """Build a fresh verifier for tests that assemble an MCP application."""
    key_pair = RSAKeyPair.generate()
    return JWTVerifier(
        public_key=key_pair.public_key,
        issuer=ISSUER,
        audience=AUDIENCE,
    )
