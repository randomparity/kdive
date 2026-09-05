# Local external-boot readiness window and channel migration design

Issue: #2243
Decision: [ADR-0600](../../adr/0600-local-external-boot-readiness-window.md)

## Goal

Make local external-boot readiness observe only the boot started by the current session, and reject
owned legacy definitions without the guest-agent channel before the operation mutates provider
state.

## Constraints

- Preserve ADR-0576's worker-owned, truncate-before-start console inode; do not restore byte offsets.
- Use `KDIVE_LIBVIRT_BOOT_WINDOW_S` as one monotonic deadline and the existing five-second cadence.
- Keep `ProviderRuntime.external_boot` unbound until #2246.
- Do not redefine an existing System as a migration side effect.
- Return no console bytes, host paths, or raw libvirt errors to an agent.
- This change targets the declared x86_64 and ppc64le code paths, but the campaign excludes a native
  ppc64le live proof. The x86_64 host is the live-test target.

## Components and data flow

### Prepared console window

The session factory receives a narrow `prepare_console(UUID) -> None` capability. `_ConcreteSession`
uses it only after proving the domain inactive and immediately before each `domain.create()`. A
successful call is the readiness anchor: ADR-0576 has validated and truncated the one worker-owned
inode, so a whole-file read contains only bytes from the following boot.

`start()` and `restore_power("running")` both use one private prepare-and-create method. Each success
increments a session-local generation and invalidates any prior readiness result. If preparation or
create fails there is no readable generation, so `readiness()` fails closed rather than observing
the old boot.

### Bounded readiness probe

`LocalExternalBootReadiness` is a production callable built with narrow injectable primitives for
time, sleep, console read, and domain-state probing. Its call receives the System ID and executes a
complete poll loop:

1. Calculate one monotonic deadline from `KDIVE_LIBVIRT_BOOT_WINDOW_S`.
2. Read at most the configured console evidence cap plus one sentinel byte.
3. Classify the current window. Ready and crashed are terminal answers.
4. Probe domain state. A terminal domain triggers one final bounded console read, then failure.
5. Preserve the first closed-vocabulary `ProbeFailure`; an operational probe failure is observable
   but does not extend the deadline.
6. Sleep no longer than the remaining deadline. Expiry returns unanswered failure.

The session calls it only after its own successful prepare-and-create generation. A second start
must call preparation again before the probe can run, so it cannot reuse a previous anchor.

The read primitive is explicitly size-bounded. A missing initial file cannot occur after successful
preparation; disappearance, replacement, truncation that violates the window, or oversize content
fails closed. No recovery path falls back to an offset of zero on a historical file.

### Legacy channel gate

The factory already reads and validates the owned inactive XML before opening the overlay and
artifact root. At that point it additionally requires exactly one
`devices/channel/target[@name='org.qemu.guest_agent.0']` whose type is `virtio`. Absence,
duplication, or a malformed target raises terminal `CategorizedError` with category
`READINESS_FAILURE`, the System ID, and the static recovery text “reprovision this System.”

This ordering precedes artifact-directory creation, materialization, guest mutation, domain
redefinition, and power mutation. A conforming definition proceeds byte-for-byte unchanged.

## Failure contract

Preparation failures retain their existing categorized host-configuration/provisioning behavior.
Domain-start errors retain the caller's libvirt error handling. Readiness returns the existing
`ReadinessResult`; crashed and terminal domains answer `ok=False`, timeout answers
`answered=False`, and probe failures remain in `ProbeFailure`. The coordinator maps every result
other than `ReadinessResult(True, True, None)` to bounded `READINESS_FAILURE` as today.

Legacy-channel rejection is terminal `READINESS_FAILURE`. Its message identifies reprovisioning as
the recovery action. Its details contain only `system_id`.

## Security and trust boundaries

No new boundary is added. The worker reads a host-owned console path derived solely from the pinned
System ID and invokes libvirt on the already ownership-validated domain. Existing boundaries are
tightened:

- The console window is created through ADR-0576's `O_NOFOLLOW`, owner, type, and link-count checks.
- The domain definition is parsed with defused XML and checked against pinned ownership before the
  channel gate.
- Console data is capped and used only for classification; it never enters an error payload.
- Raw domain-probe diagnostics remain bounded worker logs; the result carrier exposes only the
  ADR-0594 enum.

Authenticated tenants cannot choose a path, XML document, deadline, command, or credential through
this seam. Operator-side out-of-band domain starts, log replacement, and host misconfiguration are
outside the change; each fails closed when it violates the checked identity or evidence contract.

## Verification

- A prior ready marker followed by a new panic fails because preparation clears the first marker.
- Every direct create path records `prepare`, then `create`; active starts fail before preparation.
- A second start prepares a second generation; readiness cannot run after a failed or absent start.
- Deterministic clock tests cover immediate ready, crash, terminal-domain reread, probe failure,
  deadline expiry, exact evidence limit, oversize evidence, and no sleep past the deadline.
- Owned active and inactive legacy definitions both reject before any artifact, guest, define, or
  power event; channel-present definitions open without redefine.
- Production composition binds preparation and readiness while still advertising no external-boot
  port.
- A controlled test fault removes preparation and makes the stale-marker regression fail.
- Focused tests, `just lint`, `just type`, and `just ci` pass.
