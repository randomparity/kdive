"""Build-subprocess privilege drop for a root worker (ADR-0214).

When the worker runs as root, the local kernel-build lane (ADR-0162) would clone and ``make``
untrusted source as root. ``BuildSandbox`` carries an unprivileged identity and spawns build
subprocesses demoted to it via ``subprocess``'s child-side ``user=``/``group=``. ``SandboxProvider``
resolves the sandbox once per build, fail-closed when root-without-``KDIVE_BUILD_USER``.
"""

from __future__ import annotations

import logging
import os
import pwd
import subprocess  # noqa: S404 - fixed argv, no shell; demotion via user=/group=
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import kdive.config as config
from kdive.config.core_settings import BUILD_USER
from kdive.domain.errors import CategorizedError, ErrorCategory

_log = logging.getLogger(__name__)
_CHILD_ENV_DROP = frozenset({"XDG_RUNTIME_DIR", "XDG_CACHE_HOME"})


@dataclass(frozen=True, slots=True)
class BuildSandbox:
    """An unprivileged identity build subprocesses are demoted to (ADR-0214)."""

    uid: int
    gid: int
    extra_groups: tuple[int, ...]
    user_name: str
    home: str
    umask: int = 0o077

    def run(
        self, argv: list[str], *, env: dict[str, str] | None = None, **kwargs: Any
    ) -> subprocess.CompletedProcess:
        """Spawn ``argv`` demoted to this identity (child-side setuid/setgid + build-user env)."""
        return subprocess.run(
            argv,
            user=self.uid,
            group=self.gid,
            extra_groups=list(self.extra_groups),
            umask=self.umask,
            env=self._child_env(env),
            **kwargs,
        )

    def own(self, path: str | Path) -> None:
        """``chown`` ``path`` to this identity so a demoted subprocess can write under it."""
        os.chown(path, self.uid, self.gid, follow_symlinks=False)

    def _child_env(self, env: dict[str, str] | None) -> dict[str, str]:
        # subprocess user=/group= change uid/gid but NOT the environment. Without this the demoted
        # child inherits the root worker's HOME=/root etc., breaking tools that write under $HOME
        # and leaving an incomplete sandbox. Layer the build-user identity over the caller's env
        # (e.g. the hardened git env) rather than discarding it.
        base = {
            key: value
            for key, value in (env if env is not None else os.environ).items()
            if key not in _CHILD_ENV_DROP
        }
        base.update(HOME=self.home, USER=self.user_name, LOGNAME=self.user_name)
        return base


def sandbox_run(
    sandbox: BuildSandbox | None, argv: list[str], **kwargs: Any
) -> subprocess.CompletedProcess:
    """Run ``argv`` demoted when ``sandbox`` is set, else as the current user (no setuid ask)."""
    if sandbox is None:
        return subprocess.run(argv, **kwargs)
    return sandbox.run(argv, **kwargs)


def _resolve_sandbox() -> BuildSandbox | None:
    """Resolve the build sandbox from euid + ``KDIVE_BUILD_USER`` (ADR-0214 resolution table)."""
    if os.geteuid() != 0:
        return None
    name = (config.get(BUILD_USER) or "").strip()
    if not name:
        raise CategorizedError(
            "the worker runs as root but KDIVE_BUILD_USER is not set, so the local build lane "
            "would compile untrusted source as root; set KDIVE_BUILD_USER to an unprivileged "
            "account (see resource://kdive/docs/operating/build-source-staging.md)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    try:
        entry = pwd.getpwnam(name)
    except KeyError as exc:
        raise CategorizedError(
            "KDIVE_BUILD_USER does not name a known account on the worker host",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_user": name},
        ) from exc
    if entry.pw_uid == 0:
        raise CategorizedError(
            "KDIVE_BUILD_USER must be an unprivileged account, not root (uid 0)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"build_user": name},
        )
    return BuildSandbox(
        uid=entry.pw_uid,
        gid=entry.pw_gid,
        extra_groups=tuple(os.getgrouplist(entry.pw_name, entry.pw_gid)),
        user_name=entry.pw_name,
        home=entry.pw_dir,
    )


class SandboxProvider:
    """Resolve the build sandbox once per build, memoized; re-raise a fail-closed error."""

    def __init__(self) -> None:
        self._resolved = False
        self._sandbox: BuildSandbox | None = None
        self._error: CategorizedError | None = None

    def get(self) -> BuildSandbox | None:
        """The resolved sandbox (``None`` when the worker is not root); raise if fail-closed."""
        if not self._resolved:
            try:
                self._sandbox = _resolve_sandbox()
                self._log_outcome()
            except CategorizedError as exc:
                self._error = exc
            self._resolved = True
        if self._error is not None:
            raise self._error
        return self._sandbox

    def _log_outcome(self) -> None:
        if self._sandbox is None:
            _log.debug("build: no privilege drop (worker euid != 0)")
        else:
            _log.info(
                "build: dropping privileges to %s (uid=%d gid=%d)",
                self._sandbox.user_name,
                self._sandbox.uid,
                self._sandbox.gid,
            )


def resolve_build_sandbox_provider() -> SandboxProvider:
    """A fresh memoizing provider; resolution is deferred to the first ``.get()`` at build time."""
    return SandboxProvider()
