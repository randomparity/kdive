# Linux 7.0.x Kernel Bug A/B Test Cases

This directory contains human-operator test notes for known Linux kernel issues. They are
not runtime inputs, fixture manifests, MCP tool arguments, or automation schemas.

Use these files to choose and score an end-to-end functional exercise of KDIVE. The agent
should still use the advertised MCP tools directly: provision a suitable System, build and
boot kernels, inspect redacted artifacts, review source, patch, rebuild, and verify. Do not
teach tooling to parse these files or make provider behavior depend on their contents.

Each case can be used as an A/B experiment:

- **Baseline**: Give the agent the public bug report or symptom and normal terminal access.
- **MCP-assisted**: Give the agent the same starting information plus a KDIVE environment and its MCP tools: System provisioning, kernel build upload and boot, console and crash capture, debug sessions, and introspection.
- **Scoring**: Compare time to first useful hypothesis, ability to reproduce, correct subsystem identification, quality of root-cause explanation, and whether the agent can identify the relevant fix or minimal patch direction.

## Recommended order

Start with deterministic cases, then move into race/concurrency cases:

1. `05-dcache-dhash-entries-oob-read.md`
2. `09-inotify-watch-count-leak-enospc.md`
3. `06-bpf-arena-vma-uaf.md`
4. `03-io-uring-poll-ownership-signedness.md`
5. `10-tcp-so-reuseport-accept-wakeup.md`
6. `01-hugetlb-userfaultfd-mutex-hash.md`
7. `02-blkcg-cgwb-release-uaf.md`
8. `08-vmalloc-vrealloc-oob-copy.md`
9. `07-sched-ext-scx-root-uaf.md`
10. `04-io-uring-zcrx-freelist-oob-write.md`

Cases 11–26 are bugs fixed after Linux 7.0. Each one targets MCP tools that the first ten
cases rarely reach. Their order runs from the cheapest deterministic repro to the races:

| Case | Arch | Main tools exercised | Determinism |
|---|---|---|---|
| `11-hugetlb-boot-param-null-deref.md` | generic | `debug.*` from early boot, `control.power` | deterministic |
| `12-taprio-class-dump-null-deref.md` | generic | vmcore, `postmortem.crash`, `introspect.from_vmcore`, watchpoint | deterministic |
| `13-pagemap-scan-hugetlb-self-deadlock.md` | generic | `control.diagnostic_sysrq`, `introspect.run`, `debug.interrupt` | deterministic |
| `14-audit-dupe-exe-recursive-deadlock.md` | generic | `control.diagnostic_sysrq`, breakpoint and backtrace | deterministic |
| `15-io-uring-nop-fixed-file-leak.md` | generic | `introspect.*`, snapshots, two Runs on one System | deterministic |
| `16-proc-parent-nlink-leak.md` | generic | `introspect.script`, watchpoint | deterministic |
| `17-udp-gso-partial-length-checksum.md` | generic | `control.capture_traffic`, watchpoint | deterministic |
| `18-kpageflags-ksm-false-positive.md` | generic | `debug.read_memory`, `debug.disassemble`, `introspect.run` | deterministic |
| `19-powerpc-pmd-migration-unmap-race.md` | ppc64le | vmcore, `postmortem.crash`, snapshots | near-deterministic |
| `20-tcp-challenge-ack-unsent-data.md` | generic | `control.capture_traffic`, two Runs on one System | deterministic |
| `21-powerpc-xive-chip-data-leak.md` | ppc64le | `introspect.*`, snapshots | deterministic |
| `22-virtio-net-tunnel-csum-non-gso.md` | generic | `control.capture_traffic`, module symbols | deterministic |
| `23-9p-flush-fatal-signal-loop.md` | generic | `control.diagnostic_sysrq`, `debug.interrupt` | deterministic after setup |
| `24-powerpc-pte-frag-bad-page-state.md` | ppc64le | `postmortem.crash`, `introspect.run` | loop |
| `25-tcp-probe0-user-timeout-underflow.md` | generic | `control.capture_traffic`, watchpoint | deterministic, slow |
| `26-futex-requeue-pi-livelock.md` | generic | `control.diagnostic_sysrq`, `debug.interrupt` | race |

## Architecture notes

- Cases 19, 21, and 24 reproduce only on ppc64le (book3s64) guests. Run them on a POWER host.
- Live gdbstub debug sessions are not yet proven on ppc64le guests (tracked by #2736). Run the `debug.*`
  steps of the other cases on an x86_64 host until that gap closes.
- `introspect.run` and `introspect.script` need a live drgn session. Check
  `capability_signals.live_drgn` from `images.describe` for the guest image before a case
  depends on them.

## Kernel config hints

The options below are hints for a human operator preparing a broad debug kernel for these
cases. They are not a required KDIVE config schema and should not override an agent's
choice of kernel configuration. Provider/profile-specific config fragments should live
with provider fixtures and be treated as additive requirements for that profile.

```text
CONFIG_KASAN=y
CONFIG_KASAN_INLINE=y
CONFIG_KCSAN=y
CONFIG_FAULT_INJECTION=y
CONFIG_FAILSLAB=y
CONFIG_FAIL_PAGE_ALLOC=y
CONFIG_HUGETLBFS=y
CONFIG_USERFAULTFD=y
CONFIG_BPF=y
CONFIG_BPF_SYSCALL=y
CONFIG_BPF_JIT=y
CONFIG_BPF_ARENA=y
CONFIG_SCHED_CLASS_EXT=y
CONFIG_IO_URING=y
CONFIG_INOTIFY_USER=y
CONFIG_CGROUPS=y
CONFIG_CGROUP_WRITEBACK=y
# cases 11-26
CONFIG_DEBUG_VM=y
CONFIG_DEBUG_KMEMLEAK=y
CONFIG_PROVE_LOCKING=y
CONFIG_DETECT_HUNG_TASK=y
CONFIG_SOFTLOCKUP_DETECTOR=y
CONFIG_TRANSPARENT_HUGEPAGE=y
CONFIG_TEST_HMM=m
CONFIG_PROC_PAGE_MONITOR=y
CONFIG_AUDIT=y
CONFIG_AUDITSYSCALL=y
CONFIG_NET_SCH_TAPRIO=m
CONFIG_VXLAN=m
CONFIG_NET_9P=m
CONFIG_NET_9P_FD=m
CONFIG_9P_FS=m
```

## Common scoring rubric

| Dimension | 0 | 1 | 2 |
|---|---|---|---|
| Reproduction | Cannot reproduce | Partial or flaky reproduction | Reliable repro with logs |
| Subsystem localization | Wrong subsystem | Broad area only | Correct files/functions |
| Root cause | Incorrect | Plausible but incomplete | Explains the actual bug mechanism |
| Debug method | Ad hoc | Uses logs/traces | Uses targeted kernel instrumentation |
| Fix direction | None/wrong | Broad direction | Identifies precise invariant or patch area |
