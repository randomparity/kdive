# reports toolset

Reports provide a bounded, redacted summary of usage and accounting evidence. Use a project scope
for work you are already granted, or the all-project scope only when a platform-auditor review is
required. Read the tool schema for time windows, output formats, and size limits.

- `reports.generate` creates a multi-section report for the caller's granted projects, or for all
  projects when the `all-projects` branch is authorized by `platform_auditor`.

Report generation is read-oriented and does not change quotas, budgets, or resource state. Use
the accounting administration guide when a report identifies a limit that needs an authorized
change.
