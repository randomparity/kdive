"""The typed ``profile`` parameter advertises the ``ProvisioningProfile`` schema (#451, ADR-0124).

Two guarantees: (1) the advertised input schema for ``profile`` is the typed object schema, not the
old freeform ``additionalProperties: true`` blob; (2) the FastMCP 3.4.0 client renders that input
schema and binds a valid profile (the in-tree proof of the client-rendering spike — ADR-0113
flattened recursive *output* schemas for this client, but the ``ProvisioningProfile`` *input* is not
self-recursive and renders cleanly).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair

_TYPED_PROFILE_TOOLS = ("systems.define", "systems.provision", "systems.reprovision")

_VALID_REMOTE_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
    "boot_method": "disk-image",
    "kernel_source_ref": "git:abc123",
    "provider": {"remote-libvirt": {"base_image_volume": "base.qcow2"}},
}


_VALID_LOCAL_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git:abc123",
    "provider": {
        "local-libvirt": {"rootfs": {"kind": "catalog", "provider": "local-libvirt", "name": "f40"}}
    },
}


def _verifier() -> JWTVerifier:
    kp = make_keypair()
    return JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)


def _build() -> FastMCP:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    return build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())


def _profile_schema(tool_params: dict[str, Any]) -> dict[str, Any]:
    return tool_params["properties"]["profile"]


def test_typed_profile_tools_advertise_non_freeform_input_schema() -> None:
    app = _build()

    async def _run() -> None:
        tools = {t.name: t for t in await app.list_tools()}
        for name in _TYPED_PROFILE_TOOLS:
            schema = _profile_schema(tools[name].parameters)
            # The old freeform blob advertised additionalProperties: true; the typed model forbids
            # extra keys and names its fields.
            assert schema.get("additionalProperties") is not True, name
            assert "properties" in schema, name
            assert "schema_version" in schema["properties"], name

    asyncio.run(_run())


def _probe_app_with_typed_profile() -> FastMCP:
    """A minimal app exposing one tool typed exactly like the real ``profile`` param.

    Proves the FastMCP 3.4.0 client renders the real ``ProvisioningProfile`` input schema and binds
    a valid value, without needing a DB pool (the real tool body would touch the pool).
    """
    app: FastMCP = FastMCP(name="probe")

    @app.tool(name="systems.define")
    async def _define(allocation_id: str, profile: ProvisioningProfile) -> dict[str, Any]:
        return {"arch": profile.arch}

    return app


def test_client_renders_input_schema_and_binds_a_valid_profile() -> None:
    app = _probe_app_with_typed_profile()

    async def _run() -> None:
        async with Client(app) as client:
            # list_tools forces the client to parse the advertised input schema; a render failure
            # (the #404/ADR-0113 output-schema problem, if it recurred on input) would surface here.
            names = {t.name for t in await client.list_tools()}
            assert "systems.define" in names
            # A well-formed profile binds at the boundary and reaches the body; the round-trip
            # proves the schema rendered and the value bound (no client-side schema error).
            result = await client.call_tool(
                "systems.define",
                {"allocation_id": "alloc-1", "profile": _VALID_REMOTE_PROFILE},
            )
            assert result.data == {"arch": "x86_64"}

    asyncio.run(_run())


def test_typed_param_round_trip_matches_the_raw_mapping_path() -> None:
    # The registrar binds a typed ProvisioningProfile then passes dump_profile(profile) to the
    # handler; the old path passed the raw mapping straight to parse(). Both paths end up storing
    # dump_profile(parse(...)), so the stored profile and the reprovision dedup digest must be
    # identical. The dump is alias-keyed (provider section is 'local-libvirt', not the Python field
    # name local_libvirt_section) and round-trips through parse() unchanged. A default model_dump()
    # would emit the field names instead of aliases and parse() would reject it.
    from kdive.profiles.provisioning import ProvisioningProfile, dump_profile, profile_digest

    for raw in (_VALID_REMOTE_PROFILE, _VALID_LOCAL_PROFILE):
        # Old path: parse the raw mapping.  Typed path: bind the model, then dump_profile to the
        # mapping the handler receives, then parse that.
        from_raw = ProvisioningProfile.parse(raw)
        typed_mapping = dump_profile(ProvisioningProfile.parse(raw))
        from_typed = ProvisioningProfile.parse(typed_mapping)
        # Provider section is alias-keyed, proving by_alias=True (not the field name).
        provider_section = typed_mapping["provider"]
        assert isinstance(provider_section, dict)
        assert set(provider_section) & {"local-libvirt", "remote-libvirt"}
        assert dump_profile(from_raw) == dump_profile(from_typed)
        assert profile_digest(from_raw) == profile_digest(from_typed)
