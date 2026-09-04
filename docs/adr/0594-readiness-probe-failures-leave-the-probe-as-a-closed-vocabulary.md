# 0594 — Readiness probe failures leave the probe as a closed vocabulary

## Status

Accepted (2026-09-03)

## Context

`CategorizedError.details` is surfaceable to the agent — `lifecycle/rootfs/upload_staging.py:665`
says so in as many words. The local-libvirt readiness probe puts free-form `virsh` text in it:
`_domain_exit_probe` forwards subprocess stderr on a nonzero exit and renders `str(exc)` for a
`SubprocessError`/`OSError`, whose `.filename` and `.strerror` name a host path.
`_bounded_probe_error` truncates to 200 characters and does nothing else.

Two renderers carry `details` to an agent, and both are type filters rather than content filters:

- `safe_error_details` (`serialization.py:96-120`) reduces values to JSON scalars and drops
  non-scalars, so a `str` passes verbatim to `data.probe_error`;
- the worker's `_failure_context` (`jobs/worker.py:768-777`) is persisted as the job's
  `failure_context` (`worker.py:527`) and merged into the agent-facing envelope by
  `ToolResponse.from_job` — `data.update(job.failure_context)` on a `FAILED` job
  (`mcp/responses.py:321`) — which is what `jobs.get`/`jobs.wait` return. The boot step runs as a
  worker job (`jobs/handlers/runs/boot.py:63`).

Measured at `811538fb2` on x86_64 with the project venv, driving the real probe with the ordinary
daemon-unreachable stderr: the MCP payload renders the transport socket path verbatim, and
`Redactor.redact_text` returns that same stderr byte for byte — it filters secrets (URL userinfo,
secret-named keys, registered values), and a host path is not a secret by its rules. So both
egresses leak, and the second was not identified when #2220 was filed.
`BootAttempt.as_data()` (`services/runs/steps.py:286-292`) is fixed-key, so `data.boot_readiness`
is not a third. `probe_error`'s only production reader is `install.py:249-250`;
`external_boot.py:1218-1219` compares the whole `ReadinessResult` and discards the field.

`probe_error` also has real value: `virsh domstate timed out after 2s` tells an operator something
actionable. What this record settles is not whether to keep the signal but where to constrain it.

## Decision

The probe classifies its own failure. `readiness.py` gains `ProbeFailure`, a `StrEnum` of
`virsh_missing`, `virsh_timeout`, `virsh_probe_failed`, and `virsh_nonzero_exit` — one per branch
`_domain_exit_probe` already distinguishes. `_DomainExitProbe.error` and
`ReadinessResult.probe_error` are typed `ProbeFailure | None`, and `install.py` renders `.value`
into `details["probe_error"]`.

The bounded raw text is logged at `WARNING` where it is classified and is not returned. That is the
return-valued form of what this module's siblings already do on the raise path: `_install_failure`
and `_libvirt_transport_failure` (`install.py:132-136`, `:149-152`) emit a static message plus
`details={"domain": domain_name}` and leave the libvirt text to the operator's log. `probe_error`
was the one site in the file that departed from the convention.

The enforcement is the type, not a filter. Under `ty`'s strict whole-tree check a free-form `str`
cannot be assigned to `probe_error` at any call site in `src/` or `tests/`, so the leak stops being
representable. No renderer changes: `safe_error_details` stays a type filter and `Redactor` stays a
secrets filter, because after this there is nothing at either boundary to filter.

`VIRSH_MISSING` is deliberately wider than its name suggests. Python maps errno to an `OSError`
subclass at construction, so `except FileNotFoundError` precedes the `OSError` arm and absorbs
**every** ENOENT raised by the exec — including an ENOENT on a socket path, a transport fault
rather than a missing binary. That arm keeps its agent-facing meaning and gains `str(exc)` as its
*logged* detail, so the operator keeps the errno and path. The arms are not reordered: that is real
control-flow change in a change meant to shrink what crosses a boundary, and the pre-fix behaviour
is identical.

## Consequences

- Both egresses close with one change, and a third would close with it too. A boundary filter
  would have had to be written once per renderer.
- The four values are an agent-facing compatibility surface. Renaming one changes what an agent
  reads; a new probe branch needs a member or a deliberate reuse of one. That is the property this
  record exists to hold immutable.
- `virsh_missing` does not mean only "the binary is absent". An agent acting on it as a host
  provisioning fault will be right for `shutil.which` returning `None` and wrong for an ENOENT
  transport fault. The distinction survives only in the log.
- The diagnostic crosses a process boundary rather than moving within one. The boot step runs as a
  worker job (`jobs/handlers/runs/boot.py:63` is the only production caller), so the raw text lands
  in the **worker's** log, not the caller's. An operator reading `jobs.wait` sees the token and
  recovers the detail from the worker log, joining on the domain name the `WARNING` carries.
- `kdivectl` operators lose the raw text, and that is the intended reduction rather than a cost.
  `flatten_envelope` (`cli/render.py:50-64`) lifts every envelope `data` key into the operator's
  row, so `kdivectl` shows `probe_error` — but it is an MCP client (`pyproject.toml:58`) and
  remote-capable, which puts it on the client side of the boundary this record defends. Recovering
  the raw text requires host access to the worker log, and an operator without host access is
  precisely the actor the payload must not carry a host path to.
- Log volume, worst case: the poll count is `_boot_window_polls` scaled by
  `tcg_deadline_multiplier(accel)` (`install.py:209`), 1.0 only for `accel == "kvm"` and otherwise
  `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER`, default 10.0 — so up to 180 `WARNING` lines on KVM and
  1800 on the TCG tier or an unknown accelerator, at the same 12 lines a minute over a window that
  stretches from 15 minutes to 150. First-failure-only logging was not taken: it needs per-boot
  state in a function that holds none, and hides a failure whose classification changes mid-window.
- The probe returns rather than raises, so no `raise … from None` site exists. Moving
  classification onto a raise path later would re-attach the raw text through the chained
  traceback, so the return-valued shape is now load-bearing.
- Other `details` keys on other categories may leak by the same mechanism. This record fixes
  `probe_error` and does not audit the corpus.

## Considered & rejected

- **Drop an operator-only detail key at the MCP boundary renderer, keeping the raw text in
  `details` for the CLI and worker.** verified: this is the shape #2220 proposes, and it closes one
  of two egresses. Measured at `811538fb2`, `_failure_context` renders
  `failure_detail_probe_error` with the socket path intact and it is persisted to the job row and
  read back on `jobs.wait`, so the worker path keeps leaking. It is also a denylist: a future
  provider adding a leaky detail key defaults to leaking.
- **Run `Redactor` on the MCP path as well.** verified: at `811538fb2`,
  `Redactor(registry=SecretRegistry()).redact_text(<the daemon-unreachable stderr>)` compares equal
  to its input. It filters secrets, not host paths, so it does not fix the defect; #2220 also puts
  its implementation out of scope.
- **Scrub host paths in a producer-side redacting wrapper.** verified: #2211 built exactly this and
  withdrew it at commit `c79129460` — "it raised a host path itself, producing the defect it was
  added to remove" — and that work was deleted with this surface reassigned here.
- **Keep `probe_error` a `str` and return fixed author-controlled literals.** judgment: closes both
  egresses with the same five rewrites and the same log call, and skips retyping
  `_DomainExitProbe.error` and `ReadinessResult.probe_error` — which is what pulls
  `test_install.py:185` and `test_external_boot.py:2666` into the change. Rejected because a later
  edit can put free text back with nothing failing: the type is what makes the leak
  unrepresentable rather than merely absent today.
- **Make `safe_error_details` a content filter.** judgment: it is the shared reduction for every
  error payload in the codebase, #2220 puts its general contract out of scope, and a content filter
  over arbitrary free text is a far larger promise than any call site here needs.
- **Drop `probe_error` entirely and log everything.** judgment: cheaper, but it costs the agent the
  one distinction it acts on — a missing `virsh` needs an operator, a timeout may clear on retry —
  and #2220 exists to keep boot failures diagnosable.
- **Reorder the `except` arms so an ENOENT transport fault classifies separately from a missing
  binary.** verified: `OSError(2, …)` constructs a `FileNotFoundError` and `OSError(13, …)` a
  `PermissionError`, so the arms are separable in principle. judgment: it changes control flow in a
  change whose whole purpose is to shrink what crosses a boundary, the pre-fix behaviour is
  identical, and no caller distinguishes the two today. Recorded as a consequence instead.
- **Do nothing.** judgment: `CategorizedError.details` is agent-facing by design, and error text is
  a common route by which host identifiers reach transcripts and issue reports.
