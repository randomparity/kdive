"""Resource-bound remote NBD ordering proof and evidence (#2816, ADR-0558)."""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import select
import shlex
import subprocess  # noqa: S404 - fixed SSH protocol  # nosec B404
import tempfile
from contextlib import contextmanager
from multiprocessing.connection import Connection
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import libvirt
import libvirt_qemu
import psycopg

from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.providers.ports.traffic import RemoteCaptureConfiguration
from kdive.providers.remote_libvirt.composition import (
    build_capture_quiescence,
    capture_operation_configuration,
)
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    TlsCertRefs,
    remote_config_for_resource,
    remote_instance_names,
)
from kdive.providers.remote_libvirt.connection.transport import remote_connection
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings
from tests.integration.live_stack.remote_lifecycle import (
    REMOTE_REPRESENTATIVES,
    observer,
    on_remote_system,
    remote_host,
    staged_base_volume,
    volume_sha256,
)
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.tool_cells import observe_guest

# This fixed helper neither interprets commands nor writes host files. EOF and a
# total deadline release the peer even if the owning test disappears.
_LISTENER = r"""
import select, socket, sys, time
end = time.monotonic() + 60
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    print(listener.getsockname()[1], flush=True)
    ready, _, _ = select.select([listener, sys.stdin], [], [], max(0, end-time.monotonic()))
    if listener not in ready:
        raise SystemExit(2)
    peer, _ = listener.accept()
    with peer:
        print("accepted", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], max(0, end-time.monotonic()))
        if not ready or sys.stdin.readline().strip() != "release":
            raise SystemExit(3)
print("closed", flush=True)
"""


class Barrier:
    """One bounded provider-local listener, controlled only by its owning SSH process."""

    def __init__(self, destination: str) -> None:
        self.process = subprocess.Popen(  # noqa: S603,S607  # nosec B603 B607
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                destination,
                "python3 -u -c " + shlex.quote(_LISTENER),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.released = False

    def read(self, timeout: float = 10) -> str:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        assert ready, "provider listener did not report its next event"
        line = self.process.stdout.readline().strip()
        assert line, "provider listener exited before its next event"
        return line

    def release(self) -> None:
        if not self.released:
            self.released = True
            assert self.process.stdin is not None
            self.process.stdin.write("release\n")
            self.process.stdin.flush()

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.release()
                self.process.wait(timeout=5)
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()


@contextmanager
def tls(configuration: RemoteCaptureConfiguration):  # noqa: ANN201 - contextmanager inferred
    """Use the Resource's production TLS materialization for a native monitor client."""
    registry = SecretRegistry()
    config = RemoteLibvirtConfig(
        uri=configuration.uri,
        cert_refs=TlsCertRefs(
            client_cert_ref=configuration.client_cert_ref,
            client_key_ref=configuration.client_key_ref,
            ca_cert_ref=configuration.ca_cert_ref,
        ),
        concurrent_allocation_cap=1,
        storage_pool=configuration.storage_pool,
    )
    with remote_connection(
        config,
        FileRefBackend(Path(configuration.secrets_root), registry),
        open_connection=libvirt.open,
    ) as connection:
        yield connection


def mutate(configuration: RemoteCaptureConfiguration, domain: str, node: str, port: int) -> None:
    """Native QMP blocks while the accepted NBD peer withholds its handshake."""
    with tls(configuration) as connection:
        command = {
            "execute": "blockdev-add",
            "arguments": {
                "driver": "nbd",
                "node-name": node,
                "server": {"type": "inet", "host": "127.0.0.1", "port": str(port)},
            },
        }
        libvirt_qemu.qemuMonitorCommand(connection.lookupByName(domain), json.dumps(command), 0)


def probe(
    configuration: RemoteCaptureConfiguration, domain: str, node: str, events: Connection
) -> None:
    """Observe native-call entry without substituting the production probe or its results."""
    native = libvirt_qemu.qemuMonitorCommand

    def entered(domain: object, cmd: str, flags: int) -> str:
        if json.loads(cmd)["execute"] == "object-del":
            events.send("entered")
        return native(domain, cmd, flags)

    try:
        with patch.object(libvirt_qemu, "qemuMonitorCommand", entered):
            evidence = build_capture_quiescence(configuration).prove_absent(
                configuration.resource_id, domain, node
            )
        events.send(
            (
                str(evidence.resource_id),
                evidence.domain_name,
                evidence.qom_id,
                evidence.result,
                evidence.ordering,
            )
        )
    except Exception as exc:
        events.send(("failed", type(exc).__name__))
    finally:
        events.close()


def receive(events: Connection, timeout: float) -> object:
    assert events.poll(timeout), "fresh monitor did not report its next event"
    return events.recv()


def ordered(
    configuration: RemoteCaptureConfiguration, domain: str, destination: str
) -> dict[str, object]:
    """Require acceptance, a held fresh probe, client death, then ordered absence."""
    node = "q2816-" + uuid4().hex
    context = multiprocessing.get_context("spawn")
    receive_end, send_end = context.Pipe(duplex=False)
    barrier = Barrier(destination)
    mutator = None
    fresh = None
    try:
        port = int(barrier.read())
        assert 0 < port < 65536, "invalid listener port"
        mutator = context.Process(target=mutate, args=(configuration, domain, node, port))
        mutator.start()
        assert barrier.read() == "accepted", "NBD mutation was not accepted"
        assert mutator.is_alive(), "accepted mutation returned before release"
        fresh = context.Process(target=probe, args=(configuration, domain, node, send_end))
        fresh.start()
        assert receive(receive_end, 10) == "entered", "fresh native probe did not enter"
        assert not receive_end.poll(0.5), "fresh probe acknowledged a held mutation"
        mutator.terminate()
        mutator.join(5)
        assert not mutator.is_alive(), "mutation client did not terminate"
        assert not receive_end.poll(0.5), "client death was mistaken for quiescence"
        barrier.release()
        assert barrier.read() == "closed", "listener did not close"
        assert barrier.process.wait(5) == 0, "listener cleanup failed"
        expected = (str(configuration.resource_id), domain, node, "absent", "fresh-qmp-connection")
        assert receive(receive_end, 15) == expected, "fresh probe did not prove matching absence"
        fresh.join(5)
        assert fresh.exitcode == 0, "fresh probe process did not finish cleanly"
        with tls(configuration) as connection:
            reply = json.loads(
                libvirt_qemu.qemuMonitorCommand(
                    connection.lookupByName(domain),
                    json.dumps({"execute": "query-named-block-nodes"}),
                    0,
                )
            )
        assert isinstance(reply.get("return"), list), "block-node cleanup query failed"
        assert all(item.get("node-name") != node for item in reply["return"]), "NBD node remains"
        return {
            "accepted": True,
            "client_terminated": True,
            "held_before_s": 0.5,
            "held_after_s": 0.5,
            "ordering": "fresh-qmp-connection",
            "node_absent": True,
            "listener_closed": True,
            "resource_and_domain_matched": True,
        }
    finally:
        # Release the hypervisor before stopping clients or allowing System teardown.
        try:
            barrier.close()
        finally:
            for process in (mutator, fresh):
                if process is not None:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)
                    assert not process.is_alive(), "owned monitor process survived cleanup"
            receive_end.close()
            send_end.close()


async def configuration_for(db_url: str, system_id: str) -> RemoteCaptureConfiguration:
    """Read the actual System's Resource; never invent a fixture Resource identity."""
    async with await psycopg.AsyncConnection.connect(db_url) as connection:
        cursor = await connection.execute(
            "SELECT r.id, r.name, r.kind, s.domain_name FROM systems s "
            "JOIN allocations a ON a.id=s.allocation_id JOIN resources r ON r.id=a.resource_id "
            "WHERE s.id=%s",
            (UUID(system_id),),
        )
        row = await cursor.fetchone()
    assert row is not None and row[2] == "remote-libvirt", "System is not remote Resource-bound"
    assert row[3] == domain_name_for(UUID(system_id)), "System domain identity mismatch"
    assert isinstance(row[1], str) and row[1], "Resource has no inventory name"
    return RemoteCaptureConfiguration.from_canonical_json(
        capture_operation_configuration(row[0], row[1])
    )


def verify_domain(configuration: RemoteCaptureConfiguration, domain: str, destination: str) -> None:
    """Fail before the barrier if TLS and operator observation select different domains."""
    names = remote_instance_names()
    assert len(names) == 1, "expected exactly one configured remote instance"
    assert configuration.uri == remote_config_for_resource(names[0]).uri, "Resource URI mismatch"
    connection = observer(destination)
    try:
        observed_uuid = connection.lookupByName(domain).UUIDString()
    finally:
        connection.close()
    with tls(configuration) as selected:
        assert selected.lookupByName(domain).UUIDString() == observed_uuid, (
            "TLS/observer domain mismatch"
        )


def cells() -> list[Cell]:
    return [
        cell
        for cell in build_contract().cells
        if cell.operation == "remote-quiescence" and cell.owner == 2816
    ]


async def scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    """Normal HTTP provisioning and cleanup surround the actual Resource-bound TLS proof."""
    host = remote_host()

    async def body(op: LiveStackClient, system_id: str, _owned: list[str]) -> None:
        with tempfile.TemporaryDirectory(prefix="remote-quiescence-") as directory:
            await observe_guest(
                run, op, system_id, Path(directory), REMOTE_REPRESENTATIVES["enterprise"]
            )
        configuration = await configuration_for(db_url, system_id)
        domain = domain_name_for(UUID(system_id))
        await asyncio.to_thread(verify_domain, configuration, domain, host.dest)
        result = await asyncio.to_thread(ordered, configuration, domain, host.dest)
        for assertion in ("accepted-mutation", "client-terminated", "fresh-monitor-ordering"):
            run.prove(assertion, result)

    await on_remote_system(
        run, base_url, issuer, db_url, project="remote-quiescence", family="enterprise", body=body
    )


def bindings(candidate: str) -> InputBindings:
    """Bind the staged image before running, independently of submitted evidence."""
    host = remote_host()
    image = REMOTE_REPRESENTATIVES["enterprise"]
    digest = volume_sha256(host.dest, host.pool, staged_base_volume(image))
    return InputBindings(
        version=1,
        candidate_sha=candidate,
        matrix_sha256=build_contract().matrix_sha256,
        cells={
            cell.id: Context.model_validate(
                {
                    "host_os": host.host_os,
                    "host_arch": host.host_arch,
                    "guest_os": f"{image.distro}:{image.version}",
                    "guest_arch": image.arch,
                    "accelerator": "kvm",
                    "image_sha256": digest,
                }
            )
            for cell in cells()
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Bind the remote quiescence image and host.")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.write_text(bindings(args.candidate).model_dump_json(indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
