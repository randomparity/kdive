# allocations toolset

Allocations reserve capacity for an investigation before it creates a System. They are independent
of provider cleanup: releasing an Allocation does not prove a System or its artifacts are gone.
Read each tool's schema for resource selectors, limits, and returned fields.

- `allocations.request` reserves matching capacity for a project. Use the returned Allocation ID
  when creating the System that will consume it.
- `allocations.wait` waits for an Allocation request to become ready or terminal. Reissue bounded
  waits while it remains pending; do not guess that a requested Allocation is usable.
- `allocations.list` shows the project's allocations and their state. Follow the response cursor
  when it is present.
- `allocations.renew` extends an Allocation's lease before its recorded deadline when the
  investigation still needs it.
- `allocations.release` gives capacity back when no System or follow-up work needs it. Teardown is
  separate: release only after accounting for the System lifecycle and evidence you still need.

For available provider capacity, inspect `resources` first. This guide supplies workflow purpose;
the individual tool schemas define parameters, authorization, and response fields.
