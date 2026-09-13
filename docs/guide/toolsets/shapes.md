# shapes toolset

Shapes define reusable capacity and cost-class settings used when planning allocations. Use the
operator tools to maintain shared definitions after confirming their effect on future requests;
read each schema for fields and authorization.

- `shapes.set` creates or updates a shared shape or its cost-class configuration for a
  `platform_operator`.
- `shapes.delete` removes an unused shared shape for a `platform_operator`; confirm that active
  users no longer depend on it before deletion.

`shapes.list` is the all-audience discovery tool used by investigation planning. This guide covers
the operator maintenance actions, not a substitute for checking capacity availability.
