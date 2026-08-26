"""
벡터 저장 계층 인터페이스.

불변 조건 (rag_lab 사고에서 나온 규칙 — DECISIONS 참조)
  1. 전체 인덱싱은 선삭제하지 않는다. begin_build() → add() → promote() 순서로만 교체한다.
  2. 인덱스 상태의 정본은 저장소 자신이다 (Chroma 컬렉션 이름 / PostgreSQL revisions 행). 별도 상태 파일이 없다.
  3. 실패한 build 는 자동으로 지우지 않는다 (중단의 증거). abandon_build() 로 명시적으로 지운다.
  4. query() 의 score 는 cosine similarity (1 - distance) 이다.

hit 레코드: {_id, text, path, type, line_start, line_end, section, symbol, score}
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol


class StoreError(RuntimeError):
    pass


class ProjectNotFound(StoreError, LookupError):
    pass


class VectorStore(Protocol):
    kind: str

    def projects(self) -> list[str]: ...
    def incomplete(self) -> list[dict]: ...
    def count(self, project_id: str) -> int: ...
    def project_info(self, project_id: str) -> dict | None: ...
    def index_fingerprint(self, project_id: str) -> dict | None: ...
    def begin_build(self, project_id: str, *, fingerprint: dict, meta: dict | None = None) -> str: ...
    def add(self, build: str, chunks: list[dict], vectors: list[list[float]], *, project_id: str) -> None: ...
    def promote(self, project_id: str, build: str, *, meta: dict | None = None) -> None: ...
    def abandon_build(self, project_id: str, build: str | None = None) -> int: ...
    def query(self, project_id: str, vector: list[float], top_k: int) -> list[dict]: ...
    def get_by_ids(self, project_id: str, ids: list[str]) -> dict[str, dict]: ...
    def iter_chunks(self, project_id: str, *, batch_size: int = 500) -> Iterator[dict]: ...
    def drop(self, project_id: str) -> None: ...


def chunk_id(project_id: str, chunk: dict, fallback: int = 0) -> str:
    """경로 + 파일 내 순번 기반 안정 ID. 컬렉션 이름이 임시(building-)여도 ID 는 최종 project_id 기준."""
    idx = chunk.get("chunk_index", fallback)
    return f"{project_id}:{chunk.get('path', '?')}:{idx}"


def hit_from_meta(cid: str, text: str, meta: dict, score: float) -> dict:
    meta = meta or {}
    return {
        "_id": cid,
        "text": text,
        "path": meta.get("path", ""),
        "type": meta.get("type", "code"),
        "line_start": meta.get("line_start") or None,
        "line_end": meta.get("line_end") or None,
        "section": meta.get("section") or None,
        "symbol": meta.get("symbol") or None,
        "score": float(score),
    }
