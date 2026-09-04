# Readiness probe failures leave the probe as a closed vocabulary

Issue: [#2220](https://github.com/randomparity/kdive/issues/2220).
Decision record: [ADR-0594](../../adr/0594-readiness-probe-failures-leave-the-probe-as-a-closed-vocabulary.md).

## Goal

Stop the local-libvirt readiness probe minting free-form `virsh` transport text into
`ReadinessResult.probe_error`. The probe classifies its own failure into a closed vocabulary; the
bounded raw text goes to the operator log and never crosses the function boundary. No agent-facing
renderer changes, because afterwards there is nothing for one to filter.

**ADR-0594 is the record**: the verified leak (both egresses, with the commands that reproduce
them), the decision, its consequences, and the eight rejected alternatives all live there and are
not repeated here. This spec covers only what the implementation must satisfy that the record does
not fix: the vocabulary contract, the threat model, and the proof obligations.

## The vocabulary contract

Values are snake_case, matching the bounded-reason-token convention already in the codebase
(`external_boot_restricted`, `no_active_activation`, `system_job_active`).

| Member | Value | Raised when |
|---|---|---|
| `VIRSH_MISSING` | `virsh_missing` | `shutil.which` finds no `virsh`, **or any ENOENT from the exec** |
| `VIRSH_TIMEOUT` | `virsh_timeout` | `subprocess.TimeoutExpired` |
| `VIRSH_PROBE_FAILED` | `virsh_probe_failed` | `subprocess.SubprocessError`, or an `OSError` that is not ENOENT |
| `VIRSH_NONZERO_EXIT` | `virsh_nonzero_exit` | the process ran and exited nonzero |

`VIRSH_MISSING`'s second case is easy to miss and changes how the tests must be written. Python
maps errno to an `OSError` subclass at construction, so `OSError(2, …)` **is** a
`FileNotFoundError`, and that arm precedes the `OSError` arm — it absorbs an ENOENT on a socket
path too. A test aiming at `VIRSH_PROBE_FAILED` through the `OSError` arm must raise a non-ENOENT
errno; `OSError(13, …)` constructs a `PermissionError`, which that arm catches. ADR-0594 records
why the arms are not reordered, and why the `FileNotFoundError` arm gains `str(exc)` as its logged
detail.

These four values are an agent-facing compatibility surface: renaming one changes what an agent
reads.

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

The raw text is unreachable from the classified branch. `ty` runs whole-tree under strict defaults,
so once `probe_error` is typed `ProbeFailure | None` a free-form `str` cannot be assigned to it at
any call site in `src/` **or** `tests/`.

`probe_error`'s only production reader is `install.py:249-250`. `external_boot.py:1218-1219`
compares the whole `ReadinessResult` against `ReadinessResult(True, True, None)`, so it discards
the field rather than reading it — the retype costs no existing consumer anything.

## Error handling

`_domain_exit_probe` returns values and raises nothing, so this change introduces no re-raise and
#2220's `raise … from None` criterion has no site to apply to. That is a property to preserve, not
a gap: were classification ever moved onto a raise path, `from exc` would re-attach the raw text
through the chained traceback.

Classification never fails — every branch returning `_DomainExitProbe(False, …)` returns a member,
so `probe_error` is `None` only where the probe did not fail.

## Threat model

**Boundary inventory.** No boundary is added and none widened. Two existing ones are narrowed, both
the same kind: provider-minted error text crossing from host-trust to agent-trust —
`ToolResponse.failure_from_error`, and the worker's `_failure_context` on `jobs.wait`.
`data.boot_readiness` is not a third: `BootAttempt.as_data()` is fixed-key.

**Actor model.** The untrusted party is the MCP-connected agent and everything downstream of it:
transcripts, logs it writes, issues it files. It is authenticated and project-scoped, and not
assumed discreet with what it is handed. The probe *runs* only in the worker — the boot step is a
worker job (`jobs/handlers/runs/boot.py:63`) — and the worker is host-trust with host access
already, so host paths in its log disclose nothing its operator cannot read directly. That is where
the design places its trust, and why the log is an acceptable destination for text the payload may
not carry. Qualified once: where `observability/facade.py` bridges the root logger, the bounded
text also reaches the operator's OTLP collector. That is still host-trust infrastructure and
outside the agent's reach either way, and `RedactingLogProcessor` is not asked to become a
host-path filter, for the reason ADR-0594 gives about `Redactor`.

**`kdivectl` is a client, not a host-trust operator, and it does lose the raw text.** Separate
*invocation* from *rendering*: no CLI invokes the probe, but `kdivectl` is an MCP client
(`pyproject.toml:58`) whose `flatten_envelope` (`cli/render.py:50-64`) lifts every envelope `data`
key into the operator's row, so it shows `probe_error` today and the token after this change. That
is a reduction; criterion 2 permits one that is stated and justified, and ADR-0594 records the
justification — a remote-capable client sits on the far side of the boundary this change defends,
so recovering the raw text should require the host access an operator on the near side already has.
`src/kdive/__main__.py`, the CLI the charter's surface names, is the host-side daemon entrypoint and
the probe path does not reach it.

**Control per boundary.** One control, upstream of both: the value is a member of a vocabulary
fixed at compile time, so no host-derived substring can occupy it. On failure it discloses the
vocabulary itself — four tokens naming the probe condition — which is the disclosure the feature
exists to make. The boundary controls are unchanged: `safe_error_details` stays a type filter,
`Redactor` stays a secrets filter, and neither is asked to become a content filter.

**Out of scope, stated rather than left silent.** The issue's three exclusions carry unchanged —
`safe_error_details`' general contract, the `Redactor` implementation, and the remote-libvirt
provider, whose readiness module is separate code with its own probe. `Redactor`'s inability to
strip host paths is a property of a secrets filter, not a defect of one. Two threats are also
excluded: other `details` keys on other categories, which may carry host-derived text by the same
mechanism (this fixes `probe_error` and does not audit the corpus); and
`_libvirt_transport_failure`'s `from exc` chaining, which puts libvirt text in the traceback but
not in `details` or the message, so it reaches neither egress.

## Proof obligations

The deciding tests assert **absence** of the transport substrings from the rendered payload, not
inequality against the raw stderr, so a transform returning a *different* leaky string fails them.
Both egresses get their own absence test — the worker one is what a boundary-only fix would have
missed. The `OSError` arm is covered separately with a non-ENOENT errno, both `VIRSH_MISSING`
branches are reached (member coverage alone hides the ENOENT-from-exec one), and a `caplog`
assertion proves the raw text still reaches the operator.

Every new test is bite-proved: committed, faulted, observed failing cleanly, reverted, and
byte-verified by `sha256sum`. Two conditions make a bite evidence rather than noise — the recorded
failure text must name the behaviour the fault changed, and every test must pass under a no-fault
control.
