"""Init-only client and atomic handoff for Kubernetes worker credentials."""

from __future__ import annotations

import asyncio
import os
import ssl
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import kdive.config as config
from kdive.config.core_settings import (
    KUBERNETES_CREDENTIAL_BROKER_CA,
    KUBERNETES_CREDENTIAL_BROKER_HOST,
    KUBERNETES_CREDENTIAL_BROKER_PORT,
    POD_NAME,
    POD_NAMESPACE,
    POD_UID,
)
from kdive.processes.kubernetes_credential_broker import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    BrokerReply,
    BrokerRequest,
    decode_reply,
    encode_request,
    read_frame,
    write_frame,
)

type Token = Callable[[], str]
type Exchange = Callable[[BrokerRequest], Awaitable[BrokerReply]]

_PROJECTED_TOKEN = Path("/run/kdive/worker-credential-token/token")
_CREDENTIAL_PATH = Path("/run/kdive/worker-incarnation-credential")


def write_credential(path: Path, credential: str) -> None:
    """Atomically write the one worker-readable credential with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(descriptor, 0o400)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handoff:
            handoff.write(f"{credential}\n")
            handoff.flush()
            os.fsync(handoff.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class KubernetesCredentialInit:
    """Retry delivery and durable acknowledgment before allowing worker startup."""

    namespace: str
    name: str
    uid: str
    credential_path: Path
    token: Token
    exchange: Exchange
    retry_delay: float = 0.2

    async def run(self) -> None:
        """Write once after delivery, then retry acknowledgment without re-delivering the secret."""

        def request(operation: str) -> BrokerRequest:
            return BrokerRequest(operation, self.token(), self.namespace, self.name, self.uid)

        while True:
            try:
                reply = await self.exchange(request("deliver"))
            except OSError, asyncio.IncompleteReadError:
                await asyncio.sleep(self.retry_delay)
                continue
            if reply.credential is not None:
                write_credential(self.credential_path, reply.credential)
                break
            if reply.refused:
                raise RuntimeError("worker credential delivery was refused")
            await asyncio.sleep(self.retry_delay)
        while True:
            try:
                reply = await self.exchange(request("ack"))
            except OSError, asyncio.IncompleteReadError:
                await asyncio.sleep(self.retry_delay)
                continue
            if reply.acknowledged:
                return
            if reply.refused:
                raise RuntimeError("worker credential acknowledgment was refused")
            await asyncio.sleep(self.retry_delay)


def projected_token() -> str:
    """Read the init-only Pod UID-bound service-account token."""
    token = _PROJECTED_TOKEN.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("worker credential projected token is empty")
    return token


async def exchange_tls(request: BrokerRequest, *, host: str, port: int, ca: Path) -> BrokerReply:
    """Send one framed request to the private broker with certificate verification."""
    context = ssl.create_default_context(cafile=str(ca))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    reader, writer = await asyncio.open_connection(host, port, ssl=context, server_hostname=host)
    try:
        await write_frame(writer, encode_request(request), maximum=MAX_REQUEST_BYTES)
        return decode_reply(await read_frame(reader, maximum=MAX_RESPONSE_BYTES, kind="response"))
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()


async def run_init() -> None:
    """Gate worker startup on a durably acknowledged authority credential handoff."""
    host = config.require(KUBERNETES_CREDENTIAL_BROKER_HOST)
    port = config.require(KUBERNETES_CREDENTIAL_BROKER_PORT)
    ca = Path(config.require(KUBERNETES_CREDENTIAL_BROKER_CA))
    init = KubernetesCredentialInit(
        namespace=config.require(POD_NAMESPACE),
        name=config.require(POD_NAME),
        uid=config.require(POD_UID),
        credential_path=_CREDENTIAL_PATH,
        token=projected_token,
        exchange=lambda request: exchange_tls(request, host=host, port=port, ca=ca),
    )
    await init.run()


if __name__ == "__main__":
    asyncio.run(run_init())
