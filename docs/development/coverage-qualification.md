# Coverage obligations and qualification

`just coverage-check` validates the reviewed ownership of the shipped tool registry and image,
architecture and provider-capability inventory. It runs in `just ci` and PR CI. A zero exit means
that the mappings are complete; its output explicitly says **live qualification unproven**.
Pending scenarios remain required even when pytest never collects them.

The version-1 mapping is `scripts/coverage_campaign/obligations.toml`. Every registered tool,
including conditional recovery tools, needs an explicit observable assertion and owning issue.
Provider-dependent operations expand across providers, native architectures and relevant capability
modes. Catalog smoke covers every image, including build images. Deep lifecycle, foreign TCG,
clean-host, failure/resource and kernel-corpus obligations also remain in the expected set.
Native x86_64/KVM, ppc64le/KVM-HV and foreign-architecture TCG are separate cells.

The foundation marked no live scenario implemented; catalog image smoke (#2808), the local deep
lifecycle (#2809) and `host-install` (see [Host-installation producer](#host-installation-producer))
are now bound to their producers. The owner issues in the manifest and expanded cells supply their
fixtures and producers under epic
[#2803](https://github.com/randomparity/kdive/issues/2803). Adding a scenario means adding its
repository-relative pytest node ID to the manifest's `implementations` table, keyed by the existing
scenario ID. The checker verifies that the file and test function exist. Existence alone is not
execution proof. The existing campaign driver is not an evidence producer.

## Qualify a candidate

Run against separately prepared binding and result files:

```sh
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

Exit codes are 0 for complete qualification, 1 for incomplete/failed qualification and 2 for invalid
input or contract. The report lists every required cell in stable order. Missing results become
`not-run`; duplicate and unexpected results fail instead of overwriting earlier evidence. The
command always builds the shipped contract; result files cannot select a smaller required set.

There are six outcomes:

| Outcome | Meaning |
|---|---|
| `success` | All required observable functional assertions have retained evidence. |
| `rejection` | The separate expected-rejection cell proves its boundary and unchanged state. |
| `unsupported` | The contract contains a reviewed capability/architecture reason. |
| `failure` | The scenario failed or its evidence does not match the contract. |
| `blocked` | Required infrastructure or input bindings are unavailable. |
| `not-run` | The scenario did not run, was skipped, has no result or remains unimplemented. |

Only the first three can qualify, each for its own contract kind. A functional cell cannot be
replaced by a rejection or unsupported result. `known-defect` forces failure;
`missing-prerequisite` forces blocked unless a known defect already forces failure. Neither can
be turned into an exemption. Pending implementations cannot qualify through submitted success.

## Version-1 records

Bindings are an object with `version` (integer 1), `candidate_sha` (full lowercase Git SHA),
`matrix_sha256` (from the fast check) and `cells`, a map from required cell ID to `Context`.
The binding is the independent expectation: prepare it from the selected candidate and immutable
fixtures before admitting producer results. Do not derive expected inputs from the result file.
Unknown binding keys fail; missing applicable bindings prevent qualification. Catalog smoke also
checks the exact guest distribution/version against its catalog row. Family lanes check the guest
family, or the host family for clean-host installation, against catalog ownership.

Each `Context` has these fields. Optional fields use JSON null when inapplicable; a cell's required
input names override that optionality.

| Field | Value |
|---|---|
| `host_os`, `guest_os` | Public distribution/version, e.g. `ubuntu:26.04`, `fedora:44`; guest may be null. |
| `host_arch`, `guest_arch` | `x86_64`, `ppc64le` or host/service `aarch64`; guest may be null. |
| `accelerator` | `none`, `kvm`, `kvm-hv` or `tcg`, matching the required cell. |
| `image_sha256`, `kernel_sha256` | Content digests for the selected image and kernel. |
| `kernel_source_sha` | Full lowercase Git SHA of the kernel source. |
| `kernel_config_sha256`, `compiler_id` | SHA-256 identities of configuration and compiler/toolchain. |
| `kernel_build_id` | Lowercase hexadecimal ELF build ID, 16–128 characters. |

OS names are restricted to `ubuntu`, `debian`, `fedora`, `rhel`, `rocky`, `almalinux`,
`centos-stream`, `opensuse-leap`, `opensuse-tumbleweed` and `sles`, followed by a numeric dotted version. Use a public platform
identity, never a host name, address or private path. This schema does not declare new VM targets.

Results are one JSON array. Each record contains:

| Field | Value |
|---|---|
| `version` | Integer 1. |
| `cell_id`, `scenario_id`, `node_id` | Exact required cell, scenario and implemented pytest node. |
| `outcome` | One of the six values above. |
| `candidate_sha`, `matrix_sha256` | Exact candidate and generated matrix identities. |
| `input_sha256` | Canonical digest of the independently bound context. |
| `deployed_roles` | Map of deployed `server`, `worker`, `reconciler` and, when installed, `authority` revisions to full SHAs. It must include the cell's required roles. |
| `context` | Actual observed `Context`, equal to the independent binding. |
| `duration_seconds` | Finite, nonnegative seconds measured by the producer. |
| `assertions` | Map of exact required assertion IDs to SHA-256 artifact digests. |
| `artifacts` | Retained content digests referenced by those assertions. |
| `impediments` | Array containing `known-defect`, `missing-prerequisite`, or neither. |

The required roles, inputs and assertions belong to the generated cell. Producers cannot reduce
that set. Every deployed revision supplied must match the candidate; unknown or missing required
revisions fail. Functional/rejection assertions must match the required set exactly and reference
retained artifacts. Terminal effects and owned-resource cleanup are assertions, not assumptions
made from a successful tool envelope. No artifact URL or raw producer diagnostic enters the report.

Reviewed producer code can import `Context`, `Evidence`, `InputBindings` from
`scripts.coverage_campaign.evidence` and `build_contract`, `digest` from
`scripts.coverage_campaign.contract`. Compute `input_sha256` with `digest(context)`; it hashes the
canonical model JSON, including default null fields. `build_contract().cells` exposes the exact IDs,
roles, assertions and input requirements to scenario authors. These are offline tooling interfaces,
not production MCP APIs. A mapping, schema, catalog or capability change invalidates the old matrix.

JSON rejects duplicate keys, extra fields and wrong types. Files are bounded to 64 MiB and 100,000
records; each result allows at most 256 assertions and artifact references. Put large diagnostics
in retained artifacts and submit their digests. Validation errors name a schema field/category
without echoing supplied values. Keep detailed producer diagnostics private and redact them before
publishing separately.

## Host-installation producer

`scripts/host_install_proof.py` produces the `host-install/local-libvirt/<arch>/<family>` results
([ADR-0716](../adr/0716-host-install-evidence-producer.md)). The `host-install` scenario binds
every family and architecture to one on-host node,
`tests/integration/test_host_install_live.py::test_installed_host_boots_pinned_kernel`. That node
skips unless the runner supplies its phase inputs.

1. Cut the pinned kernel bundle on the host where the fixture verifies in place, its native build
   host:

   ```sh
   uv run python -m scripts.host_install_proof bundle --fixture "$fixture_root/longterm"      --output "$bundle"
   ```

2. Reset an exclusive host to its clean baseline. The host needs non-interactive `sudo`,
   `/dev/kvm` and a pinned SSH host key. Then, from a clean controller checkout at the
   candidate, run:

   ```sh
   uv run python -m scripts.host_install_proof run --target "$user@$host"      --known-hosts "$known_hosts" --family fedora --candidate "$(git rev-parse HEAD)"      --bundle "$bundle" --guest-image fedora-kdive-ready-44 --output "$run_dir"      [--operator-prerequisites "$prerequisites"]
   ```

   The runner checks that the host is clean and that its distribution and architecture match the
   cell. It then runs the documented entry points in separate login sessions: bootstrap, the
   example's kernel tree (`scripts/fetch-kernel-tree.sh`), `just setup`, `just prepare-local-libvirt-host`, `just check-local-libvirt`,
   `examples/local-libvirt/demo-up.sh` and `examples/local-libvirt/build-image.sh`. After that it
   runs the node, repeats the setup steps, and runs the node again. The optional prerequisites
   file holds only operator duties the installation docs assign, such as a Docker engine where
   the distribution has no known package. Its digest is part of the evidence.

3. Combine runs and qualify:

   ```sh
   uv run python -m scripts.host_install_proof merge --output "$merged" "$run_dir"...
   uv run python -m scripts.coverage_campaign qualify --inputs "$merged/inputs.json"      --results "$merged/results.json"
   ```

A run directory holds these files:

| File | Contents |
|---|---|
| `binding.json` | One-cell bindings: host platform from the pre-install observation, guest platform from the catalog row, kernel identity from the bundle, image digest from the built image. |
| `result.json` | The single `Evidence` record. |
| `artifacts/` | One canonical JSON artifact per assertion. |
| `summary.json` | Each step's `name`, `exit_code` and `seconds`, plus the `outcome` and, for exits 2 and 3, a `reason`. |
| `steps/`, `phases/` | Private transcripts and phase records. |

Keep the run directory private: it holds the transcripts, the phase records and every assertion
artifact. Publish only sanitized excerpts.

`run` exits with one of these codes:

| Exit | Meaning |
|---|---|
| 0 | `success` |
| 1 | `failure` |
| 2 | Invalid input or a host that does not match the cell (nothing mutated), or a controller error such as a full disk after the run started (the host may be mutated; reset it). |
| 3 | `blocked`, or a host that could not be identified (no result; `merge` skips the directory and the cell stays `not-run`). `summary.json` `reason` says why: `not-clean` or `unidentified` may clear after a reset; `no-sudo` and `no-kvm` will not, so a reset wrapper caps its retries. |

A failed or timed-out install or setup step stops the run. A failed boot phase that wrote its
record still continues through repeat setup and the second boot, so both phases are recorded.
A host reset is the lab's responsibility, not KDIVE's.

## Limits and verification

The qualifier checks accounting and identity, not whether a trusted producer fabricated an
assertion or digest. It does not fetch artifacts or attest remote execution. Scheduling, artifact
retention and release enforcement belong to
[#2819](https://github.com/randomparity/kdive/issues/2819).

The focused tests start with a complete synthetic contract, then deliberately remove results,
skip scenarios, stale fixture identities, mismatch deployed SHAs and fail scenarios. Every such
experiment must turn qualification red. Synthetic success proves the checker, not a live platform.
See [ADR-0686](../adr/0686-independent-coverage-obligations.md) for the decision and approved scope.
