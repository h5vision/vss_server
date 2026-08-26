"""
출처(reference) 구성 — 답변의 [N] 마커를 파싱해 프론트가 쓸 형태로 변환합니다.

두 층으로 나눕니다.

  references       청크 단위. [N] 과 1:1 대응. 순서 고정.
                   → 답변 본문의 [2] 를 클릭하면 references[1]

  reference_files  파일 단위로 묶음. 화면 하단 "출처" 목록용.
                   → 같은 파일의 여러 청크가 하나로 합쳐짐

⚠ 왜 두 층인가
   검색 단위는 파일이 아니라 청크입니다. 한 파일에서 3개 청크가 걸리면
   [1][2][3] 이 전부 같은 파일입니다. 화면에 3줄로 보이면 혼란스러우므로
   파일 단위로 묶되, [N] 대응은 청크 단위로 유지해야 합니다.
   파일로 묶은 뒤 번호를 다시 매기면 답변의 [3] 이 가리킬 대상이 사라집니다.
"""

from __future__ import annotations

import re

# "[1]", "[2][3]", "[1, 2]" 등을 모두 잡습니다
_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def parse_citations(answer: str) -> list[int]:
    """
    답변 본문에서 인용 번호를 추출합니다. 등장 순서 유지, 중복 제거.

        "A 입니다 [1]. B 는 [3][1] 입니다."  →  [1, 3]
    """
    out: list[int] = []
    for m in _CITE.finditer(answer or ""):
        for part in m.group(1).split(","):
            try:
                n = int(part.strip())
            except ValueError:
                continue
            if n not in out:
                out.append(n)
    return out


def build_references(contexts: list[dict], answer: str | None = None,
                     cited_only: bool = False,
                     include_text: bool = True,
                     text_limit: int = 400) -> dict:
    """
    검색 결과(contexts) → 프론트용 출처 구조.

    Parameters
    ----------
    contexts    searcher.search() 의 contexts. 순서가 곧 [N] 번호입니다.
    answer      LLM 답변. 주면 실제 인용된 것만 추릴 수 있습니다.
    cited_only  True 면 답변이 인용한 근거만 남깁니다.
                ⚠ 번호는 원래 값을 유지합니다. 재번호를 매기면 [N] 이 깨집니다.
    include_text 청크 원문 포함 여부. 응답 크기가 커지므로 필요할 때만.
    text_limit  원문을 포함할 때 자를 길이.

    Returns
    -------
    {"references": [...], "reference_files": [...], "cited": [...]}
    """
    cited = parse_citations(answer) if answer else []

    refs: list[dict] = []
    for i, c in enumerate(contexts, start=1):
        if cited_only and cited and i not in cited:
            continue

        ls = c.get("line_start") or None
        le = c.get("line_end") or None

        ref = {
            "n": i,                       # ★ [N] 과 1:1. 절대 바꾸지 말 것
            "path": c.get("path", ""),
            "type": c.get("type", "code"),
            "line": ls,                   # 프론트 요청 형식 (점프용)
            "line_start": ls,             # 범위 시작
            "line_end": le,               # 범위 끝 (강조용)
            "section": c.get("section") or None,
            "score": round(float(c.get("score", 0.0)), 4),
            "cited": (i in cited) if cited else None,
        }
        if include_text:
            t = c.get("text", "")
            ref["text"] = t[:text_limit] + ("…" if len(t) > text_limit else "")
        refs.append(ref)

    # ── 파일 단위 묶기 (등장 순서 유지) ────────────────────────
    files: dict[str, dict] = {}
    for r in refs:
        f = files.setdefault(r["path"], {
            "path": r["path"],
            "type": r["type"],
            "citations": [],      # 이 파일이 갖는 [N] 목록
            "lines": [],          # [[start, end], ...]
            "sections": [],
            "best_score": 0.0,
            "cited": False,
        })
        f["citations"].append(r["n"])
        if r["line_start"]:
            f["lines"].append([r["line_start"], r["line_end"]])
        if r["section"] and r["section"] not in f["sections"]:
            f["sections"].append(r["section"])
        f["best_score"] = max(f["best_score"], r["score"])
        if r.get("cited"):
            f["cited"] = True

    for f in files.values():
        # 하이퍼링크 기본 목적지 = 가장 앞선 줄
        f["line"] = min((l[0] for l in f["lines"]), default=None)
        f["chunk_count"] = len(f["citations"])

    return {
        "references": refs,
        "reference_files": list(files.values()),
        "cited": cited,
    }


def to_editor_uri(path: str, line: int | None = None,
                  project_root: str | None = None) -> str:
    """
    VSCode 에서 파일을 열 수 있는 URI.

        vscode://file/C:/Pj/fest-api/scripts/translate.py:53

    ⚠ project_root 는 Extension 이 도는 머신 기준이어야 합니다.
       rag_lab 은 인덱싱한 머신의 경로만 알고 있으므로, 원칙적으로
       이 변환은 Extension 쪽에서 하는 것이 안전합니다.
       여기서는 편의를 위해 제공만 합니다.
    """
    base = (project_root.rstrip("/\\").replace("\\", "/") + "/") if project_root else ""
    uri = f"vscode://file/{base}{path}"
    return f"{uri}:{line}" if line else uri
