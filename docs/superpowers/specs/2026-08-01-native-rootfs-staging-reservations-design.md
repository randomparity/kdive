# Historical verification — Spec: Native uploaded-rootfs staging reservations (#1546)

Recorded 2026-08-01 for the then-current checkout. These are historical results; they do not
establish a passing result or supported procedure for the current release.

Verified on 2026-08-01: the focused module passed 108 tests, including a successful real-filesystem
native allocation smoke. `just ci` passed with 11,693 tests and 16 skips: six absent live-stack
OIDC fixtures, six Docker/image smoke prerequisites, absent `promtool` in compose and Helm checks,
one required CLI-option case, and one live-stack skew test that deliberately skips on an
uncommitted source tree. The concurrency mutation replaced native reservation with unconditional
success and the deterministic race failed before either caller reached its allocator barrier. The
identity-length, gzip-shrink, and 64-bit-prototype mutations each reddened their focused test. The
degrade test replaces `os.posix_fallocate` with a sentinel that fails immediately if the unsupported
native-allocation path ever calls it.

Decision and executable owners:

- [0450-uploaded-rootfs-staging-free-space-precheck.md](../../adr/0450-uploaded-rootfs-staging-free-space-precheck.md)
- [0530-native-rootfs-staging-reservations.md](../../adr/0530-native-rootfs-staging-reservations.md)
- [test_rootfs_upload_fetch.py](../../../tests/providers/local_libvirt/test_rootfs_upload_fetch.py)
