# audit toolset

Audit tools answer who performed an accountable operation and when. Use them to investigate an
operational event, verify a completed intervention, or prepare an authorized review; they do not
modify the recorded history. Read the tool schema for query filters, cursors, and returned fields.

- `audit.query` searches the audit history for an authorized project-admin or platform-auditor
  caller. Scope the query as narrowly as the question allows and follow its cursor for additional
  records.

Audit output is evidence, not authorization to repeat an operation. Use the relevant operator
tool only after confirming its current role and confirmation requirements.
