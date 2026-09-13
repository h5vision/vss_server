"""
휴리스틱 재정렬 — 순서만 바꿉니다 (BM25 융합·심볼 재정렬과 같은 자리, 같은 불변식).

규칙 둘. 근거는 9/4 run 두 건의 재집계입니다 (docs/ACCURACY.md, 2026-09-07 세션).
  1. 경로 감점   — tests/ 아래 청크를 뒤로. fastapi-cli 는 top-3 자리의 40% 가 테스트 파일이었는데 gold 가 테스트인 문항은 0 이었다.
  2. 파일당 상한 — 같은 파일의 청크는 앞자리에 cap 개까지, 나머지는 뒤로. AST 청크는 같은 헤더(경로·스코프)를 달아 벡터가 뭉친다.
                   top-5 에 같은 파일이 두 번 이상 든 문항이 api-test 28/30, fastapi-cli 35/46 이었다.

불변식
  · 뒤로 보낼 뿐 버리지 않는다. pool 의 원소와 점수가 그대로라 `top_score`(불변 조건 5)도, 개수도 변하지 않는다.
  · 같은 등급 안에서는 원래 순서(벡터·RRF 순)를 지킨다.
  · 켤지는 인덱스 세대에서 읽는다(auto) — ast-v3 이상만. 옛 인덱스의 측정값은 한 run 안에서 그대로 재현된다.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from functools import lru_cache

from .config import CHUNKER_RANK, is_excluded

RERANK_FROM = "ast-v3"


@lru_cache(maxsize=16)
def _split_spec(spec: str) -> tuple[tuple[str, ...], str]:
    """`/` 없는 패턴은 경로 조각 하나(디렉터리명·파일명)에, `/` 있는 패턴은 전체 상대 경로에 맞춘다.

    exclude_globs 의 `**/test_*.*` 는 `.*test_` 로 풀려 `latest_data.py` 까지 잡는다(조각 경계가 없다).
    감점은 코퍼스 규칙이 아니라 순서 규칙이라 그 문법을 손대지 않고 여기서 조각 단위로 맞춘다.
    """
    segs, full = [], []
    for raw in spec.split(","):
        p = raw.strip().replace("\\", "/").strip("/")
        if not p:
            continue
        (full if "/" in p else segs).append(p)
    return tuple(segs), ",".join(full)


def demoted(path: str, spec: str) -> bool:
    if not spec or not path:
        return False
    segs, full = _split_spec(spec)
    rel = path.replace("\\", "/").lstrip("./")
    if any(fnmatchcase(part, pat) for part in rel.split("/") for pat in segs):
        return True
    return bool(full) and is_excluded(rel, full)


def enabled(mode: str, chunker: str | None) -> bool:
    """mode: auto | on | off. auto 는 그 인덱스의 청커 세대로 정한다."""
    m = (mode or "auto").lower()
    if m == "on":
        return True
    if m == "off":
        return False
    return CHUNKER_RANK.get(chunker or "", 0) >= CHUNKER_RANK[RERANK_FROM]


def reorder(hits: list[dict], *, per_file_cap: int, demote_spec: str) -> list[dict]:
    """정렬 키 (감점 여부, 파일당 상한 초과 여부, 원래 순서). 상한 초과한 원본 코드 청크가 테스트 청크보다 앞이다."""
    if not hits:
        return hits
    seen: dict[str, int] = {}
    keyed = []
    for i, h in enumerate(hits):
        path = str(h.get("path") or "")
        demote = 1 if demoted(path, demote_spec) else 0
        n = seen.get(path, 0)
        seen[path] = n + 1
        overflow = 1 if (per_file_cap > 0 and n >= per_file_cap) else 0
        keyed.append(((demote, overflow, i), h))
    return [h for _, h in sorted(keyed, key=lambda p: p[0])]
