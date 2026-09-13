# vmcore toolset

Capture a crashed guest's vmcore when kernel state must be analyzed after the crash. The capture is
asynchronous and complements redacted console evidence; it is not a substitute for confirming the
System state and selecting the right Run.

- `vmcore.fetch` requests vmcore capture for a crashed System or Run and returns a job handle.
  Wait with `jobs.wait` until the capture reaches a terminal result, then use the returned
  references to select postmortem or introspection work.

Before capture, confirm the target is crashed with `systems.get` and retain the console evidence
needed to explain the crash. For crash(8) analysis follow the `postmortem` guide; for typed drgn
analysis of the captured core follow the `introspect` guide. This guide supplies purpose and order;
the `vmcore.fetch` schema owns parameters, permissions, and response fields.
