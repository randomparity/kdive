# vmcore toolset

Capture a crashed guest's vmcore when kernel state must be analyzed after the crash. The capture is
asynchronous and complements redacted console evidence; it is not a substitute for confirming the
System state and selecting the right Run.

- `vmcore.fetch` takes the **Run ID** whose bound System is **CRASHED**; do not pass a System or
  artifact ID. It returns a job handle: wait with `jobs.wait` until terminal, then pass the same
  Run ID to `postmortem.crash` or `introspect.from_vmcore` for analysis.

Before capture, confirm the target is crashed with `systems.get` and retain the console evidence
needed to explain the crash. For crash(8) analysis follow the `postmortem` guide; for typed drgn
analysis of the captured core follow the `introspect` guide. This guide supplies purpose and order;
the `vmcore.fetch` schema owns parameters, permissions, and response fields.
