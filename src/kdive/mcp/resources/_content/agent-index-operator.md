# Operating a KDIVE platform

Start here with a platform role when maintaining shared KDIVE capacity, operational state, or
cross-project evidence. These guides describe purpose and workflow; read each tool's current
schema for parameters, confirmation requirements, and response fields. A listed tool is not
permission to use it on every object.

Platform roles are separate from project roles. `platform_operator`, `platform_admin`, and
`platform_auditor` have different grants; the direct tool response names the required role when a
request is refused. Destructive actions require their documented confirmation fields and should
follow the returned recovery guidance.

## Operator toolsets

| Toolset | Use it for | Guide |
| --- | --- | --- |
| accounting | Set project budget and quota limits; inspect cost and usage context | resource://kdive/docs/guide/toolsets/accounting.md |
| audit | Query accountable operation history | resource://kdive/docs/guide/toolsets/audit.md |
| inventory | Inspect reconciled inventory and clear an authorized override | resource://kdive/docs/guide/toolsets/inventory.md |
| ops | Reconcile, diagnose, tune, and recover platform operations | resource://kdive/docs/guide/toolsets/ops.md |
| reports | Produce bounded accounting reports, including authorized all-project reports | resource://kdive/docs/guide/toolsets/reports.md |
| secrets | Inspect configured secret references without retrieving secret material | resource://kdive/docs/guide/toolsets/secrets.md |
| shapes | Maintain shared capacity shapes and cost classes | resource://kdive/docs/guide/toolsets/shapes.md |

Use the investigation workflow index for a project's run, system, artifact, debug, and crash
workflow. This operator index is listed and readable only to callers holding a platform role.
