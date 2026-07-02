# Plan — Per-family live SSH reachability proof (#956, ADR-0294)

Derived from `docs/superpowers/specs/2026-07-01-per-family-ssh-reachability-design.md`
and [ADR-0294](../../adr/0294-per-family-ssh-reachability-proof.md). Small, self-contained;
implemented directly (not subagent-dispatched). Guardrails: `just lint`, `just type`
(whole-tree), `just test` (deselects `live_stack`), run individually as CI does.

## Task 1 — Add the gated per-family reachability test (TDD)

**Where it fits:** the substantive close for #956 — proves the always-rendered SSH
forward reaches a guest sshd per family, rather than by assumption.

**Files:**
- `tests/integration/test_live_stack.py` — add the parametrized test + a per-family
  preflight helper + the two image env-var constants + a minimal reachability provision
  profile factory.

**TDD sequence (this is a `live_stack`-gated test, so "fails for the right reason" =
skips cleanly with the exact fix message when a prerequisite is absent; it cannot fail
red in CI):**
1. Write the test first. Confirm that under `just test` (which runs
   `-m "not live_vm and not live_stack"`) it is **deselected**, and that when collected
   directly without a stack/images it **skips** with actionable messages (never errors).
   `uv run python -m pytest tests/integration/test_live_stack.py -m live_stack --co -q`
   must collect the parametrized cases; running them without prerequisites must skip.
2. Implement the helpers so the skip messages name the exact missing env var.
3. LIVE-PROVE on this KVM host (see Task 5): run the marked test against a real stack +
   at least one per-family image, capture the pass, record it in the PR body.

**Test shape (reuse spine helpers `allocate`-equivalent inline / `provision_to_ready`,
`scalar`, `ok`, `drain_job`, `await_system_state`, `data_str`):**
- `@pytest.mark.live_stack` + `@pytest.mark.parametrize("family, image_env", [...])`.
- Preflight: resolve issuer + stack + db_url (reuse `_spine_preflight`'s checks) AND the
  per-family image env var AND `KDIVE_KERNEL_SRC`; `pytest.skip` with the exact fix when
  any is absent. **Skip only the current parameter.**
- Profile: `direct-kernel`, `vcpu/memory/disk` matching the allocation request and
  `LOCAL_ALLOCATION_DISK_GB`, `kernel_source_ref` from `KDIVE_KERNEL_SRC`, rootfs
  `{"kind": "local", "path": <per-family image>}`. **No `ssh_credential_ref`, no
  `destructive_ops`.**
- Body: `allocations.request` → `systems.provision` → `await_system_state(..., "ready")`
  → `systems.ssh_info` (assert `data.ssh.host_scope == "worker_loopback"`, non-empty
  host+port, status ok) → `systems.authorize_ssh_key(public_key=<throwaway ed25519 pub>)`
  → `drain_job`. On a non-succeeded drain raise `SpinePhaseError(family, ...,
  error_category=...)` including the job `failure_detail`. `finally`:
  `allocations.release`.
- Throwaway key: a syntactically valid `ssh-ed25519 <base64> kdive-956-e2e` public key
  that passes `validate_authorized_public_key` (only the public half; no private key
  needed).

**Acceptance:** deselected by `just test`; collects under `-m live_stack`; skips cleanly
without prerequisites; on a live host with a per-family image, provisions to ready and
the authorize job drains succeeded. Does not un-gate any marker.

**Rollback:** the test is additive; revert the file hunk to remove it.

## Task 2 — Register the two image env vars in the env reference

**Where it fits:** success criterion. `scripts/check_env_documented.py` sweeps `src/
tests/ scripts/ deploy/` for `KDIVE_*` tokens (verified) and fails CI on any token that
is neither a registry setting nor catalogued in `kdive.config.external_env`. The two new
test-only tokens must therefore be catalogued.

**Files:** `src/kdive/config/external_env.py` — add two `ExternalEnvVar` entries in the
"test-only (gated suites)" block next to `KDIVE_GUEST_IMAGE` (line ~45), scope `"test"`,
`default=None`, help naming the family and the "unset → that parameter skips" behavior.

**No generated snapshot to regenerate:** `scripts/gen_config_reference.py` renders from
`external_env`, but its test (`tests/scripts/test_gen_config_reference.py`) uses synthetic
fixtures, and no committed doc lists the real live-stack image vars — verified. So this is
a one-file change; no snapshot invalidation.

**Acceptance:** `uv run python scripts/check_env_documented.py` exits 0; `just test`
green (the guard has a test wrapper). The two vars sit alongside `KDIVE_GUEST_IMAGE`.

**Rollback:** revert the two `EXTERNAL_ENV_VARS` entries.

## Task 3 — Correct the stale `debian.py` comment

**Where it fits:** housekeeping criterion 4; the comment near `debian.py:110` claims
"cloud-init's cloud-ifupdown-helper DHCPs the NIC", false post-#962 (cloud-init is no
longer disabled; the mechanism is the cloud.cfg `dhcp4` netplan config, not
cloud-ifupdown-helper).

**Files:** `src/kdive/images/families/debian.py` (comment only, no code change).

**Acceptance:** comment matches the ADR-0288 reality; `just lint`/`just type` green; no
behavior change (the argv line is untouched).

**Rollback:** revert the comment.

## Task 4 — Update the `ssh_reachable` PlannedSignal rationale + tracking issue

**Where it fits:** housekeeping criterion 5; the `PlannedSignal("ssh_reachable", "#956",
"sshd/keygen liveness is broken; not an honest per-image fact yet")` rationale is stale
post-#962.

**Files:** `src/kdive/images/capability_signals.py`.

**Dependency:** Task 6 (the follow-up issue number) must exist first so the
`tracking_issue` points at it, not #956.

**Acceptance:** rationale reflects that reachability now works and the open question is
static-signal-vs-runtime-probe; `tracking_issue` is the new issue. Check for a test that
pins the `PLANNED_SIGNALS` contents (there is enforcement in the ADR-0286 test set) and
update it in the same change. `just test` green.

**Rollback:** revert the tuple entry + any pinning test.

## Task 5 — LIVE-PROVE on the KVM host

**Where it fits:** ADR-0294 says the proof is operator-run; this host runs KVM/libvirt,
so run it. Bring up the live stack (`scripts/live-stack/up.sh` / `just stack-up` per the
runbook), build or locate a `debian-kdive-ready-*` and/or rhel-family ready image, set
the env vars, and run the marked test. Capture the pass (and the phase output) for the PR
body. If only one family's image is available, prove that family and note the other as
deferred in the PR body — do not fake the missing one.

**Acceptance:** at least one family's parametrized case passes live; recorded in the PR
body with the command and outcome.

## Task 6 — File the deferred (B) follow-up issue

**Where it fits:** criterion 6; deferred `ssh_reachable` health-signal design.

**Action:** `gh issue create` — title "surface `ssh_reachable` on
`systems.get`/`ssh_info`", labels `type:enhancement`, `area:provisioning`. Body states
the fork (runtime probe on `ssh_info` vs the existing static `PlannedSignal`
image-capability layer), references #956, and notes the ADR-0288 fix made reachability
real. Feed the returned number into Task 4.

**Ordering:** Task 6 before Task 4 (Task 4 needs the number).

## Overall ordering

1. Task 6 (file follow-up issue → get number).
2. Task 4 (uses the number) + Task 3 (independent) — doc + code housekeeping.
3. Task 1 (the test) + Task 2 (env docs).
4. Guardrails green after each commit.
5. Task 5 (live-prove) once the test compiles + skips cleanly.
6. Adversarial branch review, security review, full suite, push, PR.
