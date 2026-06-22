"""The reconciler's inventory pass: reconcile ``systems.toml`` into the catalog (ADR-0112).

This is the loop trigger of the M2.6 inventory engine (#391/#393). Each pass resolves the
inventory path via :func:`kdive.inventory.path.systems_toml_path` (``KDIVE_SYSTEMS_TOML``, else
the XDG default ``~/.config/kdive/systems.toml``) and reconciles it through the one ordered
chain (:func:`kdive.inventory.reconcile_pipeline.reconcile_all`): into ``image_catalog`` via
:func:`kdive.inventory.reconcile_images.reconcile_images`, prices ``cost_class_coefficients``
via :func:`kdive.inventory.reconcile_coefficients.reconcile_coefficients` run **before** the
resource pass (ADR-0115), into ``resources`` via
:func:`kdive.inventory.reconcile_resources.reconcile_resources` (the fault-inject/remote
config overlay that supplies the sizing #385 lacked), and into ``build_hosts`` via
:func:`kdive.inventory.reconcile_build_hosts.reconcile_build_hosts`.

Two load-bearing invariants (plan Task 1.6):

* **Absent default file = quiet no-op.** ``systems.toml`` is gitignored, so an absent file is
  the normal pre-config state; :func:`kdive.inventory.load_inventory_optional` returns ``None``
  and the pass does nothing and records **no** failure. Feeding an empty document to
  ``reconcile_images`` would prune every config row, so an absent file must short-circuit
  *before* the reconcile step, not parse to an empty doc.
* **Drift repair is NOT gated on the file hash.** This is the ADR-0021 drift-repair spec: it
  must repair DB drift (a config-owned row manually deleted/corrupted) even when the file is
  unchanged. The content-hash cache therefore only skips the *parse/validate* step (caching
  the last-good :class:`~kdive.inventory.model.InventoryDoc` keyed by the file's hash); the
  reconcile-against-DB step runs **every** pass. With #390's change-detecting upserts a
  no-drift pass is cheap (reads + diff, no writes).

A present-but-malformed file raises :class:`~kdive.inventory.InventoryError`; the pass logs and
re-raises so the loop's per-repair ``try/except`` records it as a failed-this-pass spec while
sibling reaper repairs keep running. It never raises out of ``reconcile_once``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from psycopg import AsyncConnection

import kdive.config as config
from kdive.config.core_settings import SYSTEMS_TOML
from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.model import InventoryDoc
from kdive.inventory.path import systems_toml_path
from kdive.inventory.reconcile import ReconcileDiff
from kdive.inventory.reconcile_images import ImageHeadStore
from kdive.inventory.reconcile_pipeline import reconcile_all

_log = logging.getLogger(__name__)


def _changes(diff: ReconcileDiff) -> int:
    return len(diff.created) + len(diff.updated) + len(diff.pruned) + len(diff.cordoned)


def _cwd_inventory_shadowed(resolved: Path) -> bool:
    """True when a repo-relative ``./systems.toml`` is present but no longer auto-loaded.

    ADR-0112 removed the working-directory-relative inventory fallback: the path is now
    ``KDIVE_SYSTEMS_TOML`` else the XDG default. An operator who relied on a repo-root
    ``./systems.toml`` upgrades into a silent no-op. This detects exactly that case — the
    var is unset, the XDG default does not exist, and a ``./systems.toml`` sits in the CWD —
    so the reconciler can warn once instead of reconciling nothing in silence.
    """
    return (
        config.get(SYSTEMS_TOML) is None and not resolved.exists() and Path("systems.toml").exists()
    )


class InventoryReconcilePass:
    """A stateful inventory reconcile pass that caches the last-good parse by file hash.

    The cache lets a steady-state pass skip the parse/validate step (the file is unchanged),
    but the reconcile-against-DB step still runs every pass so DB drift is repaired even when
    the file has not changed (ADR-0021). One instance is held per process by the reconciler;
    a missing file clears the cache so a later re-creation re-parses.
    """

    def __init__(self) -> None:
        self._cached_hash: str | None = None
        self._cached_doc: InventoryDoc | None = None
        # Process-lifetime, NOT parse-cache state: a CWD-shadow warning is emitted at most once
        # per instance. Deliberately kept out of reset() — reset() fires every pass while the
        # file is absent (the shadow condition itself), so clearing it there would re-warn each
        # loop and defeat the once-only guard.
        self._cwd_shadow_warned = False

    def reset(self) -> None:
        self._cached_hash = None
        self._cached_doc = None

    def make_repair(self, store: ImageHeadStore) -> Callable[[AsyncConnection], Awaitable[int]]:
        async def _repair(conn: AsyncConnection) -> int:
            return await self.run(conn, store)

        return _repair

    async def run(self, conn: AsyncConnection, store: ImageHeadStore) -> int:
        """Reconcile the inventory file into the catalog; return the count of changes.

        Args:
            conn: A fresh, transaction-free pooled connection (``reconcile_images`` owns it).
            store: The object store, used only to HEAD ``s3`` objects.

        Returns:
            The number of catalog rows created/updated/pruned/cordoned this pass (``0`` when
            the file is absent — a quiet no-op).

        Raises:
            InventoryError: The file is present but malformed/invalid (logged then re-raised
                so the loop records this pass as failed without aborting siblings).
        """
        path = systems_toml_path()
        if not self._cwd_shadow_warned and _cwd_inventory_shadowed(path):
            _log.warning(
                "inventory: ./systems.toml exists but KDIVE_SYSTEMS_TOML is unset; it is no "
                "longer auto-loaded (ADR-0112). Move it to %s or set KDIVE_SYSTEMS_TOML.",
                path,
            )
            self._cwd_shadow_warned = True
        doc = self._load(path)
        if doc is None:
            return 0
        diff = await reconcile_all(conn, doc, store)
        return _changes(diff)

    def _load(self, path: Path) -> InventoryDoc | None:
        """Return the parsed doc (from cache when the file is unchanged), or ``None`` if absent.

        Raises:
            InventoryError: The file is present but unreadable/malformed/invalid.
        """
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            self.reset()  # an absent file invalidates any cached parse
            return None
        except OSError:
            # Present but unreadable (permissions, is-a-directory, …): defer to the loader so
            # the failure surfaces as an InventoryError, not a bare OSError. Falling through to
            # load_inventory_optional below re-attempts the read there and wraps the error.
            return self._parse(path)
        digest = hashlib.sha256(raw).hexdigest()
        if digest == self._cached_hash and self._cached_doc is not None:
            return self._cached_doc
        doc = self._parse(path)
        if doc is None:
            # The file vanished between the hash read above and the loader's read (a rare
            # mid-pass delete); treat it as an absent-file no-op and drop any cached parse.
            self.reset()
            return None
        self._cached_hash = digest
        self._cached_doc = doc
        return doc

    def _parse(self, path: Path) -> InventoryDoc | None:
        try:
            return load_inventory_optional(path)
        except InventoryError:
            _log.warning("inventory: %s is present but malformed; pass failed this iteration", path)
            raise
