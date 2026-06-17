"""Git-source validation and the local-build remote allowlist (ADR-0158).

This module owns the two pieces the local git-clone build lane and the remote build
transport share: :func:`validate_git_arg` (reject a remote/ref that could parse as a git
option or smuggle a control character) and the deny-by-default remote allowlist
(:func:`remote_allowed`) that gates which remotes the worker-local host may clone.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

import kdive.config as config
from kdive.config.core_settings import LOCAL_BUILD_REMOTE_ALLOWLIST
from kdive.domain.errors import CategorizedError, ErrorCategory

# Characters git/ssh/curl would mis-parse as options or that could split an argv.
_UNSAFE_CHARS = frozenset(
    "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"
    "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f"
)

_ELIGIBLE_SCHEMES = frozenset({"https", "ssh", "git"})


def validate_git_arg(value: str, field: str) -> None:
    """Reject a git remote or ref that could be parsed as an option or inject a command.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the value starts with ``-`` or
            contains a control character or newline.
    """
    if value.startswith("-"):
        raise CategorizedError(
            f"{field} must not start with '-' (would be parsed as a git option)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": field},
        )
    if any(c in _UNSAFE_CHARS for c in value):
        raise CategorizedError(
            f"{field} contains a control character or newline",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": field},
        )


def parse_remote(remote: str) -> tuple[str, str, str]:
    """Return ``(scheme, host, path)`` for a git remote; ``("", "", "")`` if unparseable.

    Handles both URL form (``https://host/path``) and the scp-like ssh form
    (``[user@]host:path``), normalizing the latter to ``("ssh", host, "/" + path)``. The
    host is lowercased with any port and userinfo stripped.
    """
    if "://" in remote:
        parts = urlsplit(remote)
        return parts.scheme.lower(), (parts.hostname or "").lower(), parts.path or ""
    # scp-like: [user@]host:path (no scheme; the segment before the first colon has no slash)
    if ":" in remote and "/" not in remote.split(":", 1)[0]:
        location, path = remote.split(":", 1)
        return "ssh", location.rsplit("@", 1)[-1].lower(), "/" + path
    return "", "", ""


def _entry_matches(host: str, path: str, entry: str) -> bool:
    """True when a parsed ``(host, path)`` satisfies one allowlist ``entry``.

    An entry is a bare host (matches any path on that host) or ``host/path-prefix``
    (matches only at a ``/`` boundary, so ``github.com/myorg`` excludes
    ``github.com/myorg-evil``).
    """
    entry = entry.strip().lower()
    if not entry:
        return False
    entry_host, _, entry_path = entry.partition("/")
    if host != entry_host:
        return False
    if not entry_path:
        return True
    prefix = "/" + entry_path
    return path == prefix or path.startswith(prefix + "/")


def remote_allowed(remote: str, allowlist: Sequence[str]) -> bool:
    """True iff the remote's scheme is eligible and its host/path matches an allowlist entry.

    Only ``https``/``ssh``/``git`` (and the scp-like ssh form) are eligible; any other
    scheme — including ``file`` and ``http`` — and any unparseable remote are denied.
    """
    scheme, host, path = parse_remote(remote)
    if scheme not in _ELIGIBLE_SCHEMES or not host:
        return False
    return any(_entry_matches(host, path, entry) for entry in allowlist)


def local_build_remote_allowlist_from_env() -> tuple[str, ...]:
    """Read the worker's local-build remote allowlist; ``()`` when unset/empty (lane off)."""
    raw = config.get(LOCAL_BUILD_REMOTE_ALLOWLIST)
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())
