"""Direct unit tests for reusable-build attempt fences."""

import asyncio
from typing import cast

from psycopg import AsyncConnection

from kdive.services.runs.build_use import release_build_use


def test_release_absent_build_use_is_a_noop() -> None:
    asyncio.run(release_build_use(cast(AsyncConnection, None), None))
