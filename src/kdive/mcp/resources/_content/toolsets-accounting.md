# accounting toolset

Accounting connects a project's capacity request to its quota, budget, and recorded consumption.
Use it before changing limits or explaining an admission refusal. The tool schema defines fields,
authorization, and response data.

- `accounting.estimate` estimates the KCU cost of a proposed shape and lease window before a
  request consumes capacity.
- `accounting.usage` reads the project's current budget, quota, and usage figures.
- `accounting.report` produces either a project-scoped report for a granted project or an
  all-project report when the caller holds `platform_auditor`.
- `accounting.set_budget` changes a project's budget ceiling. It requires that project's admin
  role; set a limit only after confirming the intended project and operational impact.
- `accounting.set_quota` changes a project's concurrent-capacity quota. It also requires the
  project's admin role and does not itself grant capacity.

For a tenant's investigation workflow, release allocations when work is complete; changing a
budget or quota is administration, not cleanup.
