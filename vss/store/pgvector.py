"""
PostgreSQL + pgvector 저장 계층.

상태의 정본은 `rag.revisions` 행입니다.
    building   인덱싱 중이거나 중단됨 (조회 대상 아님)
    active     현재 서빙 중 (project 당 정확히 하나)
    retired    직전 인덱스 (rollback 용, chunks 는 keep_revisions 개수만 보관)
    failed     실패로 표시된 build

promote() 는 트랜잭션 하나입니다. Chroma 의 3단계 이름 변경을 흉내 낼 필요가 없습니다.
검증(VSS_PG_EXACT=1)에서는 HNSW 를 끄고 정확 검색으로 비교합니다.

스키마는 scripts/db_init.sql 이 만들지만, 없으면 ensure_schema() 가 만듭니다 (권한이 있을 때).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from ..config import CFG, normalize_fingerprint
from .base import ProjectNotFound, StoreError, chunk_id, hit_from_meta

DDL = """
CREATE SCHEMA IF NOT EXISTS {s};
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS {s}.projects (
    project_id      text PRIMARY KEY,
    active_revision bigint,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {s}.revisions (
    id           bigserial PRIMARY KEY,
    project_id   text NOT NULL,
    status       text NOT NULL CHECK (status IN ('building','active','retired','failed')),
    fingerprint  jsonb NOT NULL,
    meta         jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    chunk_count  integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    promoted_at  timestamptz
);
CREATE INDEX IF NOT EXISTS revisions_project_idx ON {s}.revisions (project_id, status);
CREATE TABLE IF NOT EXISTS {s}.chunks (
    revision_id  bigint NOT NULL REFERENCES {s}.revisions(id) ON DELETE CASCADE,
    chunk_id     text NOT NULL,
    path         text NOT NULL,
    type         text NOT NULL,
    line_start   integer,
    line_end     integer,
    section      text,
    symbol       text,
    chunk_index  integer NOT NULL DEFAULT 0,
    text         text NOT NULL,
    embedding    vector({dim}) NOT NULL,
    PRIMARY KEY (revision_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS chunks_path_idx ON {s}.chunks (revision_id, path);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON {s}.chunks USING hnsw (embedding vector_cosine_ops);
"""


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class PgVectorStore:
    kind = "pgvector"

    def __init__(self, dsn: str | None = None, schema: str | None = None, *,
                 keep_revisions: int = 1, ensure: bool = True):
        try:
            import psycopg
        except ImportError as e:      # pragma: no cover
            raise StoreError("psycopg 가 없습니다. pip install 'psycopg[binary]'") from e
        self._psycopg = psycopg
        self.dsn = dsn or CFG.pg_dsn
        self.s = schema or CFG.pg_schema
        self.keep_revisions = keep_revisions
        self.dim = CFG.embed_dim
        if ensure:
            self.ensure_schema()

    def _conn(self):
        return self._psycopg.connect(self.dsn, autocommit=False)

    def ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(DDL.format(s=self.s, dim=self.dim))
            conn.commit()

    # ── 상태 조회 ─────────────────────────────────────────────
    def _active(self, conn, project_id: str):
        row = conn.execute(
            f"SELECT r.id, r.fingerprint, r.meta, r.chunk_count, r.promoted_at, r.created_at "
            f"FROM {self.s}.projects p JOIN {self.s}.revisions r ON r.id = p.active_revision "
            f"WHERE p.project_id = %s", (project_id,)).fetchone()
        return row

    def projects(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT p.project_id FROM {self.s}.projects p "
                f"WHERE p.active_revision IS NOT NULL ORDER BY 1").fetchall()
        return [r[0] for r in rows]

    def incomplete(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT id, project_id, status, created_at, chunk_count FROM {self.s}.revisions "
                f"WHERE status IN ('building','failed') ORDER BY created_at").fetchall()
        now = time.time()
        return [{"name": f"building-{r[1]}#{r[0]}", "target": r[1], "status": r[2],
                 "chunks": r[4], "age_s": round(now - r[3].timestamp(), 1)} for r in rows]

    def count(self, project_id: str) -> int:
        with self._conn() as conn:
            row = self._active(conn, project_id)
            if not row:
                return 0
            n = conn.execute(f"SELECT count(*) FROM {self.s}.chunks WHERE revision_id=%s", (row[0],)).fetchone()
        return int(n[0])

    def project_info(self, project_id: str) -> dict | None:
        with self._conn() as conn:
            row = self._active(conn, project_id)
            if not row:
                return None
            n = conn.execute(f"SELECT count(*) FROM {self.s}.chunks WHERE revision_id=%s", (row[0],)).fetchone()
        info = dict(row[2] or {})
        info.update({"revision": row[0], "fingerprint": normalize_fingerprint(row[1]),
                     "chunks": int(n[0]), "status": "ready",
                     "promoted_at": row[4].timestamp() if row[4] else None,
                     "started_at": row[5].timestamp() if row[5] else None})
        return info

    def index_fingerprint(self, project_id: str) -> dict | None:
        with self._conn() as conn:
            row = self._active(conn, project_id)
        return normalize_fingerprint(row[1]) if row else None

    # ── 원자적 교체 ──────────────────────────────────────────
    def begin_build(self, project_id: str, *, fingerprint: dict, meta: dict | None = None) -> str:
        with self._conn() as conn:
            conn.execute(f"INSERT INTO {self.s}.projects (project_id, active_revision) VALUES (%s, NULL) "
                         f"ON CONFLICT (project_id) DO NOTHING", (project_id,))
            row = conn.execute(
                f"INSERT INTO {self.s}.revisions (project_id, status, fingerprint, meta) "
                f"VALUES (%s, 'building', %s::jsonb, %s::jsonb) RETURNING id",
                (project_id, json.dumps(fingerprint, ensure_ascii=False),
                 json.dumps(meta or {}, ensure_ascii=False, default=str))).fetchone()
            conn.commit()
        return str(row[0])

    def add(self, build: str, chunks: list[dict], vectors: list[list[float]], *, project_id: str) -> None:
        if not chunks:
            return
        rev = int(build)
        rows = [(rev, chunk_id(project_id, c, i), c["path"], c["type"],
                 c.get("line_start") or None, c.get("line_end") or None,
                 c.get("section") or None, c.get("symbol") or None, c.get("chunk_index", 0),
                 c["text"], _vec(v)) for i, (c, v) in enumerate(zip(chunks, vectors))]
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {self.s}.chunks (revision_id, chunk_id, path, type, line_start, line_end, "
                    f"section, symbol, chunk_index, text, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector) "
                    f"ON CONFLICT (revision_id, chunk_id) DO UPDATE SET text=EXCLUDED.text, embedding=EXCLUDED.embedding, "
                    f"path=EXCLUDED.path, type=EXCLUDED.type, line_start=EXCLUDED.line_start, line_end=EXCLUDED.line_end, "
                    f"section=EXCLUDED.section, symbol=EXCLUDED.symbol, chunk_index=EXCLUDED.chunk_index", rows)
                cur.execute(f"UPDATE {self.s}.revisions SET chunk_count = "
                            f"(SELECT count(*) FROM {self.s}.chunks WHERE revision_id=%s) WHERE id=%s", (rev, rev))
            conn.commit()

    def promote(self, project_id: str, build: str, *, meta: dict | None = None) -> None:
        rev = int(build)
        with self._conn() as conn:
            st = conn.execute(f"SELECT status, project_id FROM {self.s}.revisions WHERE id=%s", (rev,)).fetchone()
            if not st or st[1] != project_id:
                raise StoreError(f"승격할 revision 이 없습니다: {build}")
            if st[0] != "building":
                raise StoreError(f"revision {build} 상태가 building 이 아닙니다: {st[0]}")
            conn.execute(f"UPDATE {self.s}.revisions SET status='retired' WHERE project_id=%s AND status='active'",
                         (project_id,))
            conn.execute(
                f"UPDATE {self.s}.revisions SET status='active', promoted_at=now(), "
                f"meta = meta || %s::jsonb, chunk_count=(SELECT count(*) FROM {self.s}.chunks WHERE revision_id=%s) "
                f"WHERE id=%s", (json.dumps(meta or {}, ensure_ascii=False, default=str), rev, rev))
            conn.execute(f"UPDATE {self.s}.projects SET active_revision=%s, updated_at=now() WHERE project_id=%s",
                         (rev, project_id))
            # 오래된 retired revision 의 청크 정리 (직전 keep_revisions 개는 보관)
            old = conn.execute(
                f"SELECT id FROM {self.s}.revisions WHERE project_id=%s AND status='retired' "
                f"ORDER BY promoted_at DESC NULLS LAST OFFSET %s", (project_id, self.keep_revisions)).fetchall()
            for (oid,) in old:
                conn.execute(f"DELETE FROM {self.s}.revisions WHERE id=%s", (oid,))
            conn.commit()

    def abandon_build(self, project_id: str, build: str | None = None) -> int:
        with self._conn() as conn:
            if build:
                cur = conn.execute(f"DELETE FROM {self.s}.revisions WHERE id=%s AND project_id=%s "
                                   f"AND status IN ('building','failed')", (int(build), project_id))
            else:
                cur = conn.execute(f"DELETE FROM {self.s}.revisions WHERE project_id=%s "
                                   f"AND status IN ('building','failed')", (project_id,))
            n = cur.rowcount
            conn.commit()
        return n

    def mark_failed(self, build: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(f"UPDATE {self.s}.revisions SET status='failed', meta = meta || %s::jsonb WHERE id=%s",
                         (json.dumps({"error": error[:500]}), int(build)))
            conn.commit()

    def drop(self, project_id: str) -> None:
        with self._conn() as conn:
            conn.execute(f"DELETE FROM {self.s}.revisions WHERE project_id=%s", (project_id,))
            conn.execute(f"DELETE FROM {self.s}.projects WHERE project_id=%s", (project_id,))
            conn.commit()

    # ── 조회 ─────────────────────────────────────────────────
    _COLS = "chunk_id, path, type, line_start, line_end, section, symbol, text"

    def _row_hit(self, r, score: float) -> dict:
        return hit_from_meta(r[0], r[7], {"path": r[1], "type": r[2], "line_start": r[3],
                                           "line_end": r[4], "section": r[5], "symbol": r[6]}, score)

    def query(self, project_id: str, vector: list[float], top_k: int) -> list[dict]:
        v = _vec(vector)
        with self._conn() as conn:
            row = self._active(conn, project_id)
            if not row:
                raise ProjectNotFound(f"인덱싱된 project_id 가 아닙니다: {project_id!r}")
            if CFG.pg_exact:
                conn.execute("SET LOCAL enable_indexscan = off")
            else:
                conn.execute(f"SET LOCAL hnsw.ef_search = {max(100, top_k * 4)}")
            rows = conn.execute(
                f"SELECT {self._COLS}, 1 - (embedding <=> %s::vector) AS score FROM {self.s}.chunks "
                f"WHERE revision_id=%s ORDER BY embedding <=> %s::vector LIMIT %s",
                (v, row[0], v, top_k)).fetchall()
            conn.rollback()
        return [self._row_hit(r, r[8]) for r in rows]

    def get_by_ids(self, project_id: str, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        with self._conn() as conn:
            row = self._active(conn, project_id)
            if not row:
                raise ProjectNotFound(project_id)
            rows = conn.execute(f"SELECT {self._COLS} FROM {self.s}.chunks WHERE revision_id=%s AND chunk_id = ANY(%s)",
                                (row[0], ids)).fetchall()
        return {r[0]: self._row_hit(r, 0.0) for r in rows}

    def iter_chunks(self, project_id: str, *, batch_size: int = 500) -> Iterator[dict]:
        with self._conn() as conn:
            row = self._active(conn, project_id)
            if not row:
                raise ProjectNotFound(project_id)
            rev = row[0]
            total = conn.execute(f"SELECT count(*) FROM {self.s}.chunks WHERE revision_id=%s", (rev,)).fetchone()[0]
            seen = 0
            with conn.cursor(name="vss_iter") as cur:      # 서버 사이드 커서
                cur.itersize = batch_size
                cur.execute(f"SELECT {self._COLS} FROM {self.s}.chunks WHERE revision_id=%s ORDER BY path, chunk_index", (rev,))
                for r in cur:
                    seen += 1
                    yield self._row_hit(r, 0.0)
            if seen != total:
                raise StoreError(f"{project_id}: 순회 건수 불일치 expected={total} actual={seen}")
