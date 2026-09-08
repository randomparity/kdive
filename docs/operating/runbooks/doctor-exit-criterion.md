# Verify deployment diagnostics

Use this procedure to check that a running deployment reports a seeded fault and a useful
remediation, then reports recovery after the fault is removed. It preserves the independent
operator proof required by [ADR-0091](../../adr/0091-doctor-diagnostics-model.md); it is not a
record of a completed live run. The original guest-egress criterion remains unproven by the
production provider assembly described below.

## Prepare the deployment

Use a disposable two-host [remote stack](remote-live-stack.md), with an operator other than the
implementation author running the proof. Confirm the server, worker and reconciler and their
backends are healthy through the deployment's health endpoints first. `doctor` needs a reachable
MCP server and a worker consuming the same job queue; it does not replace process readiness.
Follow [CLI authentication](kdivectl.md#authenticating) with a `platform_operator` token.
`login --platform-role` uses the development mock issuer; production uses an IdP-issued token.

Run the baseline without fault injection. Review every returned check, including local worker
prerequisites and remote-host checks. The production factory currently assembles all enabled
provider contributions even when `--provider` is supplied, so that flag does not isolate a host
or provider. Use each row's `provider` and, where present, `resource_id` to interpret its scope.
Some worker rows lack a host identifier; verify one configured remote instance at a time when
unambiguous host attribution is required.

## Capture each run

Use a separate directory for each baseline, seeded-fault and restored run. This subshell records
the full JSON envelope, stderr and exit code while returning the diagnostic command's exit status:

```bash
(
  umask 077
  doctor_exit=0
  kdivectl doctor --json > doctor.json 2> doctor.stderr || doctor_exit=$?
  printf '%s\n' "$doctor_exit" > doctor.exit
  exit "$doctor_exit"
)
```

Checks are under `items[].data`, including `check`, `status`, `detail`, `fix`, `provider`,
`failure_category`, `resource_id` and check-specific `data`. An outer successful tool envelope
can still contain failed checks. A `fail` row makes `doctor` exit `1`; `error` without `fail`
exits `6`; no failure/error flags exits `0`, even for an empty verdict. Verify the expected rows
actually exist. Authorization denial exits `3`; transport/CLI failures may have no JSON verdict.

## Seed and restore the read checks

Keep the core healthy, seed one condition at a time, capture the result, then restore the original
configuration and rerun. A diagnostic timeout or dispatch error is an unexecuted check, not proof
that the seeded fault was detected. Record all rows: one condition can affect several checks.

| Seeded condition | Expected check | Remediation to verify |
|---|---|---|
| Provider certificate chain not trusted by the worker's configured CA | `provider_tls`: `fail` | Repair the chain or the instance's `ca_cert_ref` in `[[remote_libvirt]]` and its referenced certificate |
| Firewall `DROP` on the reserved lowest port of the configured gdbstub range, from the worker | `gdbstub_acl`: `fail` | Restore the intended worker-to-host firewall rule |
| A required platform secret file is missing while `KDIVE_SECRETS_ROOT` remains accessible | `secret_ref`: `fail` | Restore the file or correct its configured reference |
| The configured base-image volume is absent from the reachable remote storage pool | `remote_libvirt_base_image_staging`: `fail` | Stage the operator-provided volume named by the inventory |

Two probe limits affect these expectations:

- The TLS probe checks the handshake, not libvirt authorization. Its current fix text mentions
  `KDIVE_PROVIDER_CA`, but that is not a registered setting; the real CA reference is the remote
  inventory's `ca_cert_ref`. Record that discrepancy rather than calling the literal fix correct.
- The ACL probe samples only the lowest reserved port. A connection or fast connection refusal
  counts as admitted; a timeout counts as blocked. A firewall `REJECT` can therefore pass, and
  a down host can look blocked. A passing sample does not prove the rest of the range is open.
  Interpret it alongside reachability and the actual firewall policy.

The production secret check reads required secret settings from the server's configuration. It
is not an inventory of every tenant secret. The integration test's injected tenant-reference
non-disclosure case does not prove production tenant-reference coverage.

Also record a run with the provider unreachable. TLS should report `error` with no fix when the
handshake cannot run; reachability may report `fail`, and the ACL result depends on whether the
connection times out or fails another way. Any co-occurring `fail` keeps the overall exit at `1`.
Exit `6` applies only when there are errors and no failed checks.

## Guest-egress proof is unavailable in the reference deployment

The local and remote production contributions supply no egress check. With `--with-egress`,
service assembly fails; the MCP tool converts that failure into a `diagnostics` error row
and `doctor` exits `6`. Staging an image alone does not enable a probe. Do not treat that result
as a tested guest-to-object-store path or ask an operator to wire a custom service factory.

The [integration proof](../../../tests/integration/test_doctor_exit_criterion.py) exercises the
check classes, aggregation, audit and CLI exit mapping with injected provider outcomes and a
fake probe guest. It also tests backend readiness transitions. These are regression tests, not
real TLS/firewall/guest-egress measurements. The ADR's live egress criterion still requires a
real provider-bridge guest, independently verified blocked egress, and a passing request after
repair. Record it as **not exercised — production probe unavailable** for this deployment.

## Record the evidence

For each run, retain the deployed version/revision, anonymous host identifier, seeded change,
relevant verdict rows, process exit code, date, and an operator identifier showing the tester was
not the implementation author. Keep raw evidence private; redact tokens, secret references and
host/person identifiers before sharing it publicly, preserving consistent anonymous labels.

A fault proof passes only when the intended check detects the independently established fault
and names a usable repair, followed by a restored run. Record absent checks, misleading fixes,
errors and unavailable probes explicitly. A green overall exit alone does not establish the
original milestone exit criterion.
