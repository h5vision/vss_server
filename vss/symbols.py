"""
심볼 인식 검색 — 질문에 나온 이름으로 청크 순서를 바꿉니다 (BM25 융합과 같은 자리).

불변식 (search.py 와 같습니다)
  · 점수를 건드리지 않는다. 순서만 바꾼다. `top_score` 는 여전히 pool 의 최대 벡터 점수다.
  · 따라서 `top_score >= threshold ⟺ has_evidence` 가 그대로 성립한다 (CHARTER 5).

재료는 청크가 이미 들고 있는 `symbol` 하나입니다 — 저장 계층에 원래부터 있던 컬럼이라
재인덱싱이 필요 없고, 지금 도는 인덱스에서 바로 켤 수 있습니다.
"""

from __future__ import annotations

import re
from typing import Iterable

# 코드 질문에 흔하지만 심볼로 쓰면 노이즈만 되는 낱말.
_STOP = {"self", "none", "true", "false", "def", "class", "import", "from", "return",
         "http", "https", "readme", "python", "json"}

# 질문에서 "이름처럼 보이는 것" 을 줍는 패턴. 실제 심볼인지는 인덱스와 대조해서 거른다.
_BACKTICK = re.compile(r"`([^`\n]{1,120})`")
_DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")
_SNAKE = re.compile(r"\b_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
# 상수. 밑줄을 요구해서 HTTP·JSON 같은 낱말이 우연히 걸리지 않게 한다
# (대신 밑줄 없는 상수 `TIMEOUT` 은 백틱으로 감싸야 잡힌다).
_CONST = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


def candidates(query: str) -> list[str]:
    """질문에서 심볼 후보를 뽑습니다. 중복 없이, 나온 순서를 지킵니다.

    백틱 안은 사람이 "이건 이름이다" 라고 표시한 것이라 통째로 넣고, 나머지는 모양으로 줍습니다.
    한국어 문장에 섞인 영어 단어가 걸릴 수 있지만, 인덱스에 없는 이름은 뒤에서 버려집니다.
    """
    out: list[str] = []
    seen: set[str] = set()

    def push(tok: str) -> None:
        tok = tok.strip().strip("`'\"(),;:")
        if len(tok) < 2 or tok.lower() in _STOP:
            return
        if tok not in seen:
            seen.add(tok)
            out.append(tok)

    for m in _BACKTICK.finditer(query):
        inner = m.group(1).strip()
        push(inner)
        for pat in (_DOTTED, _CAMEL, _SNAKE, _CONST):   # 백틱 안이 문장이면 그 안에서 다시 줍는다
            for mm in pat.finditer(inner):
                push(mm.group(0))
    for pat in (_DOTTED, _CAMEL, _SNAKE, _CONST):
        for m in pat.finditer(query):
            push(m.group(0))
    for m in _CALL.finditer(query):
        push(m.group(1))
    return out


class SymbolIndex:
    """symbol → chunk id. 전체 이름과 마지막 조각을 둘 다 색인합니다.

    `Service.run` 으로 물어도 `run` 으로 물어도 걸리게 하기 위한 것이고,
    전체 이름이 맞은 쪽이 언제나 앞섭니다 (`lookup` 의 등급 0/1).
    """

    def __init__(self, chunks: Iterable[dict]):
        self.full: dict[str, list[str]] = {}
        self.tail: dict[str, list[str]] = {}
        self.n = 0
        for c in chunks:
            self.n += 1
            cid = c.get("_id")
            raw = c.get("symbol")
            if not cid or not raw:
                continue
            # 한 청크가 여러 이름을 담을 수 있다 (예: "A, B" 형태의 나란한 대입)
            for sym in (s.strip() for s in str(raw).split(",")):
                if not sym or sym.startswith("("):          # "(module docstring)" 같은 표시는 이름이 아니다
                    continue
                self.full.setdefault(sym, []).append(cid)
                self.full.setdefault(sym.lower(), []).append(cid)
                last = sym.split(".")[-1]
                if last != sym:
                    self.tail.setdefault(last, []).append(cid)
                    self.tail.setdefault(last.lower(), []).append(cid)

    def lookup(self, tokens: list[str]) -> dict[str, int]:
        """후보 이름들 → {chunk id: 등급}. 등급 0 이 가장 강한 일치(전체 이름)입니다."""
        out: dict[str, int] = {}
        for tok in tokens:
            for grade, table in ((0, self.full), (1, self.tail)):
                for key in (tok, tok.lower()):
                    for cid in table.get(key, ()):
                        if out.get(cid, 99) > grade:
                            out[cid] = grade
        return out


def reorder(hits: list[dict], graded: dict[str, int]) -> list[dict]:
    """일치한 청크를 앞으로 당깁니다. 점수는 건드리지 않고 같은 등급 안에서는 원래 순서를 지킵니다."""
    if not graded:
        return hits
    return [h for _, h in sorted(enumerate(hits),
                                 key=lambda p: (graded.get(p[1]["_id"], 9), p[0]))]
