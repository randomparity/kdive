"""Operator-run end-to-end proof for post-readiness console-part artifacts (#892, ADR-0095/0235).

``live_vm``-gated: needs the full live stack (KDIVE_STACK_BASE_URL, KDIVE_OIDC_ISSUER,
KDIVE_DATABASE_URL) plus a local KVM host with the kdump guest image (KDIVE_GUEST_IMAGE) and
the kernel tree (KDIVE_KERNEL_SRC). Skips cleanly on any host where those are absent.

Proves the #892 gap is closed: a System whose Run has already reached ``succeeded`` continues
to accumulate ``console-part-<gen>-<index>`` artifacts via the reconciler's ``console_rotate``
sweep. Specifically:

1. ``artifacts.list(system_id)`` grows new ``console-part-*`` rows after the Run is terminal
   and a post-readiness workload keeps writing to the serial console.
2. ``artifacts.get`` on the newest console-part inflates the gzip to plaintext containing the
   unique post-readiness proof marker written via the qemu guest agent AFTER the boot step
   completed and captured the frozen evidence.
3. ``artifacts.get`` on the frozen per-Run ``console-<run>`` evidence (``runs.get``
   ``refs["console"]``) does NOT contain that marker — proving it was captured before the
   workload and cannot have drifted post-hoc.

Required env: KDIVE_GUEST_IMAGE (built rootfs), KDIVE_KERNEL_SRC (source tree),
KDIVE_DATABASE_URL (Postgres), KDIVE_STACK_BASE_URL (MCP HTTP server), KDIVE_OIDC_ISSUER.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from uuid import UUID, uuid4

import libvirt
import pytest

from kdive.mcp.responses import ToolResponse
from kdive.prereqs.managed_ssh_key import managed_private_key_path
from kdive.providers.shared.libvirt_xml import recorded_ssh_port
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secrets import secrets_root_from_env
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.harness import LiveStackClient, OidcIssuer
from tests.integration.live_stack.spine import (
    LOCAL_ALLOCATION_DISK_GB,
    SpinePhaseError,
    await_system_state,
    drain_job,
    mint_role_token,
    ok,
    phase,
    scalar,
    seed_metering,
)
from tests.mcp.json_data import data_str

pytestmark = pytest.mark.live_vm

_GUEST_IMAGE_ENV = "KDIVE_GUEST_IMAGE"
_KERNEL_TREE_ENV = "KDIVE_KERNEL_SRC"
_DATABASE_URL_ENV = "KDIVE_DATABASE_URL"
_PROJECT = "console-parts-proof"
_AGENT_SESSION = "console-parts-sess"

# Shell used by the in-guest workload.
_SHELL = "/bin/sh"
# SSH user the rootfs --ssh-injects the managed key to (ADR-0052/0218 §1).
_SSH_USER = "root"
# Credential ref that opts the loopback SSH transport in (renders hostfwd + NIC). Its file content
# does not authenticate SSH — the kdive-managed key does — but its presence gates the transport.
_SSH_CREDENTIAL_REF = "drgn-ssh"
# Lines emitted to the kernel console: ~60 bytes × 5 000 = ~300 KiB, > 4 × 64 KiB threshold.
_PROOF_LINES = 5_000
# Timeout waiting for the reconciler's console_rotate sweep + worker drain.
_PARTS_POLL_DEADLINE_S = 300.0
_PARTS_POLL_INTERVAL_S = 5.0


def _preflight() -> tuple[OidcIssuer, str, str]:
    """Resolve live-stack env or skip with a clear, actionable message.

    Returns:
        A tuple of (OidcIssuer, base_url, db_url) when all prerequisites are present.
    """
    image = os.environ.get(_GUEST_IMAGE_ENV)
    if not image or not Path(image).exists():
        pytest.skip(
            f"{_GUEST_IMAGE_ENV} unset or points at a missing file; "
            "build the local-libvirt rootfs with `python -m kdive build-fs` and set the env var"
        )
    tree = os.environ.get(_KERNEL_TREE_ENV)
    if not tree or not Path(tree).exists():
        pytest.skip(f"{_KERNEL_TREE_ENV} unset or missing; fetch the kernel tree first")
    db_url = os.environ.get(_DATABASE_URL_ENV)
    if not db_url:
        pytest.skip(f"{_DATABASE_URL_ENV} unset; bring up the stack (see the live-stack runbook)")
    secret = secrets_root_from_env() / _SSH_CREDENTIAL_REF
    if not secret.is_file():
        pytest.skip(
            f"SSH credential ref {_SSH_CREDENTIAL_REF!r} not seeded at {secret}; seed any file "
            "there so the loopback SSH transport (the in-guest workload path) resolves"
        )
    issuer = require_issuer()
    base_url = require_stack()
    return issuer, base_url, db_url


def _token(issuer: OidcIssuer, *, role: str) -> str:
    return mint_role_token(issuer, project=_PROJECT, agent_session=_AGENT_SESSION, role=role)


def _provision_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 2048,
        "disk_gb": LOCAL_ALLOCATION_DISK_GB,
        "boot_method": "direct-kernel",
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "provider": {
            "local-libvirt": {
                "rootfs": {"kind": "local", "path": os.environ[_GUEST_IMAGE_ENV]},
                # Opt the loopback SSH transport in: this renders the SSH hostfwd + virtio NIC
                # (ADR-0218/0240) so the test can SSH-exec the post-readiness workload in-guest.
                # Local-libvirt domains carry no qemu guest-agent channel, so SSH is the in-guest
                # exec path (the same one drgn-live uses).
                "ssh_credential_ref": _SSH_CREDENTIAL_REF,
            }
        },
    }


def _build_profile() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kernel_source_ref": os.environ[_KERNEL_TREE_ENV],
        "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
    }


def _emit_proof_lines(domain: libvirt.virDomain, proof_marker: str) -> None:
    """Write post-readiness proof lines to the guest kernel console over loopback SSH.

    Local-libvirt domains carry no qemu guest-agent channel, so the in-guest workload is driven
    over the same loopback SSH transport drgn-live uses: the SSH ``hostfwd`` port recorded in the
    live domain XML (ADR-0218) plus the kdive-managed private key. The guest shell loop writes
    ``_PROOF_LINES`` records to ``/dev/kmsg`` — the kernel printk path, which reaches the
    ``console=ttyS0`` serial console with proper UART flow control (a direct userspace write to
    ``/dev/ttyS0`` races the kernel console + getty on the same pty and overflows its buffer, so
    most bytes are dropped). Output flows through virtlogd to the host console log file the
    ``console_rotate`` handler reads. ``printk_devkmsg=on`` disables the per-writer kmsg rate limit
    so every record reaches the console.

    Args:
        domain: The libvirt domain handle for the booted System.
        proof_marker: A unique string embedded in every emitted line, used to assert presence
            in the console-part artifact and absence from the frozen boot-window evidence.
    """
    port = recorded_ssh_port(domain.XMLDesc(0))
    if port is None:
        raise AssertionError(
            "no SSH hostfwd port in the domain XML; the provision profile must set "
            "provider.local-libvirt.ssh_credential_ref to render the SSH transport"
        )
    key_path = managed_private_key_path()
    if not key_path.is_file():
        raise AssertionError(f"kdive-managed SSH private key absent at {key_path}")
    # POSIX sh loop: $i is a shell variable, {proof_marker} and {_PROOF_LINES} are
    # Python f-string substitutions (safe: only hex + hyphens / a literal integer). One open of
    # /dev/kmsg for the whole loop; each echo is a single write() = one kernel log record.
    # Disabling the kmsg rate limit is load-bearing (under the default 'ratelimit' policy the
    # kernel drops all but a burst of records, so the marker would never reach a sealed part);
    # a checked write fails fast with a clear cause instead of a downstream 'marker absent'.
    script = (
        "echo on > /proc/sys/kernel/printk_devkmsg "
        "|| { echo 'cannot disable /dev/kmsg rate limit (printk_devkmsg)' >&2; exit 3; }; "
        "{ "
        f'i=0; while [ "$i" -lt {_PROOF_LINES} ]; do '
        f'echo "{proof_marker} line-$i"; '
        f"i=$((i+1)); "
        "done; } > /dev/kmsg"
    )
    argv = [
        "ssh",
        "-i",
        str(key_path),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(port),
        f"{_SSH_USER}@127.0.0.1",
        "--",
        _SHELL,
        "-c",
        script,
    ]
    result = subprocess.run(argv, capture_output=True, timeout=180.0, check=False)  # noqa: S603
    if result.returncode != 0:
        raise AssertionError(
            f"post-readiness proof workload failed (exit={result.returncode}): "
            f"{result.stderr.decode(errors='replace')!r}"
        )


def _console_part_ids(listing: ToolResponse) -> list[str]:
    """Return artifact ids of console-part-* rows from an artifacts.list response, newest-first.

    Args:
        listing: The ToolResponse from an ``artifacts.list`` call.

    Returns:
        Artifact ids whose object key contains ``console-part-``, in listing order
        (newest-first, as guaranteed by the ``ORDER BY created_at DESC`` listing query).
    """
    return [
        item.object_id for item in listing.items if "console-part-" in item.refs.get("object", "")
    ]


async def _poll_for_new_parts(
    op: LiveStackClient,
    system_id: str,
    initial_ids: set[str],
) -> str:
    """Poll ``artifacts.list`` until a new console-part-* artifact appears beyond ``initial_ids``.

    Args:
        op: The live-stack operator client.
        system_id: The System whose artifacts to poll.
        initial_ids: The set of console-part artifact ids seen before the workload started.

    Returns:
        The newest console-part artifact id not in ``initial_ids``.

    Raises:
        SpinePhaseError: When no new parts appear within ``_PARTS_POLL_DEADLINE_S``.
    """
    deadline = time.monotonic() + _PARTS_POLL_DEADLINE_S
    while True:
        listing = ok(await scalar(op, "artifacts.list", system_id=system_id), "poll-parts")
        current_ids = _console_part_ids(listing)
        new_ids_set = {aid for aid in current_ids if aid not in initial_ids}
        if new_ids_set:
            # current_ids is newest-first; return the newest among the new arrivals.
            return next(aid for aid in current_ids if aid in new_ids_set)
        if time.monotonic() >= deadline:
            raise SpinePhaseError(
                "console-parts",
                f"no new console-part artifacts within {_PARTS_POLL_DEADLINE_S:g}s "
                f"(initial={len(initial_ids)}, current={len(current_ids)})",
            )
        await asyncio.sleep(_PARTS_POLL_INTERVAL_S)


async def _full_text(op: LiveStackClient, artifact_id: str, phase_name: str) -> str:
    """Fetch the full plaintext content of a redacted artifact, paging through all windows.

    Args:
        op: The live-stack client to use for tool calls.
        artifact_id: The UUID of the redacted artifact to fetch.
        phase_name: The current phase name (for SpinePhaseError labelling).

    Returns:
        The complete decoded text of the artifact across all paged windows.
    """
    chunks: list[str] = []
    byte_offset = 0
    while True:
        env = ok(
            await scalar(op, "artifacts.get", artifact_id=artifact_id, byte_offset=byte_offset),
            phase_name,
        )
        content = env.data.get("content")
        if isinstance(content, str) and content:
            chunks.append(content)
        truncated = bool(env.data.get("content_truncated", False))
        if not truncated:
            break
        next_offset = env.data.get("next_offset")
        if next_offset is None:
            break
        byte_offset = int(str(next_offset))
    return "".join(chunks)


def test_post_readiness_console_parts_grow_beyond_run_evidence() -> None:
    """Post-readiness console-part artifacts exist and contain lines absent from frozen evidence.

    Provisions a local-libvirt System, boots a Run to ``succeeded``, emits unique
    post-readiness console lines over loopback SSH, waits for the reconciler's
    ``console_rotate`` sweep to seal new ``console-part-*`` artifacts, then asserts:

    1. New ``console-part-*`` artifacts appeared after the Run became terminal — proving
       rotation is keyed on System liveness, not Run terminality (#892).
    2. ``artifacts.get`` on the newest new part's plaintext contains the unique proof marker.
    3. ``artifacts.get`` on the frozen per-Run ``console-<run>`` evidence does NOT contain
       the proof marker — proving the boot-window snapshot cannot drift post-hoc.
    """
    issuer, base_url, db_url = _preflight()
    operator_token = _token(issuer, role="operator")
    # UUID hex: 32 hex chars, no special shell characters.
    proof_marker = f"CONSOLE-PARTS-PROOF-{uuid4().hex}"

    async def _run() -> None:
        op = LiveStackClient.over_http(base_url, operator_token)
        allocation_id = system_id = run_id = ""
        async with op:
            await seed_metering(db_url, _PROJECT)

            async with phase("allocate"):
                env = ok(
                    await scalar(
                        op,
                        "allocations.request",
                        project=_PROJECT,
                        request={
                            "vcpus": 2,
                            "memory_gb": 2,
                            "disk_gb": LOCAL_ALLOCATION_DISK_GB,
                            "resource": {"mode": "kind"},
                        },
                    ),
                    "allocate",
                )
                allocation_id = env.object_id

            async with phase("provision"):
                env = ok(
                    await scalar(
                        op,
                        "systems.provision",
                        allocation_id=allocation_id,
                        profile=_provision_profile(),
                    ),
                    "provision",
                )
                system_id = data_str(env, "system_id")
                await await_system_state(op, "provision", system_id, "ready")

            async with phase("open-investigation"):
                env = ok(
                    await scalar(
                        op,
                        "investigations.open",
                        project=_PROJECT,
                        title="console-parts-proof",
                    ),
                    "open-investigation",
                )
                investigation_id = env.object_id

            async with phase("create-run"):
                env = ok(
                    await scalar(
                        op,
                        "runs.create",
                        request={
                            "investigation_id": investigation_id,
                            "system_id": system_id,
                            "build_profile": _build_profile(),
                        },
                    ),
                    "create-run",
                )
                run_id = env.object_id

            for step in ("build", "install", "boot"):
                async with phase(step):
                    env = ok(await scalar(op, f"runs.{step}", run_id=run_id), step)
                    await drain_job(op, step, env.object_id)

            # Run is now `succeeded`; System is `ready`. The #892 scenario begins here.
            # Snapshot the console-part artifact ids that exist BEFORE the workload —
            # boot messages may have already produced some parts.
            async with phase("initial-part-snapshot"):
                initial_listing = ok(
                    await scalar(op, "artifacts.list", system_id=system_id),
                    "initial-part-snapshot",
                )
                initial_part_ids = set(_console_part_ids(initial_listing))

            # Emit the post-readiness workload over loopback SSH. The Run is already
            # `succeeded` (terminal) yet the in-guest process keeps writing to the serial
            # console — the exact #892 repro scenario.
            async with phase("emit-proof-lines"):
                libvirt_conn = libvirt.open("qemu:///system")
                try:
                    domain = libvirt_conn.lookupByName(domain_name_for(UUID(system_id)))
                    await asyncio.to_thread(_emit_proof_lines, domain, proof_marker)
                finally:
                    libvirt_conn.close()

            # Wait for the reconciler's console_rotate sweep to dispatch a worker job and
            # the worker to seal at least one NEW console-part artifact beyond the initial
            # set. The periodic reconciler loop is the advance mechanism (the live stack
            # runs a background reconciler).
            async with phase("wait-for-new-parts"):
                newest_part_id = await _poll_for_new_parts(op, system_id, initial_part_ids)

            # Fetch the newest new console-part artifact's full plaintext.
            async with phase("read-newest-part"):
                part_text = await _full_text(op, newest_part_id, "read-newest-part")

            # Fetch the frozen per-Run boot-window console evidence (the ``console-<run>``
            # artifact captured at the time the boot step completed).
            async with phase("read-frozen-evidence"):
                run_env = ok(await scalar(op, "runs.get", run_id=run_id), "runs.get")
                console_ref = run_env.refs.get("console")
                assert console_ref is not None, (
                    "runs.get returned no refs.console for a succeeded run "
                    "(boot evidence absent — #892 bootstrap failure)"
                )
                frozen_text = await _full_text(op, str(console_ref), "read-frozen-evidence")

            # Core #892 assertions.
            assert proof_marker in part_text, (
                f"proof marker absent from newest console-part artifact "
                f"(artifact={newest_part_id!r}, marker={proof_marker!r}); "
                f"part excerpt (first 500 chars): {part_text[:500]!r}"
            )
            assert proof_marker not in frozen_text, (
                "proof marker found in frozen Run console evidence — "
                "the boot-window snapshot must not include post-readiness lines (#892)"
            )

            # Release the allocation; the reconciler tears down the System.
            async with phase("release"):
                ok(
                    await scalar(op, "allocations.release", allocation_id=allocation_id),
                    "release",
                )

    asyncio.run(_run())
