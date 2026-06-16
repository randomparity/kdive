# ADR 0125 — Diagnostics host-reachability probe

- **Status:** Proposed
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers

## Context

`ops.diagnostics` (ADR-0091) is meant to answer "is this provider healthy?" but the default
service factory (`src/kdive/diagnostics/service.py:204-235`) wires only the server-vantage
`secret_ref` check. The `ProviderTlsCheck`/`GdbstubAclCheck` checks exist
(`src/kdive/diagnostics/checks.py:260-362`) but were deferred to an "egress-probe wave" and are
not wired in, and there is no probe of the `qemu+tls://` libvirt connection itself. In
MCP-surface testing this left diagnostics unable to distinguish "remote-libvirt host
unreachable" from "bad profile" — it reported `secret_ref: 0 refs` and nothing about the host.

The reachability capability already exists elsewhere: `remote_connection()` +
`conn.getInfo()` (`src/kdive/providers/remote_libvirt/transport.py:54,146-181`) opens and
validates the connection, and `SshBuildHostProber`
(`src/kdive/providers/shared/build_host/reachability.py:44-93`) is the established
`asyncio.to_thread` + per-check-timeout reachability pattern. See
`../design/mcp-onboarding-error-ergonomics.md`.

## Decision

We will wire the existing `ProviderTlsCheck`/`GdbstubAclCheck` into the default diagnostics
service factory and add a remote-libvirt reachability check that opens `remote_connection()` and
calls `conn.getInfo()` under a bounded per-check timeout (reusing the `SshBuildHostProber`
offload pattern), reporting per-host `pass`/`fail`/`error` with the connection failure category
(`transport_failure` for an unreachable host, `configuration_error` for a bad URI/cert). The
check is server-side authz-gated like the other diagnostics checks (ADR-0091).

## Consequences

- `ops.diagnostics` can tell an operator whether a provisioning failure is the host or the
  config, which was the missing signal during onboarding.
- The probe performs network egress and TLS materialization from the server, so it inherits the
  diagnostics timeout/gating discipline; a hung host cannot stall the report beyond the per-check
  timeout.
- This refines ADR-0091 by closing its deferred egress-probe gap for the server-vantage checks;
  the separate ephemeral-probe-guest egress check stays out of scope.

## Alternatives considered

- **Leave reachability to provisioning failures**: the caller learns the host is down only by
  trying to provision and reading a (now better, per ADR-0123) error; rejected because diagnostics
  is the tool whose job is exactly this triage.
- **A new standalone reachability tool**: duplicates the diagnostics gating/timeout machinery;
  rejected in favor of extending `ops.diagnostics`.
- **Probe from a guest (worker-vantage)**: heavier and unnecessary for a libvirt client connection
  the server itself makes; rejected for this finding.
