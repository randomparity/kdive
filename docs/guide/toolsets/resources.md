# resources toolset

Resources describe the capacity providers make available to KDIVE. Investigation callers normally
read this toolset before requesting an Allocation; platform roles manage its registration and
health. Read each tool's schema for authorization and exact state values.

## Discover capacity

- `resources.list` enumerates registered capacity and its current availability.
- `resources.describe` reads the detailed capabilities and supported profiles for one resource.
- `resources.availability` reports whether a resource can satisfy a prospective request; use it to
  understand a shortage before changing an Allocation request.

## Platform management

- `resources.register` adds a provider resource to the platform.
- `resources.renew` renews a registered resource's platform lease.
- `resources.deregister` removes a resource that is no longer offered.
- `resources.set_status` records operational health without changing its scheduling policy.
- `resources.set_scheduling` changes whether new allocations may be placed there.
- `resources.drain` stops new placement and coordinates existing allocations before maintenance.

Platform-management operations do not replace the investigation workflow: use `allocations.request`
after selecting capacity. This guide names the tools by purpose; consult each tool schema for
parameters, role requirements, and returned state.
