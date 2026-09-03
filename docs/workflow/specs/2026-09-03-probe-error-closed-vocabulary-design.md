# Readiness probe failures leave the probe as a closed vocabulary

Issue: [#2220](https://github.com/randomparity/kdive/issues/2220).

> **No ADR number was available for this change.** The decision below has viable alternatives —
> one of them was built and withdrawn on #2211 — so by the repository's own convention it belongs
> in `docs/adr/`. ADR numbers are allocated by the run coordinating the parallel work on this
> repository today, three requests went unanswered, and minting one unilaterally is the collision
> that convention exists to prevent. So *Decision* and *Considered and rejected* below carry the
> record in full, and promoting them to a numbered ADR is outstanding work for whoever holds the
> allocation. Nothing in `src/` cites an ADR for this change, so no status guard is implicated.

## Goal

Stop the local-libvirt readiness probe minting free-form `virsh` transport text into
`ReadinessResult.probe_error`. The probe classifies its own failure into a closed vocabulary; the
bounded raw text goes to the operator log and never crosses the function boundary. No agent-facing
renderer changes, because afterwards there is nothing for one to filter.

## The leak, verified

Reproduced at base `811538fb2` (x86_64) by driving the real `_domain_exit_probe` with a faked
`subprocess.run` result whose stderr is the ordinary "cannot reach the session daemon" text, then
the real `_await_ready` aggregation, then the real `ToolResponse.failure_from_error`:

```text
rendered MCP payload = {'system_id': '2222…', 'probe_error':
  "error: failed to connect to the hypervisor\nerror: failed to connect socket to
   '/run/user/1000/libvirt/virtqemud-sock': no such file or directory"}
```

The transport socket path and the host UID reach the agent verbatim. `_bounded_probe_error`
(`readiness.py:58-59`) is `message[:200]` — length only. `safe_error_details`
(`serialization.py:96-120`) is a *type* filter: it reduces values to JSON scalars and drops
non-scalars, so a `str` passes through unchanged.

### Two agent-facing egresses, not one

#2220 records the MCP renderer and reads the CLI and job-worker paths as covered because they
"run details through `Redactor`". Measured on the same base, they are not:

```text
Redactor.redact_text(<the stderr above>) -> unchanged, byte for byte
_failure_context(...) -> {'failure_detail_probe_error': "…'/run/user/1000/libvirt/virtqemud-sock'…"}
```

`Redactor` is a secrets filter — URL userinfo, secret-named keys, registered secret values. A host
path is not a secret by its rules. And `failure_context` is agent-facing:
`jobs/handlers/connectivity/ssh_authorize.py:179-180` records `_failure_context` projecting into
`failure_detail_*` on `jobs.wait`, and the boot step runs as a worker job
(`jobs/handlers/runs/boot.py:157`). So the same string reaches an agent twice — through
`safe_error_details` as `data.probe_error`, and through `_failure_context` as
`failure_detail_probe_error`.

This settles where the fix goes. A filter in the boundary renderer closes the first and leaves the
second open, because the worker path has a renderer of its own.

`data.boot_readiness` is **not** a third egress: `BootAttempt.as_data()`
(`services/runs/steps.py:286-292`) is a fixed-key payload of `job_id`, `status`, and
`error_category`, carrying no details.

## Decision

The probe classifies its own failure. `readiness.py` gains `ProbeFailure`, a `StrEnum` of the four
branches `_domain_exit_probe` already distinguishes; `_DomainExitProbe.error` and
`ReadinessResult.probe_error` are typed `ProbeFailure | None`; `install.py` renders `.value` into
`details["probe_error"]`. The bounded raw text is logged at `WARNING` where it is classified and is
not returned.

The enforcement is the type, not a filter. No renderer changes — after this there is nothing at
either boundary to filter.

### Considered and rejected

- **Drop an operator-only detail key at the MCP boundary renderer, keeping the raw text in
  `details` for the CLI and worker.** verified: this is the shape #2220 proposes, and it closes one
  of two egresses. Measured at `811538fb2`, `_failure_context` renders
  `failure_detail_probe_error` with the socket path intact and reaches an agent through
  `jobs.wait`, so the worker path keeps leaking. It is also a denylist: a future provider adding a
  leaky detail key defaults to leaking.
- **Run `Redactor` on the MCP path as well.** verified: at `811538fb2`,
  `Redactor(registry=SecretRegistry()).redact_text` returns the daemon-unreachable stderr
  unchanged, byte for byte. It filters secrets, not host paths, so it does not fix the defect;
  #2220 also puts its implementation out of scope.
- **Scrub host paths in a producer-side redacting wrapper.** verified: #2211 built exactly this and
  withdrew it — the wrapper itself raised a host path, reproducing the defect it was added to
  prevent, and that work was deleted with this surface reassigned here.
- **Make `safe_error_details` a content filter.** judgment: it is the shared reduction for every
  error payload in the codebase, #2220 puts its general contract out of scope, and a content filter
  over arbitrary free text is a far larger promise than any call site here needs.
- **Drop `probe_error` entirely and log everything.** judgment: cheaper, but it costs the agent the
  one distinction it acts on — a missing `virsh` needs an operator, a timeout may clear on retry —
  and #2220 exists to keep boot failures diagnosable.
- **Do nothing.** judgment: `CategorizedError.details` is agent-facing by design
  (`lifecycle/rootfs/upload_staging.py:665` says so in as many words), and error text is a common
  route by which host identifiers reach transcripts and issue reports.

### Consequences

- Both egresses close with one change, and a third would close with it too. A boundary filter would
  have had to be written once per renderer.
- The four values are an agent-facing compatibility surface. Renaming one changes what an agent
  reads; a new probe branch needs a member or a deliberate reuse of one.
- The operator's diagnostic moves from the CLI `details` line to the host log. It is not lost, and
  worker and CLI are both host-trust, but an operator reading only stderr now sees a token and must
  look at the journal for the transport text.
- The probe returns rather than raises, so the return-valued shape is now load-bearing: moving
  classification onto a raise path later would re-attach the raw text through the chained
  traceback.
- Other `details` keys on other categories may leak by the same mechanism. This fixes
  `probe_error` and does not audit the corpus.

## Architecture

Two seams change, one direction each; nothing outside the local-libvirt boot path is touched.
`ty` runs whole-tree under strict defaults, so once `probe_error` is typed `ProbeFailure | None` a
free-form `str` cannot be assigned to it at any call site in `src/` **or** `tests/`. The leak stops
being representable rather than being filtered after the fact — the property a key denylist at a
renderer cannot offer.

### The vocabulary

Values are snake_case, matching the bounded-reason-token convention already in the codebase
(`external_boot_restricted`, `no_active_activation`, `system_job_active`).

| Member | Value | Raised when |
|---|---|---|
| `VIRSH_MISSING` | `virsh_missing` | `shutil.which` finds no `virsh`, or the exec raises `FileNotFoundError` |
| `VIRSH_TIMEOUT` | `virsh_timeout` | `subprocess.TimeoutExpired` |
| `VIRSH_PROBE_FAILED` | `virsh_probe_failed` | `subprocess.SubprocessError` or `OSError` |
| `VIRSH_NONZERO_EXIT` | `virsh_nonzero_exit` | the process ran and exited nonzero |

The first two already carried no host-derived text; the last two carried the leak. The vocabulary
is closed on purpose and distinguishes exactly the states an agent can act on differently:
`virsh_missing` is a host provisioning fault an operator must fix, `virsh_timeout` and
`virsh_nonzero_exit` are transport conditions a retry may clear, `virsh_probe_failed` is the
residual. A fifth member would need a fifth distinguishable action.

### Where the raw text goes

`_domain_exit_probe` logs the bounded text at `WARNING` with the domain name, at the point of
classification. This is the return-valued analogue of what the module's siblings already do on the
raise path: `_install_failure` and `_libvirt_transport_failure` (`install.py:132-136`, `:149-151`)
emit a static message plus `details={"domain": domain_name}`, drop the libvirt exception text from
the error entirely, and leave it to the operator's log. `probe_error` is the one site in this file
that departs from that convention; this restores it.

The operator keeps the full text; it moves from the agent-facing payload to the log, the sink
`__main__.py`'s printer already names for details. The probe runs in the worker, whose logs are
host-side.

**Log volume is a real consequence.** At the default `KDIVE_LIBVIRT_BOOT_WINDOW_S` of 900 s over a
5 s cadence the loop polls 180 times, so a daemon down for the whole window yields up to 180
`WARNING` lines for one boot. That is proportionate to a transport that is genuinely down for
fifteen minutes, and it is filterable by logger name. First-failure-only logging would need
per-boot state in a function that holds none, and would hide a probe failure whose classification
*changes* mid-window — the case an operator most wants to see.

## Data flow

```text
virsh stderr / OSError
  ├─ bounded to 200 chars ──> _log.warning(...)        [operator: host log only]
  └─ classified ──> ProbeFailure ──> ReadinessResult.probe_error
                                      └─> _await_ready keeps the first
                                            └─> details["probe_error"] = member.value
                                                  ├─> safe_error_details -> data.probe_error
                                                  └─> _failure_context  -> failure_detail_probe_error
```

The raw text is unreachable from the classified branch.

## Error handling

`_domain_exit_probe` returns values and raises nothing, so this change introduces no re-raise and
#2220's `raise … from None` criterion has no site to apply to. That is a property to preserve, not
a gap: were classification ever moved onto a raise path, `from exc` would re-attach the raw text
through the chained traceback and reproduce the defect.

Classification never fails — every branch returning `_DomainExitProbe(False, …)` returns a member,
so `probe_error` is `None` only where the probe did not fail.

## Threat model

**Boundary inventory.** No boundary is added and none widened. Two existing ones are narrowed, both
the same kind: provider-minted error text crossing from host-trust to agent-trust —
`ToolResponse.failure_from_error`, and the worker's `_failure_context` on `jobs.wait`.

**Actor model.** The untrusted party is the MCP-connected agent and everything downstream of it:
transcripts, logs it writes, issues it files. It is authenticated and project-scoped, and not
assumed discreet with what it is handed. The operator running `python -m kdive`, and the worker
process, are host-trust and already have host access, so host paths in their logs disclose nothing
they cannot read directly. That is where the design places its trust, and it is why the log is an
acceptable destination for text the payload may not carry.

**Control per boundary.** One control, upstream of both: the value is a member of a vocabulary
fixed at compile time, so no host-derived substring can occupy it. On failure it discloses the
vocabulary itself — four tokens naming the probe condition — which is the disclosure the feature
exists to make. The boundary controls are unchanged: `safe_error_details` stays a type filter,
`Redactor` stays a secrets filter, and neither is asked to become a content filter.

**Out of scope, stated rather than left silent.** Other `details` keys on other categories, which
may carry host-derived text by the same mechanism — this fixes `probe_error` and does not audit the
corpus. `_libvirt_transport_failure`'s `from exc` chaining, which puts libvirt text in the
traceback but not in `details` or the message, so it reaches neither egress. `Redactor`'s
inability to strip host paths, which is a property of a secrets filter rather than a defect of one.
The remote-libvirt provider's own readiness module, which is separate code with its own probe.

## Testing

The deciding tests assert **absence** of the transport substrings from the rendered payload, not
inequality against the raw stderr, so a transform returning a *different* leaky string fails them.
Both egresses get their own absence test — the worker one is what a boundary-only fix would have
missed. The `OSError` arm is covered separately because it leaks through `str(exc)` rather than
through stderr, every vocabulary member is reached, and a `caplog` assertion proves the raw text
still reaches the operator. Every new test is bite-proved: committed, faulted, observed failing
cleanly, reverted, and byte-verified by `sha256sum`. The plan holds the tests themselves.

Three existing tests pin the old free-text values and change with the contract —
`test_install.py:1141`, `:1686`, `:1892`. Intended breakage; each keeps asserting the same
behaviour against the new vocabulary.

## Out of scope

Carried unchanged from the issue: `safe_error_details`' general contract, the `Redactor`
implementation, and the remote-libvirt provider.
