"""Fixtures for build-config catalog and seed unit tests (ADR-0096).

The seed's behavior is now exercised DB-backed (``test_seed_db.py``) because the
source-guarded seed upsert needs a real ``pg_advisory_xact_lock`` (ADR-0119); the former
fake-conn / fake-store doubles were retired with the fake-conn seed tests.
"""

from __future__ import annotations
