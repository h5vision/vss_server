"""
맥락 헤더 — 청크 텍스트 앞에 구조 정보를 붙입니다.

⚠ 왜 필요한가 (RAG 에서 표준적으로 쓰이는 기법)

    임베딩은 **텍스트만** 봅니다. 메타데이터는 벡터에 들어가지 않습니다.

        메타데이터:  path = "src/payment/service.py"   ← 검색에 안 쓰임
        벡터:        청크 원문 텍스트만                 ← 검색에 쓰임

    그래서 이런 코드가 저장되면

            def process(self, req):
                self._validate(req)
                return self._gateway.submit(req, retries=3)

    "결제"라는 개념이 벡터 어디에도 없습니다.
    질문 "결제 처리는 어디서?" 와의 유사도가 낮게 나옵니다.

    헤더를 붙이면 경로·클래스·함수 이름이 텍스트에 들어갑니다.

        # src/payment/service.py > class PaymentService > def process
            def process(self, req):
                ...

⚠ 이 모듈을 켜면 **기존 인덱스가 무효**가 됩니다.
   청크 텍스트가 달라지므로 벡터도 달라집니다. 재인덱싱이 필요합니다.

⚠ 효과는 **평가셋으로 검증하기 전까지 미확인**입니다.
   일반적으로 개선되는 기법이지만, 이 레포에서 실제로 그런지는 재봐야 합니다.
   config.CFG.context_header 로 껐다 켜며 A/B 비교할 수 있게 만들었습니다.
"""

from __future__ import annotations

import re

# ── 코드에서 소속(클래스·함수)을 찾기 위한 패턴 ──────────────
# 언어별 정식 파서(AST)를 쓰지 않고 정규식으로 근사합니다.
#   장점: 언어 무관, 빠름, 의존성 없음
#   한계: 중첩·주석 안의 코드를 오인할 수 있음
# ⚠ 정확도가 필요하면 AST 기반으로 교체해야 합니다 (미검증 항목).
_DEF_PATTERNS = [
    # Python
    (re.compile(r"^(\s*)class\s+([A-Za-z_]\w*)"), "class"),
    (re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)"), "def"),
    # JS / TS
    (re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^(\s*)(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"), "function"),
    (re.compile(r"^(\s*)(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("), "const"),
    # Java / C# / Go / Rust
    (re.compile(r"^(\s*)(?:public|private|protected)?\s*(?:static\s+)?class\s+([A-Za-z_]\w*)"), "class"),
    (re.compile(r"^(\s*)func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"), "func"),
    (re.compile(r"^(\s*)(?:pub\s+)?fn\s+([A-Za-z_]\w*)"), "fn"),
]


def find_enclosing(lines: list[str], start_idx: int, max_lookback: int = 400) -> list[str]:
    """
    start_idx(0-based) 위쪽을 훑어 이 위치를 감싸는 정의를 찾습니다.

    들여쓰기가 더 얕은 정의만 채택합니다. 예를 들어

        class PaymentService:          indent 0   ← 채택
            def _helper(self): ...     indent 4   ← 건너뜀 (형제)
            def process(self, req):    indent 4   ← 채택 (직속 부모)
                ...                    ← start_idx

    반환: 바깥쪽부터 순서대로. 예) ["class PaymentService", "def process"]
    """
    if start_idx <= 0:
        return []

    # 기준 들여쓰기: 청크 첫 줄 중 내용이 있는 줄
    base_indent = None
    for ln in lines[start_idx:start_idx + 10]:
        if ln.strip():
            base_indent = len(ln) - len(ln.lstrip())
            break
    if base_indent is None:
        return []

    found: list[tuple[int, str]] = []       # (indent, label)
    lo = max(0, start_idx - max_lookback)
    cur_limit = base_indent

    for i in range(start_idx - 1, lo - 1, -1):
        ln = lines[i]
        if not ln.strip():
            continue
        for pat, kw in _DEF_PATTERNS:
            m = pat.match(ln)
            if not m:
                continue
            indent = len(m.group(1))
            if indent >= cur_limit:
                break                       # 형제이거나 더 깊음 → 무시
            name = m.group(2)
            found.append((indent, f"{kw} {name}"))
            cur_limit = indent              # 더 바깥만 찾도록 좁힘
            break
        if cur_limit == 0:
            break                           # 최상위까지 도달

    found.reverse()                         # 바깥 → 안쪽
    return [label for _, label in found]


def build(rel_path: str, kind: str, *,
          section: str | None = None,
          enclosing: list[str] | None = None,
          line_start: int | None = None) -> str:
    """
    맥락 헤더 한 줄을 만듭니다.

        code : # src/payment/service.py > class PaymentService > def process
        doc  : # docs/conventions.md > 에러 처리

    ⚠ 헤더는 청크 텍스트의 일부가 되어 **임베딩에 들어갑니다.**
       그래서 사람이 읽기 좋은 형태보다 **검색에 도움되는 단어**가 우선입니다.
    """
    parts: list[str] = [rel_path]
    if section:
        parts.append(section)
    if enclosing:
        parts.extend(enclosing)

    head = " > ".join(parts)
    return f"# {head}" if kind == "code" else f"# {head}"


def apply(chunk_text: str, header: str) -> str:
    """헤더를 청크 앞에 붙입니다. 이미 붙어 있으면 중복 방지."""
    if chunk_text.startswith(header):
        return chunk_text
    return f"{header}\n{chunk_text}"


def strip(chunk_text: str) -> str:
    """
    저장된 청크에서 헤더를 제거합니다.

    ⚠ 프롬프트에 넣을 때는 헤더를 빼는 편이 나을 수 있습니다.
       [N] 줄에 이미 경로가 들어가므로 중복이고, 토큰을 낭비합니다.
       다만 유지해도 해롭지는 않아 기본은 유지입니다.
    """
    if chunk_text.startswith("# ") and "\n" in chunk_text:
        first, rest = chunk_text.split("\n", 1)
        if " > " in first or first.count("/") >= 1:
            return rest
    return chunk_text
