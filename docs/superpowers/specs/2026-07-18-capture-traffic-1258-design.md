# Spec: Host-side network traffic capture (#1258)

- Issue: #1258 "Add Network Traffic Capture Tool"
- ADR: [ADR-0385](../../adr/0385-host-side-traffic-capture.md)
- Status: Design accepted

## Problem

For network-stack kernel bugs, in-guest `tcpdump` perturbs the stack under test and dies with
the guest on a panic — the packets around the failure are the ones lost. Capturing on the host
side of the guest's virtual NIC observes without touching guest state and survives the panic.
The issue asks for a `control.capture_traffic` tool that produces a host-side pcap and slots it
into the existing artifact model.

The default provider (local-libvirt) has **no host tap device**: its only guest NIC is a QEMU
SLIRP user-mode netdev (`-netdev user,id=kdivessh,...`), so `tcpdump -i <iface>` has nothing to
attach to. QEMU's built-in `filter-dump` netfilter object dumps a netdev's packets to a libpcap
file regardless of netdev type and is host-side, so it captures the SLIRP traffic no host tap
exposes.

## Requirement (restated)

Add a single fixed-duration MCP tool that captures host-side packets from a running
local-libvirt guest into a pcap, stored and egressed through the existing artifact model,
without perturbing the guest and surviving a guest panic. The agent controls the capture window,
a size cap, the per-packet snaplen (default 128 bytes), and an optional BPF capture filter.

## Tool surface

One contributor-gated MCP tool, Run-addressed (mirroring `vmcore.fetch`, ADR-0244):

`control.capture_traffic(run_id, duration_s=30, max_bytes=67108864, snaplen=128,
capture_filter=None, idempotency_key=None)`

| param | meaning | bound | default |
|-------|---------|-------|---------|
| `run_id` | investigation Run the pcap is evidence for | — | required |
| `duration_s` | capture window (seconds) | 1–300 | 30 |
| `max_bytes` | file-size cap that stops the capture early | 1 MiB – 512 MiB | 64 MiB |
| `snaplen` | per-packet bytes captured (`filter-dump maxlen`) | 1–262144 | 128 |
| `capture_filter` | optional pcap-filter(7)/tcpdump BPF expression | ≤ 1024 chars, printable | none (capture all) |
| `idempotency_key` | shared `keyed_mutation` idempotency | — | none |

All numeric bounds in `Field`/docstring text are f-string-interpolated from the enforcing
constants (the `test_tool_docs` numeric-bounds guard). The wrapper docstring and `Field`
descriptions carry no `ADR-NNNN` references (the `test_no_adr_leak` guard).

Returns the standard job envelope `{object_id: run_id, status: running, refs:{}}` with
`suggested_next_actions` steering to `jobs.wait`; on completion `refs.result` is the pcap
artifact id (the only success channel `ToolResponse.from_job` carries — like `vmcore.fetch`),
which the agent passes to `artifacts.fetch_raw(run_id, asset="pcap", artifact_id=<id>)` (see
Egress — a Run has many pcaps, so egress is capture-addressable, not `(run_id, asset)`-keyed).
The empty-capture signal rides `fetch_raw`'s existing `data.size_bytes` (see zero-packet handling
below), not the job envelope.

## Behavior contract

- **Preconditions.** The Run exists and is in the caller's projects; caller has `contributor`
  on the Run's project; the Run is bound to a System; the System is `READY` and local-libvirt;
  the bound provider advertises `supports_traffic_capture`. Failing each precondition returns a
  typed envelope and creates no job:
  - unknown/foreign Run → `config_error` / `not_found`.
  - unbound Run → `config_error` `{reason: run_unbound}`.
  - System not `READY` → `config_error` `{current_status: <state>}`.
  - non-local provider → `capability_unsupported` (`capability="traffic_capture"`).
  - malformed `run_id` → `invalid_uuid`.
  - `capture_filter` failing the admission hygiene check (too long / non-printable) →
    `config_error` `{reason: invalid_filter}`.
- **Admission.** Enqueues `JobKind.CAPTURE_TRAFFIC` with `CaptureTrafficPayload(run_id,
  duration_s, max_bytes, snaplen, capture_filter)` under `keyed_mutation`; contributor-cancelable
  (in `CONTRIBUTOR_CANCELABLE_JOB_KINDS`). Admission does **not** run a subprocess — the server
  stays non-blocking; authoritative filter validation happens in the worker.
- **Worker capture.** Under a per-System advisory lock, re-verify `READY`+local and resolve the
  `TrafficCapturer` port. The worker creates `/var/lib/kdive/pcap/<system_id>/` (mirroring the
  console-log/host-dump path owner/label handling so QEMU `svirt_t` can write there). Lock-free,
  **the handler owns the poll loop** (the port is thin primitives — see Provider seam): `attach`
  (`object-del` any stale `kdive-dump-<job_id>` filter — **tolerating not-found**, since the
  first-ever capture has none — then `object-add` a `filter-dump` with that QOM id on the
  `SYSTEM_SSH_NETDEV_ID` (`kdivessh`) netdev writing to `<job_id>.pcap` with `maxlen=snaplen`). Then poll every `POLL_INTERVAL`, stopping when `duration_s` elapses, the file
  reaches `max_bytes` (`truncated=True`), or a direct async read of the owning job row returns
  `CANCELED`. The size read is `os.stat(dest_path).st_size` — visible cross-uid even where the
  ADR-0223 content-read wall blocks the later whole-file read, and the cancel read is a plain
  async `SELECT state` on the handler's autocommit dispatch connection (not a sync callback
  threaded into a `to_thread` capture loop). `detach` (`object-del`) runs on **every** exit path
  (success, error, cancel). The result carries `bytes_captured` (the stat size) and `packets` (a
  small pure-Python pcap record walk).
- **Zero-packet capture.** A window with no matching packets yields a valid header-only pcap
  (the 24-byte libpcap global header, no records). This is the *common* case: the System NIC
  defaults to `restrict=on` (`guest_egress` False), so only the agent's SSH forward rides
  `kdivessh`. It is a **success**, not a failure. The agent detects it without a full download
  from `artifacts.fetch_raw`'s `data.size_bytes` (== 24 ⇒ zero packets), and the tool docstring
  steers a zero-packet result toward enabling `guest_egress`, broadening `capture_filter`, or
  driving traffic. A `capture_filter` that matches nothing is the same 24-byte success. The
  provider result's `packets`/`bytes_captured` feed the worker's per-kind job telemetry
  (observability), not the agent envelope (`from_job` carries no `data.*` success channel).
- **Worker filter + store.** Read the raw pcap off host disk (a `PermissionError` under
  qemu:///system → `CONFIGURATION_ERROR` with `WORKER_READABILITY_REMEDIATION`, ADR-0223). If
  `capture_filter` is set: validate with `tcpdump -d <expr>` (compile-only; failure →
  `CONFIGURATION_ERROR` `{reason: invalid_filter}` carrying tcpdump's stderr), then
  `tcpdump -r <raw> -w <out> <expr>` (single argv, no shell). Store the resulting pcap named
  `pcap-<job_id>` (job-unique + retry-stable), as `SENSITIVE`, `retention_class="pcap"`,
  `owner_kind="runs"`, `owner_id=run_id`. The pcap is bounded by `max_bytes` and already read whole
  for the readback-wall check and packet count, so it is stored with `put_artifact` (in-memory), not
  a disk-backed stream. The store runs
  under a second per-System-locked transaction (mirroring `diagnostic_sysrq._store_capture`) that
  re-checks the job is not `CANCELED` and skips the store if it is; otherwise it inserts the
  artifact row insert-if-absent on the object key (at-least-once safe), audits
  (`tool="control.capture_traffic"`, `transition="capture_traffic"`), deletes the host files, and
  returns the artifact id.
- **Egress.** A Run has **many** pcaps (one per capture), so egress is capture-addressable, not
  `(run_id, asset)`-keyed: `artifacts.fetch_raw` gains an optional `artifact_id` (used only for
  `asset="pcap"`). The agent passes the id from the job's `refs.result`; the `_resolve_key` PCAP
  branch resolves that exact row requiring `owner_kind='runs'`, `owner_id=run_id` (cross-Run id →
  `not_found`), `retention_class='pcap'`. With `artifact_id` omitted it returns the newest pcap
  (`ORDER BY created_at DESC, id DESC LIMIT 1`); earlier captures stay reachable by id via the
  `jobs.list`/`jobs.get` trail. Presigned URL, `contributor` over the Run's project.
  `artifacts.get`/`find` return `not_found` for the `SENSITIVE` pcap (unchanged `REDACTED`-only
  gate).
- **Cancellation / early exit.** `jobs.cancel` is a cooperative DB state flip (no signal to the
  handler), so the poll reads the job row's state each `POLL_INTERVAL` and, on `CANCELED`, breaks,
  `detach`es the filter, deletes the partial host file, and returns **without** storing. The final
  store transaction re-checks `CANCELED` under the lock, so a cancel observed any time before that
  commit stores nothing. This per-interval cancel-check is a new mechanism (neither
  `watch_for_crash` nor `diagnostic_sysrq` has it), added because a stray `filter-dump` fills host
  disk. `CAPTURE_TRAFFIC` is in `CONTRIBUTOR_CANCELABLE_JOB_KINDS`. Residual (documented, benign):
  a cancel that commits in the narrow window *after* the store transaction commits but *before*
  `queue.complete` still ends the job `CANCELED` with no `result_ref`, yet the pcap exists and is
  reachable as the Run's newest pcap — consistent with the "stored pcaps persist" non-goal.
- **Worker-crash orphans (no new reaper).** The `filter-dump` stays attached for the whole window,
  so a worker `SIGKILL`/host crash between `attach` and `detach` strands it. This is contained
  without a new reconciler port: (1) the deterministic `kdive-dump-<job_id>` id makes the
  at-least-once **retry**'s `attach` (`object-del`-before-`object-add`) clean the stranded filter
  and never double-attach — the normal recovery; (2) System teardown `rmtree`s the per-System
  `/var/lib/kdive/pcap/<system_id>/` directory (a new host-fs step under the existing best-effort
  teardown `try/except`; the domain is already destroyed by then, and the worker owns the dir so
  `unlink` of the QEMU-written files succeeds), sweeping orphaned host pcap files; (3) the filter
  dies when the domain stops. The one residual — a `SIGKILL` on the *final* attempt with no retry — is bounded:
  the filter captures only low-volume SSH-forward traffic on the default `restrict=on` NIC and is
  freed at the next domain stop. A dedicated `qemuMonitorCommand` reconciler reaper is a named
  follow-up, not warranted at priority:low (see ADR-0385 rejected alternatives).

## Provider seam

- New port `TrafficCapturer` (`providers/ports/`) — thin primitives, so the handler owns the
  loop and cancel semantics (like `Controller`): `attach(domain_name, *, qom_id,
  dest_path, snaplen) -> None` (`object-del`-then-`object-add` of the `filter-dump`) and
  `detach(domain_name, *, qom_id) -> None` (`object-del`). The captured netdev is
  `SYSTEM_SSH_NETDEV_ID`, a local-libvirt-internal XML detail chosen inside the capturer, so it is
  not a port parameter (it must not cross the provider boundary into the handler). No
  `capture()`/`cancelled` callback —
  the handler does the size `os.stat` and the async `CANCELED` read itself, avoiding a
  sync-callback-across-`to_thread` boundary. Keyed on the provider domain name, DB-free.
- `ProviderRuntime.traffic_capturer: TrafficCapturer | None = None` and a static
  `ProviderSupport.supports_traffic_capture: bool = False` (ADR-0378 `supports_snapshots`
  pattern), **surfaced on `systems.get`** (`mcp/tools/lifecycle/systems/view.py`, exactly where
  `supports_snapshots` is) so an agent discovers it before calling. Local-libvirt sets both;
  remote-libvirt leaves them fail-closed.
- `SYSTEM_SSH_NETDEV_ID = "kdivessh"` is extracted to a shared constant in
  `providers/local_libvirt/lifecycle/xml.py` (today a bare literal written twice in
  `_append_ssh_forward`) and imported by both `xml.py` and the capture impl + its test, so a
  rename cannot silently detach the capture from a non-existent netdev.
- Local impl `LocalLibvirtTrafficCapture` (`providers/local_libvirt/lifecycle/`): narrow
  `_LibvirtConn`/`_LibvirtDomain` Protocols wrapping `libvirt_qemu.qemuMonitorCommand`,
  `object-del`-then-`object-add` of `filter-dump` in `attach`, `object-del` in `detach`. Both
  offloaded via `asyncio.to_thread`. `attach`'s leading `object-del` swallows the QMP
  `DeviceNotFound`/"object not found" error (matched on the QMP error class/message string —
  `qemuMonitorCommand` yields no distinct `VIR_ERR_*` code, unlike the typed
  `control._idempotent`/`snapshot._delete_if_exists` swallows); other monitor failures raise
  `CONTROL_FAILURE`. Unit-tested with a fake connection; the real `libvirt_qemu` adapter is
  `live_vm`-only.
- No new reconciler port: worker-crash orphan containment is idempotent re-attach + System
  teardown directory removal (see Behavior contract). System teardown
  (`jobs/handlers/systems.py`) gains a **new host-filesystem** step that `shutil.rmtree`s the
  per-System `/var/lib/kdive/pcap/<system_id>/` tree (ignore-missing) under the **existing
  best-effort `try/except`** that already wraps `_reclaim_console_artifacts`/`_reclaim_sysrq_artifacts`,
  so a filesystem fault is logged-and-continued and never blocks the System reaching
  `TORN_DOWN`. This is a new operation type — those `_reclaim_*` functions touch only the
  object store + `artifacts` rows, not the host FS — not a mirror of them.

## Cross-cutting integration

- `JobKind.CAPTURE_TRAFFIC` appended (`domain/operations/jobs.py`; Postgres enum member — the
  `jobs_kind_check` constraint widened by a migration) and added to
  `CONTRIBUTOR_CANCELABLE_JOB_KINDS`.
- `CaptureTrafficPayload(RunPayload)` (run-addressed, like `CaptureVmcorePayload`) in
  `jobs/payloads.py` carrying `duration_s`/`max_bytes`/`snaplen`/`capture_filter`, registered in
  `_ACTIVE_PAYLOAD_MODELS`.
- Handler `capture_traffic_handler` + `register_handlers` in `jobs/handlers/control/`, wired via
  a `_capture_traffic_handler_registrar` appended to `jobs/assembly.py`
  `build_handler_registrars`.
- Tool wrapper + admission handler added inside the existing `control.register`
  (`mcp/tools/lifecycle/control/registrar.py`) — no new tool-registration tuple entry.
- `mcp/exposure.py` `_TOOL_SCOPES`: `"control.capture_traffic": _CONTRIBUTOR`.
- `mcp/tools/lifecycle/systems/view.py`: add `data["supports_traffic_capture"]` from
  `runtime.support` to the `systems.get` envelope (mirrors `supports_snapshots`).
- `jobs/handlers/systems.py`: teardown `rmtree`s `/var/lib/kdive/pcap/<system_id>/` (ignore-missing)
  under the existing best-effort teardown `try/except` — a new host-fs reclaim, not an object-store
  `_reclaim_*` mirror.
- `RawAsset.PCAP` + an optional `artifact_id` param on `artifacts.fetch_raw`
  (`mcp/tools/catalog/artifacts/raw_fetch.py`; used only for `asset="pcap"`, ignored for the
  singleton `vmcore`/`vmlinux`) + a `_resolve_key` PCAP branch calling a new
  `raw_pcap_key(conn, run_id, artifact_id)` (`artifacts/read_model.py`): resolve the exact
  run-owned pcap by id (validating `owner_kind`/`owner_id`/`retention_class`), or the newest for
  the Run when `artifact_id` is omitted. Do **not** inherit `raw_vmcore_key`'s single-object
  `fetchone()`-with-no-order assumption.
- `_BEHAVIOR_TESTS_BY_TOOL["control.capture_traffic"]` → the new behavior test file
  (`tests/mcp/core/test_tool_docs.py`).
- Migration `0072_capture_traffic_job_kind.sql` (next after `0071_system_snapshots.sql`; one
  file per job-kind add, per the `0069_watch_for_crash_job_kind.sql` precedent) widens
  `jobs_kind_check` for `capture_traffic`. No new table — the pcap is a plain `artifacts` row.
- Regenerate: `just rbac-matrix`, `just docs`, `just resources-docs` (and their `-check`
  variants gate CI).

## Test strategy

- **Provider unit** — fake libvirt connection asserts the exact `attach` QMP JSON (`object-del`
  of a stale filter, then `object-add` qom-type `filter-dump`, `netdev=SYSTEM_SSH_NETDEV_ID`, QOM
  id `kdive-dump-<job_id>`, `maxlen=snaplen`, `file=dest_path`) and that `detach` issues
  `object-del` for the QOM id. A **first-attach-with-no-stale-filter** vector (the leading
  `object-del` raises QMP not-found) asserts `attach` swallows it and still issues `object-add`;
  a non-not-found monitor error raises `CONTROL_FAILURE`.
- **Capture-loop unit** — the handler-owned poll loop with injected `stat`/sleeper/`read_state`
  (no libvirt), covering: stop-at-duration, stop-at-max_bytes (`truncated=True`), stop-at-cancel
  (no store), and `detach` invoked on every path (success, error, cancel).
- **Packet-count unit** — the pure pcap record walk reads the 4-byte magic to pick byte order and
  the µs-vs-ns record format, then walks `incl_len` record headers. Vectors: header-only
  (`packets=0`), a known N-packet little-endian file, a **big-endian** (`0xd4c3b2a1`) file, a
  **nanosecond-magic** (`0xa1b23c4d`) file, and a truncated/garbage tail (counts whole records
  only). A wrong count would silently corrupt the zero-packet signal, so both byte orders are
  pinned.
- **Filter validation unit** — `tcpdump -d` accept/reject and argv-not-shell construction; a
  filter containing shell metacharacters is passed literally and rejected by `tcpdump -d` (never
  interpreted).
- **Worker handler** — READY+local snapshot; store-as-SENSITIVE `owner_kind='runs'`
  `retention_class='pcap'` named `pcap-<job_id>`; insert-if-absent idempotency on retry (same job
  id → same name → one row); a distinct job id → distinct row (no stale-row collision);
  `PermissionError` → `CONFIGURATION_ERROR` with the readability remediation; host-file cleanup on
  success, filter failure, and cancellation; a zero-packet capture completes as success (the
  stored object is the 24-byte libpcap header; the empty signal is `fetch_raw`'s `size_bytes`).
- **Admission (behavior test, `test_control_tools`-adjacent)** — each precondition rejection
  creates no job; happy path enqueues `CAPTURE_TRAFFIC` and returns the running envelope;
  `capture_filter` hygiene rejection.
- **Teardown unit** — System teardown removes the per-System `/var/lib/kdive/pcap/<system_id>/`
  tree (and no-ops when it is absent), alongside the existing console/sysrq reclaim.
- **Egress** — `fetch_raw(run_id, asset="pcap", artifact_id=<id>)` presigns the exact object for a
  `contributor`; a cross-Run `artifact_id` is `not_found`; `artifact_id` omitted returns the
  newest; two pcaps on one Run are each fetchable by id; the pcap is `not_found` via
  `artifacts.get`.
- **Registry guards** — flat-top-level-params, description/maturity, no-ADR-leak,
  destructive-set (tool is **not** destructive), rbac-matrix drift.
- **Live proof (`live_vm`)** — on a READY local-libvirt guest, run a capture over a window with
  SSH-forward traffic, fetch the pcap by its `refs.result` id, and assert it is a valid libpcap
  file with `packets>0`. Validates the SELinux/label + qemu:///system readback path end to end.
  (The zero-packet path is covered by the worker-handler unit test, not the live proof.)

## Non-goals (this change)

- Remote-libvirt implementation (fail-closed `capability_unsupported`; documented follow-up:
  auto-generated netdev id discovery, remote→worker pcap transport, optional real
  `tcpdump -i vnetN` on the `vnet` tap).
- A `REDACTED` text/flow-digest sibling (pcap is fetched whole as a binary, like `vmlinux`).
- A TTL GC sweep for `retention_class="pcap"` (matches `vmcore`; broader retention concern). A
  **stored** pcap is reclaimed by no teardown/sweep today (System teardown touches only
  `owner_kind='systems'`; the GC sweeps only run-owned `build`/`kernel-build`), so — like
  `vmcore` — stored pcaps persist until an object-store lifecycle policy or manual cleanup, and a
  Run accumulates one **per capture**. Wiring pcap into the closed-investigation reclaim
  (`gc_investigation_artifacts`, which already has the `owner_kind='runs'` + retention-class
  mechanism) is a named follow-up. This non-goal is about *stored evidence* only — the *live*
  filter/host-file orphaned by a worker crash is contained by idempotent re-attach + teardown
  directory removal (see Behavior contract), not a reconciler reaper.
- A free-form tcpdump command line (the agent controls `snaplen` + BPF `capture_filter` only).
- On-the-wire filtering (impossible with `filter-dump`; the BPF filter is a post-capture trim).
- A start/stop capture pair (the operator selected the fixed-duration job).

## Risks

- **SELinux + directory creation** — the worker creates `/var/lib/kdive/pcap/<system_id>/`; QEMU
  (`svirt_t`) must be able to create/write the pcap there, the same class of host-config issue as
  the staged-rootfs path and console log. Surfaced by the live proof; a denial is an
  operator-remediation `CONFIGURATION_ERROR`, not a silent failure.
- **Disk-full mid-capture** — if the host disk fills before `duration_s`/`max_bytes`, `filter-dump`
  write failures are silent to the size-poll (the file simply stops growing, indistinguishable
  from an idle link). The capture ends as a short/empty success (detectable via `fetch_raw`'s
  `size_bytes`, the zero-packet path); `detach` still `object-del`s the filter. Detecting
  `ENOSPC` distinctly is a possible refinement, not required for correctness.
- **qemu:///system cross-uid readback** — the raw pcap is QEMU-owned; a non-root worker cannot
  read it. Reuses the ADR-0223 `WORKER_READABILITY_REMEDIATION` contract unchanged.
- **`qemuMonitorCommand` is libvirt "unsupported"** — a QEMU/libvirt version whose `filter-dump`
  QOM schema changes breaks object-add; the op fails `CONTROL_FAILURE`, visible in per-kind job
  telemetry. Precedent in-tree: `remote_libvirt/connection/transport_reset.py`.
