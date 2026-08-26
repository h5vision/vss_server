"""저장 계층 선택. VSS_STORE=chroma|pgvector. 코드의 나머지는 이 팩토리만 봅니다."""

from __future__ import annotations

import threading

from ..config import CFG
from .base import ProjectNotFound, StoreError, VectorStore, chunk_id  # noqa: F401

_LOCK = threading.Lock()
_STORE: VectorStore | None = None


def make_store(kind: str | None = None) -> VectorStore:
    k = (kind or CFG.store).lower()
    if k == "chroma":
        from .chroma import ChromaStore
        return ChromaStore()
    if k in ("pgvector", "pg", "postgres"):
        from .pgvector import PgVectorStore
        return PgVectorStore()
    raise StoreError(f"알 수 없는 VSS_STORE: {k!r} (chroma | pgvector)")


def get_store() -> VectorStore:
    """프로세스 전체가 공유하는 저장소. 매 요청마다 새로 열면 느립니다."""
    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = make_store()
        return _STORE


def set_store(store: VectorStore | None) -> None:
    global _STORE
    with _LOCK:
        _STORE = store
