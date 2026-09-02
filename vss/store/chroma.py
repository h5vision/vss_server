"""
Chroma 저장 계층 (rag_lab store.py 의 원자적 교체 규약 이식 — SALVAGE.md).

컬렉션 이름이 곧 상태입니다.
    building-<pid>   인덱싱 중이거나 중단됨. 조회 대상 아님
    <pid>-prev       교체 도중의 백업
    <pid>            완성된 인덱스. 이것만 조회됨

promote() 는 3단계(기존→prev, building→pid, prev 삭제)라 어느 시점에 죽어도 데이터가 한 벌 남습니다.
Chroma 이름 규칙(ASCII 시작, 63자)상 project_id 는 최대 54자입니다.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

from ..config import CFG, normalize_fingerprint
from .base import ProjectNotFound, StoreError, chunk_id, enclosing_list, hit_from_meta

BUILD_PREFIX = "building-"
PREV_SUFFIX = "-prev"
MAX_PROJECT_ID = 63 - len(BUILD_PREFIX)


def is_internal(name: str) -> bool:
    return name.startswith(BUILD_PREFIX) or name.endswith(PREV_SUFFIX)


class ChromaStore:
    kind = "chroma"

    def __init__(self, index_dir: str | Path | None = None):
        try:
            import chromadb
        except ImportError as e:      # pragma: no cover
            raise StoreError("chromadb 가 없습니다. pip install chromadb") from e
        path = Path(index_dir or CFG.index_dir()).resolve()
        path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(path))

    # ── 조회 도우미 (아무것도 생성하지 않습니다) ────────────────
    def _names(self) -> set[str]:
        return {c.name for c in self._client.list_collections()}

    def _get(self, name: str):
        """없으면 예외. get_or_create 로 빈 컬렉션이 조용히 생기는 것을 막습니다."""
        try:
            return self._client.get_collection(name)
        except Exception as e:
            raise ProjectNotFound(f"인덱스가 없습니다: {name!r}") from e

    def _meta(self, name: str) -> dict | None:
        for c in self._client.list_collections():
            if c.name == name:
                return dict(c.metadata or {})
        return None

    def projects(self) -> list[str]:
        return sorted(n for n in self._names() if not is_internal(n))

    def incomplete(self) -> list[dict]:
        out = []
        for c in self._client.list_collections():
            if not is_internal(c.name):
                continue
            meta = dict(c.metadata or {})
            started = meta.get("started_at")
            out.append({"name": c.name, "chunks": c.count(),
                        "target": meta.get("target"),
                        "age_s": round(time.time() - float(started), 1) if started else None})
        return out

    def count(self, project_id: str) -> int:
        if project_id not in self._names():
            return 0
        return self._get(project_id).count()

    def project_info(self, project_id: str) -> dict | None:
        meta = self._meta(project_id)
        if meta is None or is_internal(project_id):
            return None
        info = {k: v for k, v in meta.items() if k not in ("hnsw:space", "fingerprint")}
        info["fingerprint"] = self.index_fingerprint(project_id)
        info["chunks"] = self.count(project_id)
        return info

    def index_fingerprint(self, project_id: str) -> dict | None:
        meta = self._meta(project_id)
        raw = (meta or {}).get("fingerprint")
        if not raw:
            return None
        try:
            return normalize_fingerprint(json.loads(raw) if isinstance(raw, str) else dict(raw))
        except Exception:
            return None

    # ── 원자적 교체 ──────────────────────────────────────────
    def begin_build(self, project_id: str, *, fingerprint: dict, meta: dict | None = None) -> str:
        if len(project_id) > MAX_PROJECT_ID:
            raise StoreError(f"project_id 가 {MAX_PROJECT_ID}자를 넘습니다: {len(project_id)}자")
        name = BUILD_PREFIX + project_id
        if name in self._names():
            # 이전 시도의 잔재. 새 빌드가 명시적으로 시작됐으므로 교체합니다 (조회 대상이 아닌 임시본).
            self._client.delete_collection(name)
        m = {"hnsw:space": "cosine", "status": "building", "target": project_id,
             "started_at": time.time(),
             "fingerprint": json.dumps(fingerprint, ensure_ascii=False)}
        for k, v in (meta or {}).items():
            if v is None:
                continue
            m[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)
        self._client.create_collection(name=name, metadata=m)
        return name

    def add(self, build: str, chunks: list[dict], vectors: list[list[float]], *, project_id: str) -> None:
        if not chunks:
            return
        col = self._get(build)
        col.upsert(
            ids=[chunk_id(project_id, c, i) for i, c in enumerate(chunks)],
            embeddings=vectors,
            documents=[c["text"] for c in chunks],
            metadatas=[{
                "path": c["path"], "type": c["type"],
                "line_start": c.get("line_start") or 0, "line_end": c.get("line_end") or 0,
                "section": c.get("section") or "", "symbol": c.get("symbol") or "",
                "kind": c.get("kind") or "",
                # Chroma(1.5.9)는 list 를 받지만 **빈 list 는 거부한다**(ValueError: to be non-empty).
                # enclosing 이 비는 청크(모듈 최상위·문서·줄 윈도우)가 흔해서 JSON 문자열로 담는다.
                # pgvector 는 text[] 로 담으므로, 읽을 때 base.enclosing_list() 가 양쪽을 list 로 맞춘다.
                "enclosing": json.dumps(enclosing_list(c.get("enclosing")), ensure_ascii=False),
                "chunk_index": c.get("chunk_index", 0),
            } for c in chunks],
        )

    def promote(self, project_id: str, build: str, *, meta: dict | None = None) -> None:
        prev = project_id + PREV_SUFFIX
        names = self._names()
        if build not in names:
            raise StoreError(f"승격할 임시 컬렉션이 없습니다: {build}")
        if prev in names:
            self._client.delete_collection(prev)
        if project_id in names:                                             # ① 기존 보존
            self._client.get_collection(project_id).modify(name=prev)
        self._client.get_collection(build).modify(name=project_id)         # ② 승격
        try:
            col = self._client.get_collection(project_id)
            m = dict(col.metadata or {})
            m.pop("hnsw:space", None)
            m.update({"status": "ready", "promoted_at": time.time()})
            for k, v in (meta or {}).items():
                if v is None:
                    continue
                m[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)
            col.modify(metadata=m)
        except Exception as e:                                              # pragma: no cover
            print(f"!! 컬렉션 metadata 갱신 실패 ({type(e).__name__}: {e}) — 인덱스 자체는 정상입니다.")
        if prev in self._names():                                           # ③ 백업 제거
            self._client.delete_collection(prev)

    def abandon_build(self, project_id: str, build: str | None = None) -> int:
        name = build or (BUILD_PREFIX + project_id)
        if name in self._names():
            self._client.delete_collection(name)
            return 1
        return 0

    def drop(self, project_id: str) -> None:
        for name in (project_id, BUILD_PREFIX + project_id, project_id + PREV_SUFFIX):
            if name in self._names():
                self._client.delete_collection(name)

    # ── 조회 ─────────────────────────────────────────────────
    def query(self, project_id: str, vector: list[float], top_k: int) -> list[dict]:
        if project_id not in self._names() or is_internal(project_id):
            raise ProjectNotFound(f"인덱싱된 project_id 가 아닙니다: {project_id!r}")
        col = self._get(project_id)
        n = col.count()
        if n == 0:
            return []
        res = col.query(query_embeddings=[vector], n_results=min(top_k, n),
                        include=["documents", "metadatas", "distances"])
        ids = res.get("ids", [[]])[0]
        out = []
        for cid, doc, meta, dist in zip(ids, res["documents"][0], res["metadatas"][0], res["distances"][0]):
            out.append(hit_from_meta(cid, doc, meta, 1.0 - float(dist)))   # 거리 → 유사도
        return out

    def get_by_ids(self, project_id: str, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        col = self._get(project_id)
        res = col.get(ids=ids, include=["documents", "metadatas"])
        out = {}
        for cid, doc, meta in zip(res.get("ids", []), res.get("documents", []), res.get("metadatas", [])):
            out[cid] = hit_from_meta(cid, doc, meta, 0.0)
        return out

    def iter_chunks(self, project_id: str, *, batch_size: int = 500) -> Iterator[dict]:
        """페이지 순회. 건수 변동·중복·응답 길이 불일치는 예외로 드러납니다 (빈 목록으로 숨기지 않음)."""
        col = self._get(project_id)
        total = col.count()
        seen: set[str] = set()
        offset = 0
        while offset < total:
            res = col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
            ids = res.get("ids", [])
            docs = res.get("documents", [])
            metas = res.get("metadatas", [])
            if not ids:
                break
            if not (len(ids) == len(docs) == len(metas)):
                raise StoreError(f"{project_id}: 페이지 응답 길이 불일치 offset={offset}")
            for cid, doc, meta in zip(ids, docs, metas):
                if cid in seen:
                    raise StoreError(f"{project_id}: 청크 ID 중복 {cid}")
                seen.add(cid)
                yield hit_from_meta(cid, doc, meta, 0.0)
            offset += len(ids)
        if col.count() != total:
            raise StoreError(f"{project_id}: 순회 중 청크 수가 바뀌었습니다 ({total} → {col.count()})")
        if len(seen) != total:
            raise StoreError(f"{project_id}: 순회 건수 불일치 expected={total} actual={len(seen)}")
