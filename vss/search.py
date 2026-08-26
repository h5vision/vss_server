"""
검색 — 벡터 top-k · (선택) BM25 RRF 융합 · 임계값 판정 (rag_lab searcher.search 이식 — SALVAGE.md).

불변식
  · 융합은 순서만 바꾼다. "근거가 있는가" 판정은 벡터 점수로만 한다 (BM25 로만 올라온 청크는 score 0).
  · top_score = pool 안의 최대 벡터 점수. 따라서 `top_score >= threshold ⟺ has_evidence`.
  · contexts 순서가 곧 프롬프트의 [N]. 이 배열을 정렬·필터링하지 않는다.
  · 질의 임베딩은 그 인덱스가 저장한 fingerprint 의 모델·차원을 쓴다 (현재 CFG 가 아님).
"""

from __future__ import annotations

import time
from typing import Mapping

from . import lexical
from .config import CFG
from .embedder import embed_one
from .store import ProjectNotFound, VectorStore, get_store


def serving_profile(store: VectorStore, project_id: str) -> dict:
    if project_id not in store.projects():
        raise ProjectNotFound(
            f"인덱싱된 project_id 가 아닙니다: {project_id!r}; available={', '.join(store.projects()) or '(없음)'}")
    fp = store.index_fingerprint(project_id)
    if not fp:
        raise RuntimeError(f"인덱스 설정 지문을 확인할 수 없습니다: {project_id!r}. 재인덱싱이 필요합니다.")
    return fp


def search(query: str, project_id: str, *, top_k: int | None = None,
           threshold: float | None = None, store: VectorStore | None = None,
           search_profile: Mapping | None = None, embed_text: str | None = None) -> dict:
    """embed_text 를 주면 임베딩에는 그것을, BM25 에는 query 를 씁니다 (선택 코드 첨부 시)."""
    k = int(top_k if top_k is not None else CFG.top_k)
    th = float(threshold if threshold is not None else CFG.score_threshold)
    st = store or get_store()
    profile = serving_profile(st, project_id)
    options = dict(search_profile or {})

    use_bm25 = bool(options.get("use_bm25", profile.get("use_bm25", False)))
    pool = int(options.get("pool", CFG.fusion_pool)) if use_bm25 else k
    pool = max(pool, k)

    t0 = time.perf_counter()
    vec = embed_one(embed_text or query, model=str(profile["embed_model"]),
                    expected_dim=int(profile["embed_dim"]))
    t1 = time.perf_counter()
    hits = st.query(project_id, vec, pool)
    t2 = time.perf_counter()
    timing = {"embed_ms": round((t1 - t0) * 1000, 1), "search_ms": round((t2 - t1) * 1000, 1)}
    sp = {"use_bm25": use_bm25, "pool": pool, "top_k": k, "threshold": th}

    if not hits:
        return {"has_evidence": False, "contexts": [], "all_hits": [], "top_score": None,
                "ranked1_score": None, "threshold": th, "reason": "empty_index", "timing": timing,
                "serving_profile": profile, "search_profile": sp, "bm25_active": False}

    bm25_active = False
    if use_bm25:
        t_bm = time.perf_counter()
        idx = _bm25_cache(project_id, st)
        if idx is not None:
            bm25_active = True
            lex = idx.search(query, pool)
            fused = lexical.rrf_fuse(hits, lex, k=int(options.get("rrf_k", CFG.rrf_k)))
            by_id = {h["_id"]: h for h in hits}
            missing = [i for i, _ in lex if i not in by_id]
            if missing:
                by_id.update(st.get_by_ids(project_id, missing[:pool]))
            hits = sorted((by_id[i] for i in fused if i in by_id), key=lambda h: -fused[h["_id"]])
        timing["bm25_ms"] = round((time.perf_counter() - t_bm) * 1000, 1)

    top = max(h["score"] for h in hits)              # pool 안의 최대 벡터 점수
    ranked1 = hits[0]["score"]
    passed = [h for h in hits if h["score"] >= th][:k]
    return {
        "has_evidence": bool(passed),
        "contexts": passed,
        "all_hits": hits[:max(k * 2, 10)],
        "top_score": top,
        "ranked1_score": ranked1,
        "threshold": th,
        "reason": "ok" if passed else "below_threshold",
        "timing": timing,
        "serving_profile": profile,
        "search_profile": sp,
        "bm25_active": bm25_active,
    }


# BM25 는 요청마다 JSON 을 파싱하면 느리므로 파일 mtime 기준으로 캐시합니다.
_BM25: dict[str, tuple[float, object]] = {}


def _bm25_cache(project_id: str, st: VectorStore):
    p = lexical.index_path(project_id)
    if not p.exists():
        return None
    mtime = p.stat().st_mtime
    cached = _BM25.get(project_id)
    if cached and cached[0] == mtime:
        return cached[1]
    idx = lexical.BM25.load(p)
    if idx is None:
        return None
    n = st.count(project_id)
    if len(idx.doc_ids) != n:
        # 역색인이 인덱스와 다르면 하이브리드를 조용히 켜지 않습니다.
        print(f"!! {project_id}: BM25 문서 수({len(idx.doc_ids)}) ≠ 청크 수({n}) — 융합 비활성. "
              f"python -m vss.cli bm25 --project {project_id}")
        return None
    _BM25[project_id] = (mtime, idx)
    return idx


def invalidate_bm25(project_id: str | None = None) -> None:
    if project_id is None:
        _BM25.clear()
    else:
        _BM25.pop(project_id, None)
