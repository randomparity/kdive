"""Measure external-build finalization over the real MCP path (#2318, ADR-0656).

Drives `investigations.open` → `runs.create` → `artifacts.create_run_upload` → presigned PUT →
`runs.complete_build` against the live stack, then correlates the server's measurement record
for that Run out of `server.log`, and writes one JSON row per bundle.

The Run is deliberately **unbound** — no allocation, no System, no VM. Finalization is what is
being measured, and binding a System would add provisioning time and a provider dependency to
every row without changing what `complete_build` does.

Two clocks, kept apart because conflating them is how the decision becomes unfalsifiable:

- `SUPPORTED_BUDGET_S` (300 s) is the read timeout the MCP SDK applies when the clients this
  repository ships pass none, which `LiveStackClient.over_http` does not override. It is the
  threshold ADR 0655 tests against. The 30-second figure is the *connect* default and is not the
  request bound; an earlier revision of this harness took one for the other and the resulting
  ADR selected the opposite branch.
- `KDIVE_MEASUREMENT_TIMEOUT_S` (default 1800 s) is what *this driver* runs under, so a
  finalization that would breach the budget is recorded rather than truncated at it. A driver
  bounded by the budget could not measure the case that decides whether the budget is enough.

The driver measures; it does not gate. It asserts the record is present, the outcome succeeded,
and the phases sum within the total — never a duration threshold. Comparing against the budget
is ADR 0655's job, from the recorded rows.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.resources.external_build_contract import EXTERNAL_BUILD_CONTRACT_URI
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.measurement import (
    SUPPORTED_BUDGET_S,
    MeasurementRow,
    bundle_for_arch,
    read_measurement_record,
)
from tests.integration.live_stack.spine import (
    accepted_run_upload_names,
    build_profile,
    mint_role_token,
    ok,
    put_presigned,
    scalar,
    sha256_b64,
)

_PROJECT = "spine-proj"
_AGENT_SESSION = "measure-sess"
_STAGE_DIR_ENV = "KDIVE_MEASUREMENT_STAGE_DIR"
_OUT_ENV = "KDIVE_MEASUREMENT_OUT"
_TIMEOUT_ENV = "KDIVE_MEASUREMENT_TIMEOUT_S"
_LOG_DIR_ENV = "KDIVE_STACK_LOG_DIR"
_HEALTH_ENV = "KDIVE_HEALTH_BIND_ADDR"

_DEFAULT_TIMEOUT_S = 1800.0
_DEFAULT_HEALTH_ADDR = "127.0.0.1:9464"


def _measurement_timeout_s() -> float:
    return float(os.environ.get(_TIMEOUT_ENV, _DEFAULT_TIMEOUT_S))


def _stage_dir(tmp_path: Path) -> Path:
    """Where `combined_kernel_tar` stages `modules_install` and writes the tar.

    Defaults to pytest's `tmp_path`, which is fine for a small bundle. A large one stages a
    module tree of tens of GB beside the tar, and the system temp root is commonly tmpfs
    (RAM-backed) with pytest retaining the last three runs — so a caller measuring a large
    bundle points this at real disk.
    """
    configured = os.environ.get(_STAGE_DIR_ENV)
    if not configured:
        return tmp_path
    stage = Path(configured)
    stage.mkdir(parents=True, exist_ok=True)
    return stage


def _server_log() -> Path:
    """`server.log` in the live-stack log directory (`scripts/live-stack/lib.sh`)."""
    configured = os.environ.get(_LOG_DIR_ENV)
    if configured:
        return Path(configured) / "server.log"
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / ".live-stack-logs" / "server.log"


def _deployed_revision() -> dict[str, Any]:
    """The build the server process is running, from the aux listener (ADR-0482 §1)."""
    addr = os.environ.get(_HEALTH_ENV, _DEFAULT_HEALTH_ADDR)
    with urlopen(f"http://{addr}/readyz", timeout=10) as response:  # noqa: S310  loopback only
        body = json.loads(response.read().decode())
    # /readyz nests the deployed build under "version":
    #   {"ready": true, "checks": {...}, "version": {"version", "commit", "is_release",
    #    "started_at"}}
    deployed = body.get("version")
    if not isinstance(deployed, dict):
        raise RuntimeError(f"/readyz carried no deployed-version object: {body!r}")
    return {
        "version": deployed.get("version"),
        "commit": deployed.get("commit"),
        "is_release": deployed.get("is_release"),
        "started_at": deployed.get("started_at"),
    }


def _token(issuer: OidcIssuer, *, role: str) -> str:
    return mint_role_token(issuer, project=_PROJECT, agent_session=_AGENT_SESSION, role=role)


def _write_row(row: dict[str, Any]) -> None:
    """Append one JSON row, and always print it so a run without the env var still reports."""
    rendered = json.dumps(row, sort_keys=True)
    print(f"\nMEASUREMENT {rendered}")
    out = os.environ.get(_OUT_ENV)
    if out:
        with Path(out).open("a", encoding="utf-8") as handle:
            handle.write(rendered + "\n")


@pytest.mark.live_stack
@pytest.mark.parametrize("arch", ["x86_64", "ppc64le"])
def test_external_build_finalization_is_measured(arch: str, tmp_path: Path) -> None:
    """Record one finalization's phase attribution over the real MCP path.

    Skips cleanly without a stack, and — for the ppc64le arm — without a bundle. A bundle that
    is set but unusable raises rather than skipping: a measurement that silently produces no row
    is indistinguishable from one nobody started.
    """
    issuer = require_issuer()
    base_url = require_stack()
    tar = bundle_for_arch(arch, _stage_dir(tmp_path))

    bundle_bytes = tar.stat().st_size
    with tarfile.open(tar) as archive:
        member_count = len(archive.getmembers())

    deployed = _deployed_revision()
    timeout_s = _measurement_timeout_s()
    operator_token = _token(issuer, role="operator")

    async def _run() -> MeasurementRow:
        # Built here rather than via LiveStackClient.over_http: that helper passes no timeout,
        # so it runs at exactly the budget under test — which would cut off any arm that
        # breached it, turning the one result that matters into a transport error.
        transport = StreamableHttpTransport(
            url=base_url, headers={"Authorization": f"Bearer {operator_token}"}
        )
        op = LiveStackClient(Client(transport, timeout=timeout_s))
        async with op:
            env = ok(
                await scalar(
                    op,
                    "investigations.open",
                    **{"project": _PROJECT, "title": f"finalization-measurement-{arch}"},
                ),
                "open-investigation",
            )
            investigation_id = env.object_id

            # No system_id: an unbound Run. `target_kind` is then required, and the profile arch
            # is what the server validates the boot member against (`_build_arch` defaults to
            # x86_64 when absent, so the ppc64le arm would be rejected rather than measured).
            env = ok(
                await scalar(
                    op,
                    "runs.create",
                    **{
                        "investigation_id": investigation_id,
                        "build_profile": build_profile(arch=arch),
                        "target_kind": "local-libvirt",
                    },
                ),
                "create-run",
            )
            run_id = env.object_id

            contract = json.loads(await op.read_text_resource(EXTERNAL_BUILD_CONTRACT_URI))
            accepted = accepted_run_upload_names(contract)
            assert "kernel" in accepted, f"upload contract no longer accepts 'kernel': {accepted}"

            env = ok(
                await scalar(
                    op,
                    "artifacts.create_run_upload",
                    **{
                        "run_id": run_id,
                        "artifacts": [
                            {
                                "name": "kernel",
                                "sha256": sha256_b64(tar),
                                "size_bytes": bundle_bytes,
                            }
                        ],
                    },
                ),
                "create-run-upload",
            )
            # The presigned URL is on the per-artifact item, not the envelope.
            by_name = {item.data.get("name"): item for item in env.items}
            assert "kernel" in by_name, "create_run_upload returned no 'kernel' item"
            await put_presigned(by_name["kernel"], tar)

            started = time.monotonic()
            env = ok(await scalar(op, "runs.complete_build", run_id=run_id), "complete-build")
            client_elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)

        row = read_measurement_record(_server_log(), run_id)
        assert row is not None, (
            f"no measurement record for run {run_id} in {_server_log()}; the server did not "
            "emit one, or the stack writes its log elsewhere"
        )
        assert row.outcome == "succeeded", f"finalization did not succeed: {row.outcome}"
        assert row.accounted_ms <= row.total_ms, (
            f"phases {row.accounted_ms}ms exceed total {row.total_ms}ms"
        )

        _write_row(
            {
                "arch": arch,
                "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "bundle_bytes": bundle_bytes,
                "bundle_members": member_count,
                "supported_budget_ms": SUPPORTED_BUDGET_S * 1000.0,
                "driver_timeout_ms": timeout_s * 1000.0,
                "client_elapsed_ms": client_elapsed_ms,
                "prepare_ms": row.prepare_ms,
                "reassemble_ms": row.reassemble_ms,
                "queue_wait_ms": row.queue_wait_ms,
                "scan_ms": row.scan_ms,
                "publish_ms": row.publish_ms,
                "total_ms": row.total_ms,
                "store_requests": row.store_requests,
                "store_bytes": row.store_bytes,
                "store_wait_ms": row.store_wait_ms,
                "chunked": row.chunked,
                "outcome": row.outcome,
                "deployed_version": deployed["version"],
                "deployed_commit": deployed["commit"],
                "deployed_started_at": deployed["started_at"],
                "deployed_is_release": deployed["is_release"],
                "host_cpu_count": os.cpu_count(),
                "host_machine": platform.machine(),
            }
        )
        return row

    asyncio.run(_run())
