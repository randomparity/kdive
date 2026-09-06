"""Operator-only network/AF_UNIX health proof for ADR-0606; no provider operations.

Run ``python -m tests.live_vm.authority_network_proof /protected/proof.json``.
The mode-0600 JSON names the authority instance, configured and denied IPv4 address/port,
optional SSH target, secrets directory and credential file. The directory contains server-ca,
client-certificate, client-key, untrusted-certificate and untrusted-key. The SSH helper
uses only the deployed authority's fixed paths. Each exchange has a ten-second budget;
SSH has a fifteen-second process deadline. Omit the SSH target when running inside an
operator-created client namespace on the authority host. A failed arm makes the process exit 1.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import stat
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from ipaddress import IPv4Address
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.authority_sender import authority_sender_factory
from kdive.providers.remote_libvirt.config import RemoteAuthorityBinding
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend

_NAMES = (
    "configured_success",
    "untrusted_client_denial",
    "non_configured_destination_denial",
    "preserved_af_unix_success",
)
_DENIALS = frozenset(
    {
        "authority: tls-rejected",
        "authority: transport-failed",
        "authority: deadline-exceeded",
        "authority: invalid-response",
    }
)

# This fixed remote program accepts only the instance and a borrowed incarnation credential.
# Its TLS material and socket paths come from the installed role, never from proof input.
_UNIX_HELPER = """
import asyncio, json, ssl, sys
from kdive.providers.external_boot_authority.transport import (
    authority_server_name, encode_request_envelope, read_frame, MAX_ENVELOPE_BYTES,
)

async def main():
    value = json.loads(sys.stdin.buffer.read(16385))
    assert set(value) == {"instance", "credential"}
    root = "/etc/kdive/credentials/provider-authority/"
    context = ssl.create_default_context(cafile=root + "server-ca")
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(root + "health-client-certificate", root + "health-client-key")
    writer = None
    try:
        async with asyncio.timeout(10):
            reader, writer = await asyncio.open_unix_connection(
                "/run/kdive/provider-authority/request/authority.sock",
                ssl=context, server_hostname=authority_server_name(value["instance"]),
                ssl_handshake_timeout=10, ssl_shutdown_timeout=10,
            )
            frame = encode_request_envelope("health", {
                "schema": "external-boot-authority-health-v1"
            }, value["credential"])
            writer.write(len(frame).to_bytes(4, "big") + frame)
            await writer.drain()
            result = json.loads(await read_frame(reader, maximum=MAX_ENVELOPE_BYTES))
            assert result == {"status": "ok", "value": {
                "schema": "external-boot-authority-health-v1"
            }}
            writer.close()
            await writer.wait_closed()
    finally:
        if writer is not None:
            writer.close()
            writer.transport.abort()

try:
    asyncio.run(main())
except Exception:
    sys.exit(1)
print("health-acknowledged")
"""


class ProofConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instance: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    address: str
    port: int = Field(ge=1, le=65535)
    denied_address: str
    denied_port: int = Field(ge=1, le=65535)
    ssh_target: str | None = Field(default=None, min_length=1, max_length=255)
    secrets_root: str = Field(min_length=1, max_length=4096)
    credential_file: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def validate_inputs(self) -> ProofConfig:
        for text in (self.address, self.denied_address):
            address = IPv4Address(text)
            if str(address) != text or address.is_unspecified or address.is_multicast:
                raise ValueError("invalid-proof-config")
        if (self.address, self.port) == (self.denied_address, self.denied_port):
            raise ValueError("invalid-proof-config")
        unix_helper_argv(self.ssh_target)
        return self


def _protected_text(path: Path, maximum: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        status = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) not in (0o400, 0o600)
            or not 0 < status.st_size <= maximum
        ):
            raise ValueError("invalid-proof-config")
        value = stream.read(maximum + 1)
        if len(value) > maximum:
            raise ValueError("invalid-proof-config")
        return value


def load_config(path: Path) -> ProofConfig:
    try:
        return ProofConfig.model_validate_json(_protected_text(path, 16384))
    except OSError, ValueError:
        raise ValueError("invalid-proof-config") from None


def unix_helper_argv(target: str | None) -> list[str]:
    argv = ["sudo", "-n", "/opt/kdive-provider-authority/.venv/bin/python", "-c", _UNIX_HELPER]
    if target is None:
        return argv
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,254}", target):
        raise ValueError("invalid-proof-config")
    command = shlex.join(argv)
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", target, command]


async def collect_outcomes(
    configured: Callable[[], Awaitable[None]],
    untrusted: Callable[[], Awaitable[None]],
    denied: Callable[[], Awaitable[None]],
    unix: Callable[[], Awaitable[None]],
) -> dict[str, dict[str, bool | str]]:
    result = {}
    for name, probe, expect_denial in zip(
        _NAMES, (configured, untrusted, denied, unix), (False, True, True, False), strict=True
    ):
        try:
            async with asyncio.timeout(16):
                await probe()
        except Exception as exc:
            passed = (
                expect_denial
                and isinstance(exc, CategorizedError)
                and exc.category == ErrorCategory.INFRASTRUCTURE_FAILURE
                and str(exc) in _DENIALS
            )
            reason = "connection-denied" if passed else "probe-failed"
        else:
            passed = not expect_denial
            reason = "unexpected-acceptance" if expect_denial else "health-acknowledged"
        result[name] = {"passed": passed, "reason": reason}
    return result


async def run_proof(config: ProofConfig) -> dict[str, dict[str, bool | str]]:
    credential = SecretStr(_protected_text(Path(config.credential_file), 8192).strip())
    backend = FileRefBackend(Path(config.secrets_root), SecretRegistry())
    factory = authority_sender_factory(backend, lambda: credential)
    binding = RemoteAuthorityBinding(
        config.instance,
        config.address,
        config.port,
        "server-ca",
        "client-certificate",
        "client-key",
    )

    async def network(route: RemoteAuthorityBinding) -> None:
        sender = factory(route)
        await sender.health(deadline=asyncio.get_running_loop().time() + 10)

    async def unix() -> None:
        process = await asyncio.create_subprocess_exec(
            *unix_helper_argv(config.ssh_target),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with asyncio.timeout(15):
                stdout, _ = await process.communicate(
                    json.dumps(
                        {"instance": config.instance, "credential": credential.get_secret_value()}
                    ).encode()
                )
                if process.returncode != 0 or stdout != b"health-acknowledged\n":
                    raise ValueError("probe-failed")
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    return await collect_outcomes(
        lambda: network(binding),
        lambda: network(
            replace(
                binding,
                client_cert_ref="untrusted-certificate",
                client_key_ref="untrusted-key",  # pragma: allowlist secret - filename reference
            )
        ),
        lambda: network(replace(binding, address=config.denied_address, port=config.denied_port)),
        unix,
    )


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValueError("invalid-proof-config")
        result = asyncio.run(run_proof(load_config(Path(sys.argv[1]))))
    except Exception:
        result = {name: {"passed": False, "reason": "invalid-proof-config"} for name in _NAMES}
    print(json.dumps(result, sort_keys=True))
    return 0 if all(value["passed"] is True for value in result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
