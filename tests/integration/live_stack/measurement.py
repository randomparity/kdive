"""Readout for the external-build finalization measurement (#2318, ADR-0656).

`runs.complete_build` emits one measurement record per finalization attempt
(`complete_build._MEASUREMENT_EVENT`). The driver runs as an MCP client in a separate process
from the server that emits it, so the attribution is read back out of the server's log rather
than off the tool response — the charter forbids putting an internal cost breakdown on the
public MCP contract, and a response field would freeze it as one.

Two properties of the live-stack layout decide how this parses:

- **The server runs the OTel exporter, not `configure_logging`'s formatter.** `__main__`
  installs the stdlib floor, then `init_telemetry` builds the logger provider and
  `_bridge_root_logger` calls `remove_stdlib_floor()`, detaching that handler. So every line in
  `server.log` after startup comes from `format_log_record_json`, whose `logger` field is the
  OTel instrumentation-scope name rather than `record.name`. Keying on `logger` would silently
  match nothing; this keys on the message.
- **The payload rides in the message.** Both serializers merge only the `bind_context` fields,
  and `bind_context` rejects any name outside `CONTEXT_FIELDS`, so a `logging` `extra=`
  attribute reaches neither.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from kdive.services.runs.complete_build import _MEASUREMENT_EVENT

SUPPORTED_BUDGET_S = 300.0
"""How long a `runs.complete_build` call may take before the clients this repo ships give up.

`LiveStackClient.over_http` (`src/kdive/mcp/dev_harness.py:203-208`) and the CLI transport both
build `Client(transport)` with no timeout override, which leaves `read_timeout_seconds` as
`None`. Two consequences follow, and the second is the binding one:

- `StreamableHttpTransport.connect_session` builds an `httpx.Timeout` only when
  `read_timeout_seconds` is set; with `None` it falls through to the MCP SDK's
  `create_mcp_http_client`, whose default is
  `Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)`.
- `BaseSession.send_request` with both timeouts `None` calls `anyio.fail_after(None)`, so there
  is no session-level request timeout at all.

The bound on a long finalization is therefore the **read** timeout, 300 s. The 30 s figure that
an earlier draft of this constant carried is the *connect* default, which a request this long
has already cleared. It is checked by `test_supported_budget_matches_the_shipped_client`, which
reads the effective value off a client rather than comparing the constant to itself.

A module constant rather than an environment variable on purpose: the spec and the ADR both
rest on this being a property of the shipped client, so a knob would let whichever environment
runs the proof move the threshold the accepted decision rests on. The timeout the driver
*itself* runs under is separate and tunable (`KDIVE_MEASUREMENT_TIMEOUT_S`), because it must
outlast the budget to record a finalization that would breach it rather than truncating there.
"""

PPC64LE_BUNDLE_ENV = "KDIVE_PPC64LE_BUNDLE"
KERNEL_SRC_ENV = "KDIVE_KERNEL_SRC"


@dataclass(frozen=True, slots=True)
class MeasurementRow:
    """One finalization attempt's server-side attribution."""

    run_id: str
    prepare_ms: float
    reassemble_ms: float
    queue_wait_ms: float
    scan_ms: float
    publish_ms: float
    total_ms: float
    store_requests: int
    store_bytes: int
    store_wait_ms: float
    chunked: bool
    outcome: str

    @property
    def accounted_ms(self) -> float:
        """The four phases' sum, which cannot exceed ``total_ms``."""
        return (
            self.prepare_ms
            + self.reassemble_ms
            + self.queue_wait_ms
            + self.scan_ms
            + self.publish_ms
        )


def read_measurement_record(log_path: Path, run_id: str) -> MeasurementRow | None:
    """Return the last measurement row for ``run_id`` in ``log_path``, or ``None``.

    The *last* one, so a finalization retried within the same run reports its final attempt
    rather than an earlier failure. Lines that are not JSON, or carry no measurement message,
    are skipped: `server.log` holds every record the process emitted, and the file is also
    written while this reads it.
    """
    prefix = f"{_MEASUREMENT_EVENT} "
    found: MeasurementRow | None = None
    with log_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = envelope.get("msg")
            if not isinstance(message, str) or not message.startswith(prefix):
                continue
            try:
                payload = json.loads(message[len(prefix) :])
            except json.JSONDecodeError:
                continue
            if payload.get("run_id") != run_id:
                continue
            found = MeasurementRow(
                run_id=payload["run_id"],
                prepare_ms=float(payload["prepare_ms"]),
                reassemble_ms=float(payload["reassemble_ms"]),
                queue_wait_ms=float(payload["queue_wait_ms"]),
                scan_ms=float(payload["scan_ms"]),
                publish_ms=float(payload["publish_ms"]),
                total_ms=float(payload["total_ms"]),
                store_requests=int(payload["store_requests"]),
                store_bytes=int(payload["store_bytes"]),
                store_wait_ms=float(payload["store_wait_ms"]),
                chunked=bool(payload["chunked"]),
                outcome=str(payload["outcome"]),
            )
    return found


def ppc64le_bundle_preflight() -> Path:
    """Resolve the ppc64le bundle directory, or skip with the exact fix.

    **This deliberately diverges from `_ppc64le_bundle_preflight`
    (`tests/integration/test_live_stack.py`), which skips in both branches and also requires
    `initrd.img`.** Two reasons, both specific to a measurement harness:

    - A measurement that silently produces no row is worse than one that fails: the absence is
      indistinguishable from a run nobody started, and ADR 0655 records the arm as unrun either
      way. An unset variable is a legitimate "not asked for" and still skips; a variable that is
      set but unusable is a broken request and raises.
    - This arm boots nothing — it finalizes a Run and stops — so it needs no `initrd.img`.
    """
    configured = os.environ.get(PPC64LE_BUNDLE_ENV)
    if not configured:
        pytest.skip(
            f"set {PPC64LE_BUNDLE_ENV} to a directory holding kernel.tar.gz to measure the "
            "ppc64le arm (owned by docs/debt/0015-ppc64le-finalization-measurement-unrun.md)"
        )
    bundle = Path(configured)
    tar = bundle / "kernel.tar.gz"
    if not tar.is_file():
        raise RuntimeError(
            f"{PPC64LE_BUNDLE_ENV}={configured} does not contain kernel.tar.gz; "
            "the ppc64le measurement arm cannot run and must not report no row"
        )
    return bundle


def bundle_for_arch(arch: str, dest_dir: Path) -> Path:
    """Return the combined kernel tar to finalize for ``arch``.

    x86_64 cuts one from the built tree at ``KDIVE_KERNEL_SRC`` with the repository's own
    recipe; ppc64le takes the supplied bundle as-is, since no ppc64le tree is built here.
    """
    if arch == "ppc64le":
        return ppc64le_bundle_preflight() / "kernel.tar.gz"
    if arch != "x86_64":
        raise RuntimeError(f"no bundle source for arch {arch!r}; known: x86_64, ppc64le")
    configured = os.environ.get(KERNEL_SRC_ENV)
    if not configured:
        pytest.skip(f"set {KERNEL_SRC_ENV} to a built x86_64 kernel tree to measure this arm")
    # Imported here rather than at module import: spine pulls in the whole live-stack harness,
    # and the readout's unit tests must import this module without it.
    from tests.integration.live_stack.spine import combined_kernel_tar

    return combined_kernel_tar(Path(configured), dest_dir, arch="x86_64")
