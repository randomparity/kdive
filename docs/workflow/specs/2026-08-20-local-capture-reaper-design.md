# Local-libvirt orphaned-capture reaper — design

Implements #1948 under ADR-0556 (sweep contract) and ADR-0567 (this entry's delegated
decisions). Charter: `WORK:SCOPE` token `q1948-k7r2` on the issue.

## Outcome

A concrete `CaptureReaper` for the `local-libvirt` kind, registered in
`providers/assembly/composition.py`'s reaper builders, so the ADR-0556 sweep dispatches
terminal local capture rows to it. Plus the prepare-time pre-delete that closes local's
stale-pcap gap.

## Requirements (trace to issue acceptance criteria)

- **R1 (order)** — the reaper detaches `capture_qom_id(job_id)` on the captured domain
  *before* unlinking `pcap_path(system_id, job_id)`. A genuine filter/detach error aborts
  before the unlink. Tests assert call order.
- **R2 (tolerance)** — an already-missing domain (`VIR_ERR_NO_DOMAIN`), an already-missing
  filter, and an already-missing pcap file (`missing_ok`) are each tolerated and do not fail
  the reclaim. The filter-absence matcher is the live capturer's exact message-text matcher
  (`traffic_capture.py` `_is_not_found`): the raised `libvirt.libvirtError`'s lowercased text
  must contain `"not found"` or `"devicenotfound"`. It is message-based because QMP
  passthrough errors carry no distinct `VIR_ERR_*` code (the live capturer's module docstring
  documents this); a timeout or protocol error text does not match, so a genuine detach
  failure still aborts before the unlink.
  Every tolerated absence proceeds: a missing filter still unlinks; a missing domain ends the
  detach step with no QMP call attempted (there is no domain to address) and the unlink still
  runs — so a row with a missing domain *and* a missing filter reclaims cleanly and returns
  `True` (test case 4b).
- **R3 (colocation answered)** — answered by ADR-0567: reconciler-side reaper over
  `qemu:///system`, unlink on the shared host path. The implementation is that shape.
- **R4 (stale pcap settled)** — closed: `LocalLibvirtTrafficCapture.prepare` unlinks this
  job's own stale pcap (best-effort, `missing_ok`, suppress `OSError`) before returning the
  path. Job-keyed only — never a whole-System sweep, which would remove a concurrent
  capture's live file.
- **R5 (guardrails)** — `just ci` green.

## Components

### `LocalLibvirtCaptureReaper` (`src/kdive/providers/local_libvirt/reaping.py`)

Joins the existing `LibvirtInfraReaper` in the local reaping module. Mirrors the remote
reaper's structure (standalone, narrow protocols, blocking core offloaded with
`asyncio.to_thread`):

```python
class _CaptureConn(Protocol):
    def lookupByName(self, name: str) -> object: ...   # noqa: N802
    def close(self) -> int: ...

type CaptureConnect = Callable[[], _CaptureConn]
type CaptureMonitor = Callable[[object, str, int], str]

class LocalLibvirtCaptureReaper:
    def __init__(self, *, connect: CaptureConnect, monitor: CaptureMonitor) -> None: ...
    @classmethod
    def from_env(cls) -> LocalLibvirtCaptureReaper:
        """KDIVE_LIBVIRT_URI (default qemu:///system) + libvirt_qemu.qemuMonitorCommand;
        lazy import of the QEMU binding; opens no connection."""
    async def reclaim_capture(self, capture: OrphanedCapture) -> bool: ...
```

`reclaim_capture` returns `True` on every path that reached the unlink step (including
tolerated absences) and raises otherwise — the same only-True shape the remote reaper chose,
so a `False` decline is never a disguised failure. Failure taxonomy:

- connect / domain lookup (non-absence) / QMP `object-del` (non-absence) →
  `CategorizedError(CONTROL_FAILURE)`, details `{"domain": domain_name}` — same category the
  live capturer uses for these verbs.
- unlink `OSError` (absence already handled by `missing_ok`) →
  `CategorizedError(INFRASTRUCTURE_FAILURE)`, details `{"path": <pcap path>}`.

The connection is closed on both success and raise paths (the live capturer's
try/finally `_close` pattern; close-time errors are logged, never raised).

### Prepare pre-delete (`src/kdive/providers/local_libvirt/lifecycle/traffic_capture.py`)

`prepare` gains, after `prepare_pcap_dir(system_id)`:

```python
with contextlib.suppress(OSError):
    pcap_path(system_id, job_id).unlink(missing_ok=True)
```

Best-effort here (unlike the reaper's unlink) because the job path has a convergent backstop:
a file that could not be removed is truncated by `filter-dump` on attach and is the sweep's
candidate if the job dies anyway. The docstring states the job-keyed scope and the retry
rationale (mirrors remote's `prepare` docstring).

### Wiring

- `local_libvirt/composition.py` `build_capture_reaper` returns
  `LocalLibvirtCaptureReaper.from_env()`; docstring updated from "disabled wiring" to the
  concrete shape (ADR-0567).
- `providers/assembly/composition.py` `build_reconciler_capture_reapers` docstring: local
  kind is concrete (#1948); no code change — the builders dict already calls
  `local_composition.build_capture_reaper`.
- `reconciler/loop.py` and `mcp/tools/ops/reconcile/reconcile.py` comments saying local ships
  disabled: updated to past-tense/both-concrete wording.
- `providers/infra/reaping.py` port docstring's "#1948's local-libvirt reaper connects over
  `qemu:///system` and does not use this seam" stays — now descriptive of landed code.

## Testing

Unit tests, no libvirt daemon required (fake conn/domain via injected `connect`/`monitor`,
tmp_path for the pcap file), mirroring `tests/providers/remote_libvirt/reaping/test_capture_reaper.py`:

1. Port conformance: `isinstance(reaper, CaptureReaper)`.
2. Order: monitor `object-del` recorded before the pcap unlink (call-order assertion, R1).
3. Missing filter tolerated → unlink still happens (R2).
4. Missing domain tolerated → unlink still happens (R2).
4b. Missing domain *and* missing pcap together → no QMP call attempted, unlink attempted,
    returns `True` (the concurrent-absence flow R2 specifies).
5. Missing pcap tolerated → returns True (R2).
6. Non-not-found monitor error → `CategorizedError` CONTROL_FAILURE, no unlink, connection
   closed (R1's abort-before-unlink).
7. Non-absence unlink OSError (permission) → `CategorizedError` INFRASTRUCTURE_FAILURE.
8. `from_env` defaults: reads `KDIVE_LIBVIRT_URI`, lazy (no connection at construction).
9. Prepare pre-delete: creates two distinct job-id files under the same System's pcap dir,
   invokes `prepare` for one job, and asserts that job's stale file is gone while the other
   job's file is unchanged; an absent stale file is a no-op (R4's job-keyed scope).

Wiring tests (`tests/reconciler/test_capture_reaping_wiring.py`): the local kind flips from
disabled to concrete — `dispatchable_capture_kinds` now yields both kinds; the two
`{"remote-libvirt"}`-only assertions become `{"local-libvirt", "remote-libvirt"}`.

Timeout posture: the reaper issues libvirt's default (unbounded) local-socket connect and QMP
calls — the identical transport behavior of the live capturer and the ADR-0111 local
`InfraReaper`, over a local unix socket with no TCP dial to bound. A wedged libvirt daemon
stalling a reaper call is sweep-level exposure this change neither creates nor widens; the
pass budget already limits a stall to one candidate per pass, and bounding the stall itself is
owned by #1981. The residual is accepted for this entry under that explicit condition: stall
bounding stays unbounded until #1981's sweep-level bounding lands, and the exposure is
identical to the already-shipped local reapers running on the same reconciler process — this
entry adds no new stall surface. A wedged call is cleared by reconciler restart; the row stays
eligible and is retried on the next pass.

## Threat model

Security-relevant by the destructive-host-file criterion, judged on intent.

- **Boundaries**: (1) sweep row (Postgres ownership chain) → reaper arguments — enters as
  typed `UUID`s/`str` resolved by the sweep's SQL joins, under the reconciler's control;
  (2) reaper → local hypervisor via QMP `object-del` on a libvirt socket requiring root;
  (3) reaper → host filesystem `unlink` of a UUID-derived path.
- **Actor model**: no untrusted runtime actor reaches this code. The reconciler is a root
  host process; rows come from the operator's own database; the domain name and UUIDs come
  from the persisted ownership chain, not from agent input. Trust placement: the database is
  trusted state of record (standing ADR assumption).
- **Controls**: path derivation is `pcap_path(system_id, job_id)` from typed `UUID`s — no
  string interpolation of free-form values, no traversal surface; the per-job advisory
  ownership fence (ADR-0556) excludes a live worker's row; the settle window and
  detach-before-unlink ordering bound the destructive window; error taxonomy keeps failures
  observable rather than swallowed.
- **Out of scope**: a hostile database row (would already own the whole reconciler); SELinux
  confinement of the reconciler itself (deployment runs it unconfined-root, same as the
  existing dump-volume reaper); multi-host local deployments (contradict local-libvirt's
  definition).

## Non-goals

No remote-libvirt behavior change; no #1951/#1952 quiescence/publication work; no MCP tool
surface change; no schema or migration.
