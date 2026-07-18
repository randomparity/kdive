# Spec: Host-side network traffic capture (#1258)

- Issue: #1258 "Add Network Traffic Capture Tool"
- ADR: [ADR-0384](../../adr/0384-host-side-traffic-capture.md)
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
artifact id, which the agent passes to `artifacts.fetch_raw(run_id, asset="pcap",
artifact_id=<id>)` (see Egress — a Run has many pcaps, so egress is capture-addressable, not
`(run_id, asset)`-keyed). The completion also surfaces `data.packets` and `data.bytes_captured`
(see zero-packet handling below).

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
  console-log/host-dump path owner/label handling so QEMU `svirt_t` can write and the worker can
  read). Lock-free: `object-del` any stale filter for `kdive-dump-<job_id>` (idempotent
  re-attach after an at-least-once retry), then `object-add` a `filter-dump` with QOM id
  `kdive-dump-<job_id>` on the `SYSTEM_SSH_NETDEV_ID` (`kdivessh`) netdev writing to
  `<job_id>.pcap` with `maxlen=snaplen`. Poll the file size every `POLL_INTERVAL`, stopping when
  `duration_s` elapses, the file reaches `max_bytes` (`truncated=True`), or the owning job row is
  `CANCELED`. `object-del` the filter on **every** exit path (success, error, cancel). The result
  carries `bytes_captured` (host file size — the poll already reads it) and `packets` (a small
  pure-Python pcap record walk).
- **Zero-packet capture.** A window with no matching packets yields a valid header-only pcap.
  This is the *common* case: the System NIC defaults to `restrict=on` (`guest_egress` False), so
  only the agent's SSH forward rides `kdivessh`. It is a **success**, not a failure — the
  envelope surfaces `data.packets=0` and steers the agent toward enabling `guest_egress`,
  broadening `capture_filter`, or driving traffic. A `capture_filter` that matches nothing is the
  same success with `packets=0`.
- **Worker filter + store.** Read the raw pcap off host disk (a `PermissionError` under
  qemu:///system → `CONFIGURATION_ERROR` with `WORKER_READABILITY_REMEDIATION`, ADR-0223). If
  `capture_filter` is set: validate with `tcpdump -d <expr>` (compile-only; failure →
  `CONFIGURATION_ERROR` `{reason: invalid_filter}` carrying tcpdump's stderr), then
  `tcpdump -r <raw> -w <out> <expr>` (single argv, no shell). Stream the resulting pcap to the
  object store via `put_stream` named `pcap-<job_id>` (job-unique + retry-stable), as
  `SENSITIVE`, `retention_class="pcap"`, `owner_kind="runs"`, `owner_id=run_id`. Insert the
  artifact row insert-if-absent on the object key (at-least-once safe), audit
  (`tool="control.capture_traffic"`, `transition="capture_traffic"`), delete the host files,
  return the artifact id.
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
  handler), so the size-poll reads the job row's state each `POLL_INTERVAL` and, on `CANCELED`,
  breaks, `object-del`s the filter, deletes the partial host file, and returns **without**
  storing an object or row (job ends `CANCELED`, no `result_ref`). This per-interval cancel-check
  is a new mechanism (neither `watch_for_crash` nor `diagnostic_sysrq` has it), added because a
  stray `filter-dump` fills host disk. `CAPTURE_TRAFFIC` is in `CONTRIBUTOR_CANCELABLE_JOB_KINDS`.
- **Worker-crash orphans + reaper.** The `filter-dump` stays attached for the whole window, so a
  worker `SIGKILL`/host crash between `object-add` and `object-del` strands it writing unbounded
  to host disk and leaves a partial file. A reconciler reap (modeled on
  `reap_orphaned_dump_volumes` + `has_active_capture_job`) `object-del`s kdive `kdive-dump-*`
  filters whose owning capture job is terminal and deletes orphaned
  `/var/lib/kdive/pcap/<system_id>/*.pcap` files with no live job. The deterministic
  `kdive-dump-<job_id>` id makes both the retry re-attach and the reap idempotent.

## Provider seam

- New port `TrafficCapturer` (`providers/ports/`): `capture(domain_name, *, qom_id, netdev_id,
  duration_s, max_bytes, snaplen, dest_path, cancelled) -> TrafficCaptureResult`, where
  `cancelled: Callable[[], bool]` is the per-interval cooperative cancel probe the handler
  supplies (reads the job state) and `qom_id`/`netdev_id` are passed in (not hardcoded in the
  port). The result carries `bytes_captured`, `packets`, `truncated` (max_bytes early-stop), and
  `cancelled` (stopped by cancel). Keyed on the provider domain name, DB-free — the handler drives
  the state machine and cancel semantics, exactly like `Controller`.
- `ProviderRuntime.traffic_capturer: TrafficCapturer | None = None` and a static
  `ProviderSupport.supports_traffic_capture: bool = False` (ADR-0378 `supports_snapshots`
  pattern). Local-libvirt sets both; remote-libvirt leaves them fail-closed.
- `SYSTEM_SSH_NETDEV_ID = "kdivessh"` is extracted to a shared constant in
  `providers/local_libvirt/lifecycle/xml.py` (today it is a bare literal written twice in
  `_append_ssh_forward`) and imported by both `xml.py` and the capture impl + its test, so a
  rename cannot silently detach the capture from a non-existent netdev.
- Local impl `LocalLibvirtTrafficCapture` (`providers/local_libvirt/lifecycle/`): narrow
  `_LibvirtConn`/`_LibvirtDomain` Protocols, `qemuMonitorCommand` object-del-then-add of
  `filter-dump` (idempotent re-attach), bounded size-poll honoring the `cancelled` probe,
  `object-del` on every exit. Unit-tested with a fake connection; the real `libvirt_qemu` adapter
  is `live_vm`-only.
- Reconciler reap (`reconciler/cleanup/gc.py`, modeled on `reap_orphaned_dump_volumes`):
  `object-del` `kdive-dump-*` filters whose owning capture job is terminal, and delete orphaned
  `/var/lib/kdive/pcap/<system_id>/*.pcap` files with no live capture job
  (`has_active_capture_job`).

## Cross-cutting integration

- `JobKind.CAPTURE_TRAFFIC` appended (`domain/operations/jobs.py`; Postgres enum member — the
  `jobs_kind_check` constraint widened by a migration) and added to
  `CONTRIBUTOR_CANCELABLE_JOB_KINDS`.
- `CaptureTrafficPayload(SystemPayload-shaped over run_id)` in `jobs/payloads.py`, registered in
  `_ACTIVE_PAYLOAD_MODELS`.
- Handler `capture_traffic_handler` + `register_handlers` in `jobs/handlers/control/`, wired via
  a `_capture_traffic_handler_registrar` appended to `jobs/assembly.py`
  `build_handler_registrars`.
- Tool wrapper + admission handler added inside the existing `control.register`
  (`mcp/tools/lifecycle/control/registrar.py`) — no new tool-registration tuple entry.
- `mcp/exposure.py` `_TOOL_SCOPES`: `"control.capture_traffic": _CONTRIBUTOR`.
- `RawAsset.PCAP` + an optional `artifact_id` param on `artifacts.fetch_raw`
  (`mcp/tools/catalog/artifacts/raw_fetch.py`; used only for `asset="pcap"`, ignored for the
  singleton `vmcore`/`vmlinux`) + a `_resolve_key` PCAP branch calling a new
  `raw_pcap_key(conn, run_id, artifact_id)` (`artifacts/read_model.py`): resolve the exact
  run-owned pcap by id (validating `owner_kind`/`owner_id`/`retention_class`), or the newest for
  the Run when `artifact_id` is omitted. Do **not** inherit `raw_vmcore_key`'s single-object
  `fetchone()`-with-no-order assumption.
- `_BEHAVIOR_TESTS_BY_TOOL["control.capture_traffic"]` → the new behavior test file
  (`tests/mcp/core/test_tool_docs.py`).
- Migration: one numbered migration widening `jobs_kind_check` for `capture_traffic` (no new
  table — the pcap is a plain `artifacts` row).
- Regenerate: `just rbac-matrix`, `just docs`, `just resources-docs` (and their `-check`
  variants gate CI).

## Test strategy

- **Provider unit** — fake libvirt connection asserts the exact `object-add`/`object-del` QMP
  JSON (qom-type `filter-dump`, `netdev=SYSTEM_SSH_NETDEV_ID`, QOM id `kdive-dump-<job_id>`,
  `maxlen=snaplen`, `file=dest_path`), the object-del-then-add idempotent re-attach, the
  size-poll early-stop at `max_bytes`, and `object-del` on the success, error, **and cancel**
  paths.
- **Capture-loop unit** — a pure poll function (injected size-reader/sleeper/`cancelled`, no
  libvirt), covering: stop-at-duration, stop-at-max_bytes (`truncated=True`), and
  stop-at-cancel (`cancelled=True`, no store).
- **Packet-count unit** — the pure pcap record walk over a header-only file (`packets=0`), a
  known N-packet file, and a truncated/garbage tail (counts whole records only).
- **Filter validation unit** — `tcpdump -d` accept/reject and argv-not-shell construction; a
  filter containing shell metacharacters is passed literally and rejected by `tcpdump -d` (never
  interpreted).
- **Worker handler** — READY+local snapshot; store-as-SENSITIVE `owner_kind='runs'`
  `retention_class='pcap'` named `pcap-<job_id>`; insert-if-absent idempotency on retry (same job
  id → same name → one row); a distinct job id → distinct row (no stale-row collision);
  `PermissionError` → `CONFIGURATION_ERROR` with the readability remediation; host-file cleanup on
  success, filter failure, and cancellation; a zero-packet capture completes as success with
  `data.packets=0`.
- **Admission (behavior test, `test_control_tools`-adjacent)** — each precondition rejection
  creates no job; happy path enqueues `CAPTURE_TRAFFIC` and returns the running envelope;
  `capture_filter` hygiene rejection.
- **Reaper unit** — a terminal capture job with a still-attached `kdive-dump-*` filter is
  `object-del`ed; an orphaned `<system_id>/*.pcap` with no live job is deleted; a file with a
  live job is left untouched.
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
  filter/host-file orphaned by a worker crash **is** reaped (see Behavior contract).
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
  from an idle link). The capture ends as a short/empty success surfaced via `data.bytes_captured`
  /`data.packets` (the zero-packet path); the reaper still `object-del`s the filter. Detecting
  `ENOSPC` distinctly is a possible refinement, not required for correctness.
- **qemu:///system cross-uid readback** — the raw pcap is QEMU-owned; a non-root worker cannot
  read it. Reuses the ADR-0223 `WORKER_READABILITY_REMEDIATION` contract unchanged.
- **`qemuMonitorCommand` is libvirt "unsupported"** — a QEMU/libvirt version whose `filter-dump`
  QOM schema changes breaks object-add; the op fails `CONTROL_FAILURE`, visible in per-kind job
  telemetry. Precedent in-tree: `remote_libvirt/connection/transport_reset.py`.
