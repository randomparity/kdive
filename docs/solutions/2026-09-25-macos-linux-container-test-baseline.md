---
title: macOS `just test` gives no usable baseline; run the suite in a local Linux container
date: 2026-09-25
tags: [environment-quirk, test-baseline, docker, testcontainers, macos]
components: [justfile, tests/db/conftest.py, .github/workflows/ci.yml]
---

## Problem

On macOS, `just test` fails hundreds of tests for host reasons, on `main` and on every branch.
One branch run on 2026-09-25 gave `321 failed, 18921 passed, 142 skipped, 37 errors`, and
`main` failed in the same way. A comparison against `main` on macOS cannot show whether a change
broke a test, because the noise is larger than the signal. The first attempt at a Linux container
also had noise: `60 failed, 37 errors` on `main`.

## Root cause

The failures come from the test host, not from the code:

- macOS: tests need Linux behavior (for example `/proc`, pidfd, and Linux tool output).
- An arm64 container on Apple silicon: `error: unsupported architecture: aarch64` and
  `capture bootstrap manifest architecture drift`. CI runs on x86_64 (`ubuntu-latest`).
- A container that runs as root: `stack-services.sh must run as the provisioned
  lifecycle-control operator, not UID 0`, and the permission tests that expect a refusal
  do not get one.
- Missing tools: `FileNotFoundError: ... 'just'`, and no Docker CLI or compose plugin.
- Too many xdist workers against the one testcontainers PostgreSQL cluster:
  `cluster-global runtime-role test lock timed out after 60000 milliseconds`. These errors
  depend on timing, so they can appear on one side of a comparison only.

## Solution

Use `just test-linux`
([cross-platform guide](../development/cross-platform.md#macos-run-the-suite-in-a-linux-container),
ADR-0717, #3072). It runs the `just test` selection natively on arm64 in a committed image, and
a clean `main` exits 0, so no baseline comparison is needed. The manual amd64 recipe that this
section held is gone: under Rosetta the capture seccomp filter does not load
(`seccomp_load failed: 125`), which kept a 50-failure baseline.

## Prevention

- Do not use a macOS `just test` result as regression evidence. Use `just test-linux`, or CI.
- Keep `--maxprocesses` at 8 or lower in a container. With 12 workers the shared PostgreSQL
  global lock timed out. `just test-linux` sets 8.
