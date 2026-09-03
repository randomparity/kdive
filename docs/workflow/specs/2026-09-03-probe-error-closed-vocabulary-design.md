# Readiness probe failures leave the probe as a closed vocabulary

Issue: [#2220](https://github.com/randomparity/kdive/issues/2220).

## Goal

Stop the local-libvirt readiness probe minting free-form `virsh` transport text into
`ReadinessResult.probe_error`. The probe classifies its own failure into a closed vocabulary; the
bounded raw text goes to the operator log and never crosses the function boundary. No agent-facing
renderer changes, because after this there is nothing for one to filter.

## The leak, verified

Reproduced against the branch base (`811538fb2`, x86_64) by driving the real `_domain_exit_probe`
with a faked `subprocess.run` result whose stderr is the ordinary "cannot reach the session daemon"
text, then the real `_await_ready` aggregation, then the real `ToolResponse.failure_from_error`:

```text
rendered MCP payload = {'system_id': '2222…', 'probe_error':
  "error: failed to connect to the hypervisor\nerror: failed to connect socket to
   '/run/user/1000/libvirt/virtqemud-sock': no such file or directory"}
```

The transport socket path and the host UID reach the agent verbatim. `_bounded_probe_error`
(`readiness.py:58-59`) is `message[:200]`, which bounds length and nothing else, and
`safe_error_details` (`serialization.py:96-120`) is a *type* filter — it reduces values to JSON
scalars and drops non-scalars, so a `str` passes through unchanged.

### There are two agent-facing egresses, not one

#2220 records the MCP renderer and reads the CLI and job-worker paths as covered because they
"run details through `Redactor`". Measured on the same base, they are not covered:

```text
Redactor.redact_text(<the stderr above>) -> unchanged, byte for byte
_failure_context(...)  -> {'failure_detail_probe_error': "…'/run/user/1000/libvirt/virtqemud-sock'…"}
```

`Redactor` is a secrets filter — URL userinfo, secret-named keys, registered secret values. A host
path is not a secret by its rules, so it passes untouched. And `failure_context` is agent-facing:
`jobs/handlers/connectivity/ssh_authorize.py:179-180` records `_failure_context` projecting into
`failure_detail_*` on `jobs.wait`, and the boot step runs as a worker job
(`jobs/handlers/runs/boot.py:157`). So the same string reaches an agent twice:

1. `ToolResponse.failure_from_error` → `safe_error_details` → `data.probe_error`;
2. `jobs/worker.py::_failure_context` → `jobs.wait` → `failure_detail_probe_error`.

This is what settles where the fix goes. A filter in the boundary renderer closes egress 1 and
leaves egress 2 open, because the worker path has a renderer of its own.

`data.boot_readiness` is **not** a third egress: `BootAttempt.as_data()`
(`services/runs/steps.py:286-292`) is a fixed-key payload of `job_id`, `status`, and
`error_category`, and carries no details.

## Architecture

Two seams change, one direction each. Nothing outside the local-libvirt boot path is touched.

1. **`lifecycle/boot/readiness.py`** gains `ProbeFailure`, a `StrEnum` of the four failures
   `_domain_exit_probe` can actually distinguish. Every failing return classifies into a member.
   The bounded raw text is written to the module logger at that point and is not returned.
   `_DomainExitProbe.error` and `ReadinessResult.probe_error` become `ProbeFailure | None`.
2. **`lifecycle/install.py`** renders the member's `.value` into `details["probe_error"]`.

The type change is the enforcement. Under `ty`'s strict defaults a free-form `str` can no longer be
assigned to `probe_error` at any call site, in `src/` or in `tests/`, so the leak stops being
representable rather than being filtered after the fact. That is the property a key denylist at a
renderer cannot offer.

### The vocabulary

Four members, one per branch `_domain_exit_probe` already distinguishes. Values are snake_case,
matching the reason-token convention the codebase already uses for bounded scalar details
(`external_boot_restricted`, `no_active_activation`, `system_job_active`).

| Member | Value | Raised when |
|---|---|---|
| `VIRSH_MISSING` | `virsh_missing` | `shutil.which` finds no `virsh`, or the exec raises `FileNotFoundError` |
| `VIRSH_TIMEOUT` | `virsh_timeout` | `subprocess.TimeoutExpired` |
| `VIRSH_PROBE_FAILED` | `virsh_probe_failed` | `subprocess.SubprocessError` or `OSError` |
| `VIRSH_NONZERO_EXIT` | `virsh_nonzero_exit` | the process ran and exited nonzero |

`VIRSH_MISSING` and `VIRSH_TIMEOUT` already carried no host-derived text, so their agent-facing
meaning is unchanged in substance. `VIRSH_PROBE_FAILED` and `VIRSH_NONZERO_EXIT` are the two that
carried the leak.

The vocabulary is closed on purpose: it distinguishes exactly the states an agent can act on
differently. `virsh_missing` is a host provisioning fault an operator must fix; `virsh_timeout` and
`virsh_nonzero_exit` are transport conditions a retry may clear; `virsh_probe_failed` is the
residual. A fifth member would need a fifth distinguishable action.

### Where the raw text goes

`_domain_exit_probe` logs the bounded text at `WARNING` on the `readiness` module logger, with the
domain name, at the point of classification. This is the return-valued analogue of what this
module's siblings already do on the raise path: `_install_failure` and `_libvirt_transport_failure`
(`install.py:132-136`, `:149-151`) emit a static message plus `details={"domain": domain_name}`,
drop the libvirt exception text from the error entirely, and let the chained traceback carry it
into the operator log. `probe_error` is the one site in this file that departs from that
convention; this restores it.

The operator therefore keeps the full text. It moves from the agent-facing payload to the log,
which is the sink `__main__.py`'s printer already names for details ("stderr and the scraped log
commonly land in the systemd journal or a CI log"). The probe runs in the worker, whose logs are
host-side.

**Log volume is a real consequence, stated here rather than discovered later.** At the default
`KDIVE_LIBVIRT_BOOT_WINDOW_S` of 900 s over a 5 s cadence the boot loop polls 180 times, so a
libvirt daemon that is down for the whole window produces up to 180 `WARNING` lines for one boot.
That is proportionate — the transport is genuinely down for fifteen minutes, and every line names
the same domain — and it is filterable by logger name. The alternative, first-failure-only
logging, needs per-boot state inside a function that holds none, and would hide a probe failure
whose *classification changes* mid-window, which is the case an operator most wants to see.

## Data flow

```text
virsh stderr / OSError
   │
   ├─ bounded to 200 chars ──> _log.warning(...)          [operator: host log only]
   │
   └─ classified ───────────> ProbeFailure member
                                 │
                                 ├─> ReadinessResult.probe_error
                                 │      └─> _await_ready keeps the first
                                 │             └─> details["probe_error"] = member.value
                                 │                    ├─> safe_error_details -> data.probe_error
                                 │                    └─> _failure_context  -> failure_detail_probe_error
                                 └─ (raw text is unreachable from here)
```

## Error handling

`_domain_exit_probe` returns values and raises nothing, so this change introduces no re-raise and
#2220's `raise … from None` criterion has no site to apply to. That is a property to preserve, not
a gap: were the classification ever moved onto a raise path, `from exc` would re-attach the raw
text through the chained traceback and reproduce the defect. The spec records the constraint so a
later change cannot reintroduce it silently.

Classification never fails. Every branch of `_domain_exit_probe` that returns
`_DomainExitProbe(False, …)` returns a member, so `probe_error` is `None` only where the probe did
not fail.

## Threat model

**Boundary inventory.** This change adds no boundary and widens none. It narrows two existing ones,
both of the same kind: provider-minted error text crossing from the host-trust side to the
agent-trust side. Egress 1 is `ToolResponse.failure_from_error`; egress 2 is the worker's
`_failure_context` surfaced on `jobs.wait`.

**Actor model.** The untrusted party is the MCP-connected agent and anything downstream of it —
transcripts, logs the agent writes, issue reports it files. It is authenticated and project-scoped;
it is not assumed discreet with what it is handed. The operator running `python -m kdive`, and the
worker process, are on the host-trust side and already have host access, so host paths in their
logs disclose nothing they cannot read directly. That is where the design places its trust, and it
is why the log is an acceptable destination for text the payload may not carry.

**Control per boundary.** For both egresses the control is the same and sits upstream of both: the
value is a member of a closed vocabulary fixed at compile time, so no host-derived substring can
occupy it. On failure it leaks the vocabulary itself — four tokens naming which probe condition
occurred — which is the disclosure the feature exists to make. The controls at the boundaries are
unchanged: `safe_error_details` stays a type filter, `Redactor` stays a secrets filter, and neither
is asked to become a content filter.

**Explicitly out of scope.** Other `details` keys on other categories, which may carry host-derived
text by the same mechanism — this change fixes `probe_error` and does not audit the corpus.
`_libvirt_transport_failure`'s `from exc` chaining, which puts libvirt text in the traceback but
not in `details` or the message, so it does not reach either egress. `Redactor`'s inability to
strip host paths, which is a stated property of a secrets filter and not a defect of it. The
remote-libvirt provider's own readiness module, which is separate code with its own probe.

## Testing

The deciding test asserts **absence**, not difference, per #2220: a transform that returned a
different leaky string must fail it.

1. **End-to-end absence, MCP egress.** Drive the real `_domain_exit_probe` with a faked
   `subprocess.run` returning a nonzero exit whose stderr carries a transport socket path, through
   the real `_boot_failure_details` and the real `ToolResponse.failure_from_error`. Assert each
   transport substring — the full socket path, the `/run/user` prefix, the socket basename — is
   absent from the rendered payload, and that `probe_error` equals `virsh_nonzero_exit`.
2. **End-to-end absence, worker egress.** The same probe result through the real
   `_failure_context`, asserting the same substrings are absent from
   `failure_detail_probe_error`. This is the egress a boundary-only fix would have missed, so it
   gets its own test rather than being folded into the first.
3. **`OSError` absence.** The `OSError` arm renders `.filename` and `.strerror`; a probe raising
   `OSError(2, "No such file or directory", "/run/user/1000/libvirt/virtqemud-sock")` must
   classify to `virsh_probe_failed` with the filename absent from the rendered payload.
4. **Classification coverage.** One test per member, asserting the branch maps to the member.
5. **The raw text still reaches the operator.** `caplog` at `WARNING` over the probe module
   asserts the bounded text is logged, so the AC2 relocation is proven rather than asserted.

Every new test is bite-proved: committed first, then a controlled fault injected, a clean
assertion failure observed, the fault reverted, and byte identity re-verified by `sha256sum`.

Three existing tests assert the old free-text values and change with the contract they pin —
`test_install.py:1141`, `:1686`, `:1892`. That is intended breakage, and each keeps asserting the
same *behaviour* (a probe failure is carried to the boot-failure details) against the new
vocabulary.

## Out of scope

Carried unchanged from the issue: `safe_error_details`' general contract, the `Redactor`
implementation, and the remote-libvirt provider.
