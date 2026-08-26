"""
프롬프트 조립 · 답변 후처리 (rag_lab searcher.py 의 render_prompt / is_no_evidence / finalize 복사본 — SALVAGE.md).

불변 조건
  · 근거 헤더는 세 분기: doc+section → `[N] path #section` / 줄 정보 → `[N] path lines a-b` / 그 외 `[N] path`
  · `[N]` 은 contexts 배열 인덱스 + 1 과 1:1. 배열을 정렬·필터링하지 않습니다.
  · 근거가 없을 때 user 는 "검색된 프로젝트 문서가 없습니다."
  · 모델의 거절 문장은 정확히 한 줄 `NO_EVIDENCE`. 판정은 관대하게(앞뒤 설명 허용).
"""

from __future__ import annotations

import re

from .references import build_references

SYSTEM_RAG = (
    "제공된 근거만 사용해 한국어로 답한다. 근거에 없는 파일·동작은 추측하지 않는다.\n"
    "각 주장 끝에 사용한 근거 번호를 [1], [2] 형식으로 표기한다.\n"
    "근거가 질문에 답하기 부족하면 다른 설명 없이 정확히 다음 한 줄만 출력한다: NO_EVIDENCE"
)

SYSTEM_PLAIN = (
    "당신은 소프트웨어 개발을 돕는 어시스턴트다. 한국어로 답한다. "
    "확신이 없는 내용은 확신이 없다고 말한다."
)


def context_head(i: int, c: dict) -> str:
    if c.get("type") == "doc" and c.get("section"):
        return f"[{i}] {c['path']} #{c['section']}"
    if c.get("line_start"):
        return f"[{i}] {c['path']} lines {c['line_start']}-{c['line_end']}"
    return f"[{i}] {c['path']}"


def render_prompt(question: str, contexts: list[dict], *,
                  selected_code: str | None = None) -> list[dict]:
    """검색 결과 → LLM messages. selected_code 는 에디터에서 선택한 코드(FN-B04)입니다."""
    if contexts:
        parts = ["프로젝트 검색 결과:"]
        for i, c in enumerate(contexts, start=1):
            parts.append(f"{context_head(i, c)}\n{c['text']}")
    else:
        parts = ["프로젝트 검색 결과:\n검색된 프로젝트 문서가 없습니다."]
    if selected_code and selected_code.strip():
        parts.append(f"사용자가 선택한 코드:\n```\n{selected_code.strip()}\n```")
    parts.append(f"질문:\n{question}")
    return [
        {"role": "system", "content": SYSTEM_RAG},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def render_plain_prompt(question: str, *, selected_code: str | None = None) -> list[dict]:
    """검색 없이 모델만으로 답할 때(데모의 'RAG 끔' 비교용)."""
    user = question
    if selected_code and selected_code.strip():
        user = f"사용자가 선택한 코드:\n```\n{selected_code.strip()}\n```\n\n질문:\n{question}"
    return [{"role": "system", "content": SYSTEM_PLAIN}, {"role": "user", "content": user}]


_NO_EVIDENCE = re.compile(r"^\s*NO_EVIDENCE\s*$", re.MULTILINE)


def is_no_evidence(answer: str) -> bool:
    if not answer:
        return True
    a = answer.strip()
    return a == "NO_EVIDENCE" or bool(_NO_EVIDENCE.search(a))


def finalize(answer: str, contexts: list[dict], cited_only: bool = True,
             include_text: bool = False) -> dict:
    """LLM 답변 + 검색 근거 → 최종 응답. cited_only 여도 n 번호는 원래 값을 유지합니다."""
    if is_no_evidence(answer):
        return {"answer": "NO_EVIDENCE", "references": [], "reference_files": [],
                "cited": [], "no_evidence": True}
    r = build_references(contexts, answer=answer, cited_only=cited_only, include_text=include_text)
    return {"answer": answer, "references": r["references"],
            "reference_files": r["reference_files"], "cited": r["cited"], "no_evidence": False}


def legacy_sources(contexts: list[dict], text_limit: int = 400) -> list[dict]:
    """P의 ChatResponse.source 형태 ({file, chunk, score}). 프론트 호환용."""
    out = []
    for c in contexts:
        t = c.get("text") or ""
        out.append({"file": c.get("path", ""),
                    "chunk": t[:text_limit] + ("…" if len(t) > text_limit else ""),
                    "score": round(float(c.get("score", 0.0)), 4)})
    return out
