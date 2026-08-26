"""
BM25 어휘 검색 — 벡터 검색을 보완합니다 (rag_lab lexical.py 복사본, SALVAGE.md 참조).

⚠ 왜 필요한가

    벡터 검색은 **의미**를 잡지만 **정확한 이름**에 약합니다.

        질문: "validate_token 함수는 뭘 하나요?"

        벡터:  "토큰 검증" 개념이 비슷한 것을 다 가져옴
               → verify_signature, check_auth, is_valid ... 가 섞임

        BM25: "validate_token" 이라는 문자열을 정확히 찾음
               → 그 함수를 집어냄

    코드에는 고유명사(함수명·변수명·클래스명)가 많아서, 정확 매칭이
    의미 검색보다 나을 때가 자주 있습니다.

⚠ RAG 스파이크에서 "BM25 단독으로는 임계값 분리가 불충분" 이라는
   결론이 나왔지만, 그것은 **BM25 만 쓸 때** 이야기입니다.
   (⚠ 2026-07-27 이라는 날짜는 무효입니다 — 해당 run 이 이력에 없습니다.
    수치·run_id 는 MEASUREMENTS §1-4 참조)
   벡터와 **합치는 것**이 일반적인 방식이며, 이 모듈은 그 용도입니다.

⚠ 순수 표준 라이브러리로 구현했습니다. 외부 의존이 없습니다.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

from .config import CFG


class BM25BuildError(RuntimeError):
    """입력 청크가 완전하지 않아 BM25 파일을 만들 수 없습니다."""

# ── 토크나이저 ───────────────────────────────────────────────
# 코드 검색에 맞춘 규칙입니다.
#   · snake_case, camelCase 를 조각으로도 나눕니다
#     ("validate_token" → validate_token, validate, token)
#     그래야 "token 검증" 같은 질문도 걸립니다
#   · 한글은 2글자 이상 덩어리로
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[가-힣]{2,}|\d+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall(text.lower() if text else ""):
        out.append(w)
        # snake_case 분해
        if "_" in w:
            out.extend(p for p in w.split("_") if len(p) > 1)
        # camelCase 분해 (원문 기준이 아니라 소문자화 후라 제한적)
    # camelCase 는 원문에서 별도 추출
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text or ""):
        parts = _CAMEL.findall(w)
        if len(parts) > 1:
            out.extend(p.lower() for p in parts if len(p) > 1)
    return out


class BM25:
    """
    BM25 역색인. 메모리에 올려두고 검색합니다.

    ⚠ 청크 21,000개 기준 메모리 수십 MB 수준입니다.
       그보다 크게 늘어나면 디스크 기반으로 바꿔야 합니다.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_ids: list[str] = []
        self.doc_len: list[int] = []
        self.tf: list[dict[str, int]] = []
        self.df: Counter = Counter()
        self.avg_len: float = 0.0

    def add(self, doc_id: str, text: str) -> None:
        toks = tokenize(text)
        counts = Counter(toks)
        self.doc_ids.append(doc_id)
        self.doc_len.append(len(toks))
        self.tf.append(dict(counts))
        for t in counts:
            self.df[t] += 1

    def finalize(self) -> None:
        self.avg_len = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0

    def search(self, query: str, top_k: int) -> list[tuple[str, float]]:
        if not self.doc_ids:
            return []
        q = tokenize(query)
        if not q:
            return []
        N = len(self.doc_ids)
        scores: dict[int, float] = {}

        for term in set(q):
            df = self.df.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
            for i, tfs in enumerate(self.tf):
                f = tfs.get(term)
                if not f:
                    continue
                dl = self.doc_len[i] or 1
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avg_len or 1))
                scores[i] = scores.get(i, 0.0) + idf * (f * (self.k1 + 1)) / denom

        ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        return [(self.doc_ids[i], s) for i, s in ranked]

    # ── 저장 / 로드 ──────────────────────────────────────────
    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "k1": self.k1, "b": self.b,
            "doc_ids": self.doc_ids, "doc_len": self.doc_len,
            "tf": self.tf, "df": dict(self.df), "avg_len": self.avg_len,
        }, ensure_ascii=False)
        # 반쯤 써진 JSON이 정상 역색인처럼 남지 않도록 같은 디렉터리에서 원자 교체합니다.
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(p)

    @classmethod
    def load(cls, path: str | Path) -> "BM25 | None":
        p = Path(path)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        o = cls(d.get("k1", 1.5), d.get("b", 0.75))
        o.doc_ids = d["doc_ids"]
        o.doc_len = d["doc_len"]
        o.tf = d["tf"]
        o.df = Counter(d["df"])
        o.avg_len = d["avg_len"]
        return o


def index_path(project_id: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", project_id)
    return CFG.bm25_dir() / f"{safe}.json"


def staging_path(project_id: str) -> Path:
    """전체 인덱싱 중 검증을 마치기 전까지 쓸 BM25 파일."""
    final = index_path(project_id)
    return final.with_name(f"building-{final.name}")


def build(project_id: str, chunks: Iterable[Mapping], *,
          path: str | Path | None = None,
          expected_count: int | None = None) -> BM25:
    """청크 iterable로 역색인을 만들고 검증이 끝난 뒤에만 저장합니다.

    Store의 페이지 generator가 중간에 실패하면 ``save``까지 도달하지 않으므로
    기존 파일 또는 staging 파일을 빈/부분 BM25로 덮어쓰지 않습니다.
    """
    idx = BM25()
    seen: set[str] = set()
    for c in chunks:
        if not isinstance(c, Mapping):
            raise BM25BuildError(
                f"BM25 청크 형식 오류: project_id={project_id!r}, "
                f"type={type(c).__name__}")
        raw_id = c.get("_id")
        if raw_id is None or not str(raw_id):
            raise BM25BuildError(
                f"BM25 청크 ID가 없습니다: project_id={project_id!r}")
        doc_id = str(raw_id)
        if doc_id in seen:
            raise BM25BuildError(
                f"BM25 청크 ID 중복: project_id={project_id!r}, id={doc_id!r}")
        seen.add(doc_id)
        # 경로도 색인에 넣습니다 — "payment 관련 파일" 같은 질문에 걸리도록
        body = f"{c.get('path','')} {c.get('section') or ''} {c.get('text','')}"
        idx.add(doc_id, body)
    if expected_count is not None and len(idx.doc_ids) != expected_count:
        raise BM25BuildError(
            f"BM25 문서 수 불일치: project_id={project_id!r}, "
            f"expected={expected_count}, actual={len(idx.doc_ids)}")
    idx.finalize()
    idx.save(path or index_path(project_id))
    return idx


def doc_count(project_id: str) -> int | None:
    idx = BM25.load(index_path(project_id))
    return len(idx.doc_ids) if idx is not None else None


# ── 점수 합치기 ──────────────────────────────────────────────

def rrf_fuse(vector_hits: list[dict], lexical_hits: list[tuple[str, float]],
             k: int = 60) -> dict[str, float]:
    """
    Reciprocal Rank Fusion — 두 검색 결과를 순위 기반으로 합칩니다.

    ⚠ 점수를 직접 더하지 않는 이유
       cosine 유사도(0~1)와 BM25 점수(0~30+)는 **척도가 다릅니다.**
       정규화해서 더하면 한쪽이 압도하거나 분포가 왜곡됩니다.
       순위만 쓰면 척도 문제가 사라집니다.

           score = Σ  1 / (k + rank)

    ⚠ 단점: 원래 점수(cosine)가 사라져 **임계값 판정을 못 합니다.**
       그래서 임계값은 벡터 점수로 따로 유지합니다 (searcher 참조).
    """
    fused: dict[str, float] = {}
    for rank, h in enumerate(vector_hits, start=1):
        fused[h["_id"]] = fused.get(h["_id"], 0.0) + 1.0 / (k + rank)
    for rank, (doc_id, _) in enumerate(lexical_hits, start=1):
        fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return fused
