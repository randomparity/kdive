# Debugging a kernel race

Use this workflow to run a reproducer while observing a live kernel and collecting console
signals. For provisioning, build/upload, SSH access, and cleanup, start with
resource://kdive/docs/guide/agent-index.md.

## Choose an observation method

A GDB breakpoint stops execution and can change the interleaving you are investigating.
KDIVE has no non-stop gdbstub mode. Use GDB when stopping and stepping helps answer the
question; use live introspection or tracing when the workload must keep running.

- **Sample kernel state:** use `introspect.run` for the built-in `tasks`, `modules`, or
  `sysinfo` helper, or `introspect.script` for a targeted drgn read. First open a session
  with `debug.start_session(transport="drgn-live")`. Check the guest and kernel
  prerequisites in resource://kdive/docs/guide/toolsets/introspect.md. Local-libvirt uses
  SSH; remote-libvirt uses the guest agent. Neither provides an atomic snapshot of a
  running kernel: state may change between reads.
- **Record events:** configure the guest's tracing tools over your SSH connection. Select
  the events or functions needed for the hypothesis and collect the trace around the
  reproducer. Tracing adds overhead and can change timing; see the
  [Linux ftrace documentation](https://docs.kernel.org/trace/ftrace.html) for filters and
  collection controls appropriate to your kernel.

Non-halting observation is not a guarantee that the failure's timing is preserved. Compare
runs with and without the chosen observation, and keep the observation narrow enough to
interpret its effect.

## Run the reproducer with a console watch

1. **Prepare the guest.** Complete the kernel install/boot and SSH-authorization jobs.
   Stage the reproducer and any tracing tools, allowing guest disk space for their output.
   For access and package-egress requirements, read
   resource://kdive/docs/guide/toolsets/systems.md.
2. **Submit the watch before the workload.** `control.watch_for_crash` requires contributor
   access, a READY System, and provider crash-watch support. Supply the owning `run_id`
   while an active external boot restricts the System. Choose `deadline_s` for one batch;
   its unit is seconds measured by the worker's monotonic clock after pickup. The tool
   clamps the requested duration to its advertised maximum. At expiry it returns
   `not_fired` if no signature matched; submit another watch for a later batch.
3. **Run and observe concurrently.** Drive the reproducer over SSH while tracing or sampling
   through the drgn-live session. Poll the watch with `jobs.wait` while the workload runs.
   Do not wait for the watch to finish before starting the reproducer. Its console baseline
   is taken at worker pickup, so submission alone does not establish coverage: queue delay
   can leave an early failure outside the watched window.
4. **Read the evidence.** After the job succeeds, parse its `refs.result` JSON. `fired`
   means a recognized console signature appeared; inspect `signature`, `matched`, and
   `elapsed_s`. Signatures include non-halting diagnostics, so a match does not by itself
   establish a fatal crash. `not_fired` means no match in the watched window. Neither
   verdict, nor an SSH disconnect, establishes the guest's current state. Use `runs.get`
   and the artifact tools to read persisted console evidence, especially if SSH drops.
5. **Capture or repeat.** The watch does not mark the System CRASHED. Check `systems.get`
   before `vmcore.fetch`, which requires that state; wait for capture success before
   postmortem analysis. If evidence and state disagree, preserve the evidence and involve
   the operator. Only one watch is queued or running per System. For another live attempt,
   let the old watch finish, then submit a new request with a fresh idempotency key; reusing
   the old key replays its response. Retain needed traces before changing the guest.

A console watch observes output independently of guest SSH. Live drgn and guest tracing
still need their guest execution channel. If the guest hangs, those observations may stop;
use the control guide to choose diagnostics or recovery and check the operation's gates.
`control.force_crash` deliberately changes the evidence: use it only when a forced capture
is intended, not to make a spontaneous failure count as a crash.

End a live introspection session with `debug.end_session` when finished. Capture and triage
are covered by resource://kdive/docs/guide/toolsets/postmortem.md; watch, diagnostic, and
power operations by resource://kdive/docs/guide/toolsets/control.md.

The documentation-based race workflow is recorded in
[ADR-0366](../adr/0366-race-debugging-out-of-band.md).
