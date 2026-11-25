"""Remote-libvirt URI validation shared by config and transport."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit

from kdive.domain.errors import CategorizedError, ErrorCategory

#: The only transport a remote-libvirt URI may name (ADR-0077). Public because the reachability
#: gate re-checks it on the URI it is about to probe, and a second copy of the literal could drift.
REQUIRED_REMOTE_SCHEME = "qemu+tls"


def reportable_remote_uri(uri: str) -> str:
    """``uri`` reduced to scheme, host, port and path, for a message or an error detail.

    ``remote_connection`` composes ``?pkipath=<mkdtemp dir>`` onto the URI before handing it to the
    opener, and that directory holds the 0600 client key for the op. Reporting the composed spelling
    would put the path to live private-key material into a WARNING that ``_enter_host`` already logs
    with ``exc_info=True``. The netloc is rebuilt from the host and port rather than reused, so any
    userinfo an operator put in the URI goes with the query rather than surviving into the log.
    """
    try:
        parsed = urlsplit(uri)
        host = parsed.hostname or ""
    except ValueError:
        return "<malformed URI>"
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        # This runs on the path that *reports* a malformed port, so it cannot re-raise on one.
        port = None
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _query_param_names(query: str) -> set[str]:
    """The lowercased parameter names of a URI query, split conservatively like libvirt's.

    libvirt's URI parser percent-unescapes parameter names, matches them case-insensitively
    (``STRCASEEQ`` in the remote driver), and splits on ``&`` — falling back to ``;`` only
    when no ``&`` remains. Splitting on both separators unconditionally yields a **superset**
    of the names libvirt would extract (a phantom extra name in mixed-separator queries),
    which can only over-reject, never under-reject — the fail-closed direction.
    """
    names: set[str] = set()
    for chunk in query.replace(";", "&").split("&"):
        if not chunk:
            continue
        names.add(unquote(chunk.split("=", 1)[0]).lower())
    return names


def validate_remote_transport(uri: str) -> None:
    """Reject a URI whose transport would weaken mutual TLS (fail-closed, ADR-0077).

    The subset of :func:`validate_remote_uri` that stays true **after** the per-op pkipath is
    composed on, so a caller downstream of ``compose_pkipath_uri`` can re-check what it is about to
    connect to. Split out rather than duplicated: two copies of the scheme literal and the
    forbidden-parameter spellings would drift, and the spelling is the whole of the control.

    TLS-affecting remote-driver URI parameters reviewed (libvirt remote URIs): ``no_verify``
    (turns server-cert verification off), ``pkipath`` (operator-controlled credential source),
    and ``tls_priority`` (GnuTLS priority string; can select anonymous or weak ciphersuites).
    The remote driver also extracts an undocumented ``no_sanity`` boolean, which gates only
    the local structural pre-flight of the loaded certs (``virNetTLSCertSanityCheck``); peer
    verification is independent of it, and the cert material comes from secret refs rather
    than the URI, so it is accepted. Everything else (``mode``, ``proxy``, ``keepalive_*``,
    ``socket``/``command``/``port``, path/name) does not participate in the x509 handshake.
    Of the TLS-affecting set, ``no_verify`` and ``tls_priority`` are rejected here and
    ``pkipath`` in :func:`validate_remote_uri`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a non-``qemu+tls`` scheme, a ``no_verify``
            parameter (server-cert verification must stay on), or a ``tls_priority`` parameter
            (the ciphersuite selection must stay libvirt's default), in any casing or
            ``;``-separated spelling libvirt would accept.
    """
    try:
        parsed = urlsplit(uri)
        _ = parsed.hostname  # urllib defers hostname validation until this property is read.
    except ValueError as exc:
        raise CategorizedError(
            f"remote-libvirt URI {reportable_remote_uri(uri)!r} is malformed; "
            "configure a valid qemu+tls://host[:port]/system URI",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc
    try:
        _ = parsed.port
    except ValueError as exc:
        reportable = reportable_remote_uri(uri)
        raise CategorizedError(
            f"remote-libvirt URI {reportable!r} carries a port that is not an integer "
            "between 0 and 65535",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"uri": reportable},
        ) from exc
    if parsed.scheme != REQUIRED_REMOTE_SCHEME:
        raise CategorizedError(
            f"remote-libvirt URI {reportable_remote_uri(uri)!r} must use the qemu+tls:// scheme",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    names = _query_param_names(parsed.query)
    if "no_verify" in names:
        raise CategorizedError(
            "no_verify is forbidden on the remote-libvirt URI: server-cert "
            "verification is mandatory (ADR-0077)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    if "tls_priority" in names:
        raise CategorizedError(
            "tls_priority is forbidden on the remote-libvirt URI: it can name "
            "anonymous or weak GnuTLS ciphersuites (ADR-0077)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def validate_remote_uri(uri: str) -> None:
    """Reject any URI that would weaken mutual TLS (fail-closed, ADR-0077).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a non-``qemu+tls`` scheme, a
            ``no_verify`` parameter (server-cert verification must stay on), a
            ``tls_priority`` parameter (the ciphersuite selection must stay libvirt's
            default), or an operator-set ``pkipath`` (each op composes its own private
            pkipath) — in any casing or ``;``-separated spelling libvirt would accept.
    """
    validate_remote_transport(uri)
    names = _query_param_names(urlsplit(uri).query)
    if "pkipath" in names:
        raise CategorizedError(
            "pkipath must not be set on the remote-libvirt URI: each op "
            "materializes its own private pkipath (ADR-0077)",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
