# introspect toolset

Use drgn to inspect typed kernel state, either in a running guest or in a captured vmcore.
Live reads do not stop the kernel, so state can change between reads; they are not an atomic
snapshot. For parameters, limits, and return fields, read each tool's current schema.

## Live introspection

Live operations require contributor access and a live **drgn-live** DebugSession. Open it
with `debug.start_session(transport="drgn-live")`; the default gdbstub session cannot be
used with these tools. Live drgn does not require the local-libvirt `debug.gdbstub` flag.

Check the execution path and guest prerequisites:

- Local-libvirt invokes `/usr/local/sbin/kdive-drgn` over SSH using the System's bootstrap
  key. Remote-libvirt invokes that helper through the guest agent. The provider must
  support the requested live operation and its guest channel must be reachable.
- The guest needs the KDIVE helper, drgn, access to its running kernel's `/proc/kcore`, and
  usable matching debug information for typed lookups. The helper explicitly loads
  `/sys/kernel/btf/vmlinux` when present, otherwise uses drgn's default debug-info search.
  Live kernel access requires `CONFIG_PROC_KCORE=y`; see the
  [drgn support matrix](https://drgn.readthedocs.io/en/stable/support_matrix.html#kernel-configuration).
- Inspect any `data.missing_debuginfo` warning from attach or introspection. Successful
  attachment alone does not prove symbols resolve. Missing helper packages, unreachable
  guest services, and unusable debug information require different remedies; inspect the
  returned error and details before retrying.

Choose the operation for the question:

- `introspect.run` runs a built-in `tasks`, `modules`, or `sysinfo` helper against the session.
- `introspect.script` runs a drgn Python script with `prog` already bound to the live kernel.
  For example, `print(prog["init_task"].pid)` prints one typed field. Each call starts a fresh
  drgn process; put related work in one script and inspect returned stdout and truncation.

End the session with `debug.end_session`. For a workload running alongside observations and
console capture, read resource://kdive/docs/operating/race-debugging.md.

## Offline introspection

`introspect.from_vmcore` takes a **Run ID** and requires viewer access. It reads that Run's
captured core and matching recorded build/debug artifacts through the provider's offline
drgn support; it does not need a live guest. Complete `vmcore.fetch` successfully first.
See resource://kdive/docs/guide/toolsets/postmortem.md for capture and triage.
