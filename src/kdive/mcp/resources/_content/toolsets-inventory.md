# inventory toolset

Inventory reflects the reconciled provider declaration and any authorized runtime override. Use it
to understand what the platform is configured to offer before changing a resource or investigating
why one is absent. Read the tool schema for filters and response fields.

- `inventory.list` reads configured inventory for a `platform_auditor` or stronger authorized
  caller; it is the starting point for comparing declared and observed capacity.
- `inventory.clear_override` removes a recorded inventory override after the underlying
  configuration is corrected. It requires `platform_admin` and does not edit the source
  declaration itself.

Configuration changes and resource lifecycle actions have separate controls. Do not clear an
override merely to hide a discrepancy; first establish which declaration or provider condition is
authoritative.
