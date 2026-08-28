"""PostgreSQL advisory lock used to serialize startup recovery runs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

LOCK_NAME = "vss_snapshot_startup_recovery"
TRY_LOCK_SQL = text(
    "SELECT pg_try_advisory_lock("
    "hashtextextended(current_database() || ':' || :lock_name, 0)"
    ")"
)
UNLOCK_SQL = text(
    "SELECT pg_advisory_unlock("
    "hashtextextended(current_database() || ':' || :lock_name, 0)"
    ")"
)


class RecoveryRunLock:
    """Keep one database-scoped startup recovery coordinator active."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[bool]:
        if self._engine.dialect.name != "postgresql":
            # SQLite는 단일 프로세스 로컬 fixture에만 사용한다. 운영 동시성 보장은 아래
            # PostgreSQL advisory lock 경계에서 검증한다.
            yield True
            return

        async with self._engine.connect() as connection:
            acquired = bool(
                await connection.scalar(TRY_LOCK_SQL, {"lock_name": LOCK_NAME})
            )
            # session-level advisory lock은 transaction commit 뒤에도 이 전용 connection이
            # 닫힐 때까지 유지된다. VSS 네트워크 조회 동안 DB transaction은 열어 두지 않는다.
            await connection.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    try:
                        await connection.execute(UNLOCK_SQL, {"lock_name": LOCK_NAME})
                        await connection.commit()
                    except BaseException:
                        # unlock이 취소·실패한 connection을 pool에 돌려주면 다음 사용자가 잠금을
                        # 물려받을 수 있으므로 반드시 폐기한다.
                        await connection.invalidate()
                        raise
