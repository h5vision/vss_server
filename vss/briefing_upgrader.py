"""
프로젝트 브리핑 v2 — 결정적 추출(analysis.py) + LLM 요약을 합쳐 Markdown 한 편을 만듭니다.

출력 구성 (2026-08-26 합의 형식)
  # <프로젝트 이름>
  ## 이 프로젝트는            ← LLM (README·설정·라우트 표를 근거로 2~4문장, [N] 인용)
  ## 문서 요약                ← LLM (문서마다 별도 호출, 2~3줄, [N] 인용)
  ## 진입점                   ← 결정적
  ## 진입점별 함수 목록        ← 결정적 (AST 헤더 + 라우트 표)
  ## 기능 목록                ← LLM (README·라우트·문서 요약을 근거로)
  ## 근거                     ← 인용된 재료 목록

⚠ 이 모듈이 LLM 을 부르는 지점은 llm.chat() 뿐이고, 인덱서는 이 모듈을 import 하지 않습니다 (호출자가 on_done 으로 주입).
⚠ 근거 예산: 호출당 6,000 토큰 이하 (num_ctx 8192 − 답변 1,200 − 시스템). 넘치면 자릅니다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import analysis, llm, search as search_mod
from .chunker import read_text
from .config import CFG
from .references import build_references
from .store import get_store

BUDGET_TOKENS = 6_000
LIMIT_README = 3_000
LIMIT_DOC_CHARS = 3_500
LIMIT_DOC_FILES = 6
LIMIT_CONFIG = 600
LIMIT_ENTRY_LINES = 40
FEATURE_SEARCH_ROUNDS = 2
FEATURE_QUERY_LIMIT = 6
FEATURE_EVIDENCE_LIMIT = 12
FEATURE_LLM_EVIDENCE_LIMIT = 8
MAX_CORE_FEATURES = 10


def est_tokens(text: str) -> int:
    ko = sum(1 for c in text if "가" <= c <= "힣")
    return int(ko / 1.4 + (len(text) - ko) / 3.2)


@dataclass
class Material:
    label: str
    text: str
    path: str | None = None
    type: str = "doc"
    line_start: int | None = None
    line_end: int | None = None


@dataclass
class Collected:
    materials: list[Material] = field(default_factory=list)
    analysis: dict = field(default_factory=dict)
    truncated: list[str] = field(default_factory=list)
    readme_summary: dict | None = None
    feature_grounding: list[dict] = field(default_factory=list)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…(이하 생략)"


def collect(project_root: str | Path) -> Collected:
    root = Path(project_root).resolve()
    c = Collected(analysis=analysis.analyze(root))
    a = c.analysis
    if a["readme"]:
        txt = read_text(root / a["readme"]) or ""
        if txt.strip():
            c.materials.append(Material(f"{a['readme']} (프로젝트 설명)", _clip(txt.strip(), LIMIT_README),
                                        path=a["readme"], type="doc"))
    for name in a["configs"][:1]:
        txt = read_text(root / name) or ""
        if txt.strip():
            c.materials.append(Material(f"{name} (프로젝트 설정)", _clip(txt.strip(), LIMIT_CONFIG),
                                        path=name, type="code"))
    for e in a["entry_points"][:4]:
        txt = read_text(root / e["path"]) or ""
        lines = txt.splitlines()[:LIMIT_ENTRY_LINES]
        if lines:
            c.materials.append(Material(f"{e['path']} (진입점 — {e['reason']})", "\n".join(lines).strip(),
                                        path=e["path"], type="code", line_start=1, line_end=len(lines)))
    for rel in a["docs"]:
        if rel == a["readme"]:
            continue
        if sum(1 for m in c.materials if m.type == "doc" and m.path != a["readme"]) >= LIMIT_DOC_FILES:
            c.truncated.append(rel)
            continue
        txt = read_text(root / rel) or ""
        if txt.strip():
            c.materials.append(Material(f"{rel} (프로젝트 문서)", _clip(txt.strip(), LIMIT_DOC_CHARS),
                                        path=rel, type="doc"))
    return c


# ── LLM 호출부 ──────────────────────────────────────────────

SYSTEM = (
    "당신은 신입 개발자에게 프로젝트를 소개하는 온보딩 도우미입니다. "
    "제공된 근거만 사용해 한국어 Markdown 으로 씁니다. 근거에 없는 파일·동작·목적은 추측하지 않습니다. "
    "각 주장 끝에 사용한 근거 번호를 [1], [2] 형식으로 표기합니다. "
    "확인되지 않는 항목은 '문서에서 확인되지 않음' 이라고 적습니다."
)

README_ANALYSIS_SYSTEM = (
    "당신은 코드를 처음 전달받은 개발자를 위한 프로젝트 분석 도우미입니다. "
    "README에 실제로 적힌 내용만 사용하고, 코드의 동작이나 파일 위치를 추측하지 않습니다. "
    "응답은 설명 없이 유효한 JSON 객체 하나만 출력합니다."
)


def _numbered(mats: list[Material], idx: list[int]) -> str:
    parts = []
    for i in idx:
        m = mats[i]
        parts.append(f"[{i + 1}] {m.label}\n{m.text}")
    return "\n\n".join(parts)


def _budget_indices(mats: list[Material], wanted: list[int], extra_text: str = "") -> list[int]:
    used = est_tokens(extra_text) + 300
    out = []
    for i in wanted:
        t = est_tokens(mats[i].text) + 20
        if used + t > BUDGET_TOKENS:
            continue
        out.append(i)
        used += t
    return out


def _parse_readme_summary(text: str) -> dict:
    """README 분석 응답을 구조화된 기능 후보 목록으로 변환합니다."""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S | re.I)
    if fenced:
        candidate = fenced.group(1)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {"project_summary": "", "core_features": [], "parse_error": "invalid_json"}
    if not isinstance(parsed, dict):
        return {"project_summary": "", "core_features": [], "parse_error": "not_object"}
    summary = parsed.get("project_summary")
    features = parsed.get("core_features")
    if not isinstance(summary, str):
        summary = ""
    if not isinstance(features, list):
        features = []
    normalized: list[dict] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        name = feature.get("name")
        purpose = feature.get("purpose")
        queries = feature.get("search_queries")
        evidence = feature.get("evidence")
        if not isinstance(name, str) or not name.strip():
            continue
        normalized.append({
            "name": name.strip(),
            "purpose": purpose.strip() if isinstance(purpose, str) else "",
            "search_queries": [q.strip() for q in queries if isinstance(q, str) and q.strip()]
            if isinstance(queries, list) else [],
            "evidence": evidence.strip() if isinstance(evidence, str) else "",
        })
    return {"project_summary": summary.strip(), "core_features": normalized}


def gen_readme_summary(c: Collected, model: str | None) -> dict:
    """README만 분석해 프로젝트 요약과 코드 검색용 핵심 기능 후보를 추출합니다."""
    readme = next((m for m in c.materials if m.path == c.analysis.get("readme")), None)
    if readme is None:
        return {"project_summary": "", "core_features": [], "status": "no_readme"}
    user = (
        "다음은 프로젝트의 README.md 원문입니다. 이 문서만 근거로 분석하세요.\n\n"
        f"[1] {readme.label}\n{readme.text}\n\n"
        "다음 JSON 형식으로만 응답하세요. Markdown 코드 fence나 추가 설명은 쓰지 마세요.\n"
        "{\n"
        '  "project_summary": "프로젝트가 해결하는 문제와 주요 사용자를 2~4문장으로 요약",\n'
        '  "core_features": [\n'
        "    {\n"
        '      "name": "README에서 확인된 핵심 기능명",\n'
        '      "purpose": "사용자 관점의 기능 설명",\n'
        '      "search_queries": ["이 기능의 구현을 찾기 위한 검색어", "동의어 또는 관련 용어"],\n'
        '      "evidence": "README에서 기능을 설명하는 짧은 원문 인용"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "핵심 기능은 3~10개로 제한하고, README에 근거가 없는 기능은 포함하지 마세요."
    )
    response = llm.chat([{"role": "system", "content": README_ANALYSIS_SYSTEM},
                         {"role": "user", "content": user}],
                        model=model, temperature=0.1, num_predict=700)
    parsed = _parse_readme_summary(response)
    parsed["status"] = "ready" if "parse_error" not in parsed else "invalid_response"
    return parsed


def _parse_feature_explanation(text: str) -> dict:
    """기능 분석 LLM 응답을 구조화된 온보딩 정보로 변환합니다."""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S | re.I)
    if fenced:
        candidate = fenced.group(1)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return {"description": "", "entry_points": [], "implementation_files": [], "flow": [],
                "tests": [], "configs": [], "next_files_to_read": [], "parse_error": "invalid_json"}
    if not isinstance(parsed, dict):
        return {"description": "", "entry_points": [], "implementation_files": [], "flow": [],
                "tests": [], "configs": [], "next_files_to_read": [], "parse_error": "not_object"}

    def strings(key: str) -> list[str]:
        value = parsed.get(key)
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    def locations(key: str) -> list[dict]:
        value = parsed.get(key)
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            row = {"path": item["path"].strip()}
            if not row["path"]:
                continue
            for key_name in ("symbol", "reason"):
                if isinstance(item.get(key_name), str) and item[key_name].strip():
                    row[key_name] = item[key_name].strip()
            if isinstance(item.get("line"), int) and item["line"] > 0:
                row["line"] = item["line"]
            out.append(row)
        return out

    description = parsed.get("description")
    return {
        "description": description.strip() if isinstance(description, str) else "",
        "entry_points": locations("entry_points"),
        "implementation_files": locations("implementation_files"),
        "flow": strings("flow"),
        "tests": locations("tests"),
        "configs": locations("configs"),
        "next_files_to_read": locations("next_files_to_read"),
    }


def _feature_queries(feature: dict, evidence: list[dict]) -> list[str]:
    """1차 검색 결과에서 파일·심볼을 뽑아 2차 검색어를 만듭니다."""
    queries = list(feature.get("search_queries") or [])
    for context in evidence:
        symbol = context.get("symbol")
        path = context.get("path")
        for value in (symbol, path):
            if isinstance(value, str) and value.strip() and value not in queries:
                queries.append(value)
    return queries[:FEATURE_QUERY_LIMIT]


def _feature_evidence_prompt(feature: dict, evidence: list[dict]) -> str:
    rows = []
    for i, context in enumerate(evidence[:FEATURE_LLM_EVIDENCE_LIMIT], start=1):
        rows.append({"id": i, "query": context.get("query"), "path": context.get("path"),
                     "line_start": context.get("line_start"), "line_end": context.get("line_end"),
                     "symbol": context.get("symbol"), "text": _clip(context.get("text", ""), 1800)})
    return json.dumps({"feature": feature, "evidence": rows}, ensure_ascii=False, indent=2)


def _reference_files(evidence: list[dict]) -> list[dict]:
    """검색 청크를 파일 단위 reference file 목록으로 묶습니다."""
    files: dict[str, dict] = {}
    for context in evidence:
        path = context.get("path")
        if not isinstance(path, str) or not path:
            continue
        item = files.setdefault(path, {"path": path, "lines": [], "symbols": [], "queries": []})
        start = context.get("line_start")
        end = context.get("line_end")
        if isinstance(start, int):
            item["lines"].append([start, end if isinstance(end, int) else start])
        symbol = context.get("symbol")
        if isinstance(symbol, str) and symbol and symbol not in item["symbols"]:
            item["symbols"].append(symbol)
        query = context.get("query")
        if isinstance(query, str) and query and query not in item["queries"]:
            item["queries"].append(query)
    return list(files.values())


def _evidence_key(context: dict) -> tuple[str, str, int | None]:
    """같은 파일·심볼은 하나로 묶고, 심볼이 없을 때만 줄 위치를 구분합니다."""
    path = str(context.get("path") or "")
    symbol = str(context.get("symbol") or "")
    line = None if symbol else context.get("line_start")
    return path, symbol, line if isinstance(line, int) else None


def _empty_explanation() -> dict:
    return {"description": "", "entry_points": [], "implementation_files": [], "flow": [],
            "tests": [], "configs": [], "next_files_to_read": []}


def _validate_explanation(explanation: dict, evidence: list[dict]) -> dict:
    """LLM이 제시한 위치를 실제 검색 근거의 파일·심볼과 대조합니다."""
    valid_pairs = {(str(item.get("path") or ""), str(item.get("symbol") or "")) for item in evidence}
    valid_paths = {str(item.get("path") or "") for item in evidence}
    checked = dict(explanation)
    for key in ("entry_points", "implementation_files", "tests", "configs", "next_files_to_read"):
        locations = []
        for item in explanation.get(key) or []:
            path = str(item.get("path") or "")
            symbol = str(item.get("symbol") or "")
            if path and (path, symbol) in valid_pairs or path in valid_paths and not symbol:
                locations.append(item)
        checked[key] = locations
    if not checked.get("description") or not any(
            checked.get(key) for key in ("entry_points", "implementation_files", "tests", "configs")):
        checked["description"] = ""
    return checked


def explain_feature(feature: dict, evidence: list[dict], model: str | None) -> dict:
    """검색된 코드 근거를 바탕으로 신입 개발자용 기능 설명을 생성합니다."""
    user = (
        "다음 JSON은 README에서 찾은 기능과 해당 기능을 찾기 위해 검색한 코드 근거입니다. "
        "근거에 없는 호출 흐름이나 파일은 추측하지 마세요. 확인할 수 없는 배열은 빈 배열로 두세요.\n\n"
        f"{_feature_evidence_prompt(feature, evidence)}\n\n"
        "응답은 설명 없이 유효한 JSON 객체 하나만 출력하세요. 형식은 다음과 같습니다.\n"
        "{\n"
        '  "description": "기능이 무엇을 하는지 2~4문장",\n'
        '  "entry_points": [{"path": "파일", "symbol": "함수 또는 클래스", "line": 1, "reason": "시작점인 이유"}],\n'
        '  "implementation_files": [{"path": "파일", "symbol": "관련 심볼", "line": 1, "reason": "역할"}],\n'
        '  "flow": ["확인된 처리 단계"],\n'
        '  "tests": [{"path": "테스트 파일", "symbol": "테스트", "line": 1, "reason": "검증 내용"}],\n'
        '  "configs": [{"path": "설정 파일", "symbol": "설정 이름", "line": 1, "reason": "영향"}],\n'
        '  "next_files_to_read": [{"path": "파일", "symbol": "심볼", "line": 1, "reason": "다음에 읽을 이유"}]\n'
        "}\n"
        "줄 번호는 근거의 line_start 또는 line_end 범위 안에서만 사용하세요."
    )
    response = llm.chat([{"role": "system", "content": README_ANALYSIS_SYSTEM},
                         {"role": "user", "content": user}],
                        model=model, temperature=0.1, num_predict=900)
    return _parse_feature_explanation(response)


def ground_features(project_id: str, readme_summary: dict, model: str | None = None) -> list[dict]:
    """README 기능을 검색하고, 검색 결과를 LLM으로 기능 설명으로 구조화합니다."""
    grounded: list[dict] = []
    for feature in readme_summary.get("core_features", [])[:MAX_CORE_FEATURES]:
        queries = list(feature.get("search_queries") or [])[:FEATURE_QUERY_LIMIT]
        evidence: list[dict] = []
        seen_ids: set[str] = set()
        errors: list[str] = []
        searched: set[str] = set()
        for _ in range(FEATURE_SEARCH_ROUNDS):
            round_evidence = list(evidence)
            evidence_before_round = len(evidence)
            for query in _feature_queries(feature, round_evidence):
                if query in searched:
                    continue
                searched.add(query)
                if len(searched) > FEATURE_QUERY_LIMIT:
                    break
                try:
                    result = search_mod.search(query, project_id, top_k=5, threshold=0.0,
                                               store=get_store())
                except Exception as exc:
                    errors.append(f"{query}: {type(exc).__name__}: {exc}")
                    continue
                for context in result.get("contexts", []):
                    context_id = str(context.get("_id") or (
                        context.get("path"), context.get("line_start"), context.get("line_end")))
                    if context_id in seen_ids:
                        continue
                    seen_ids.add(context_id)
                    evidence.append({"query": query, **context})
            deduped: dict[tuple[str, str, int | None], dict] = {}
            for context in evidence:
                deduped.setdefault(_evidence_key(context), context)
            evidence = list(deduped.values())
            queries = _feature_queries(feature, evidence)
            if len(evidence) == evidence_before_round or not queries or len(searched) >= FEATURE_QUERY_LIMIT:
                break
        evidence = evidence[:FEATURE_EVIDENCE_LIMIT]
        if not evidence:
            explanation = _empty_explanation()
            status = "unverified"
            verification_required = True
        else:
            try:
                explanation = _validate_explanation(explain_feature(feature, evidence, model), evidence)
            except Exception as exc:
                errors.append(f"LLM: {type(exc).__name__}: {exc}")
                explanation = _empty_explanation()
            status = "grounded" if explanation.get("description") else "partially_verified"
            verification_required = status != "grounded"
        grounded.append({"name": feature["name"], "purpose": feature.get("purpose", ""),
                         "search_queries": queries, "evidence": evidence,
                         "reference_files": _reference_files(evidence), "explanation": explanation,
                         "status": status, "verification_required": verification_required, "errors": errors})
    return grounded


def _readme_sections(c: Collected) -> list[str]:
    summary = c.readme_summary or {}
    project_summary = summary.get("project_summary") or "문서에서 확인되지 않음"
    out = ["## README 요약", "", project_summary, "", "## README에서 찾은 핵심 기능", ""]
    features = summary.get("core_features") or []
    grounding = {item["name"]: item for item in c.feature_grounding}
    reference_ids: dict[str, str] = {}
    reference_files: list[dict] = []
    for item in c.feature_grounding:
        for reference in item.get("reference_files", []):
            path = reference["path"]
            if path not in reference_ids:
                reference_ids[path] = f"R{len(reference_ids) + 1}"
                reference_files.append(reference)
    if not features:
        out.append("README에서 핵심 기능을 찾지 못했습니다.")
        return out
    unverified: list[str] = []
    for feature in features:
        name = feature["name"]
        purpose = feature.get("purpose") or "설명이 없습니다."
        item = grounding.get(name, {})
        explanation = item.get("explanation") or {}
        verified = item.get("status") == "grounded" and not item.get("verification_required")
        description = explanation.get("description") if verified else "확인 필요: 검색된 코드 근거가 없습니다."
        if not verified:
            unverified.append(name)
        out.append(f"### {name}")
        out.append(description)
        queries = ", ".join(f"`{query}`" for query in feature.get("search_queries", []))
        if queries:
            out.append(f"- 코드 검색어: {queries}")
        for label, key in (("시작점", "entry_points"), ("핵심 구현", "implementation_files"),
                   ("관련 테스트", "tests"), ("관련 설정", "configs"),
                   ("다음에 읽을 파일", "next_files_to_read")):
            if not verified:
                continue
            locations = explanation.get(key) or []
            if not locations:
                continue
            rendered = []
            for location in locations:
                target = f"`{location['path']}"
                if location.get("line"):
                    target += f":L{location['line']}"
                target += "`"
                if location.get("symbol"):
                    target += f" ({location['symbol']})"
                if location.get("reason"):
                    target += f" - {location['reason']}"
                rendered.append(target)
            out.append(f"- {label}: {', '.join(rendered)}")
        flow = explanation.get("flow") or []
        if verified and flow:
            out.append("- 처리 흐름: " + " -> ".join(flow))
        evidence = item.get("evidence") or []
        if evidence:
            refs = []
            for reference in item.get("reference_files", []):
                ref_id = reference_ids.get(reference["path"])
                if ref_id:
                    refs.append(f"[{ref_id}] `{reference['path']}`")
            out.append(f"- 관련 reference file: {', '.join(refs)}")
        elif item.get("errors"):
            out.append("- 관련 코드: 검색하지 못했습니다.")
        else:
            out.append("- 관련 코드: 검색 결과가 없습니다.")
        out.append("")
    if unverified:
        out += ["## 코드에서 확인되지 않은 README 기능", ""]
        out.extend(f"- {name}: 확인 필요" for name in unverified)
        out.append("")
    if reference_files:
        out += ["## Reference files", "", "| ID | 파일 | 줄 | 심볼 | 검색어 |", "|---|---|---|---|---|"]
        for reference in reference_files:
            ref_id = reference_ids[reference["path"]]
            lines = ", ".join(f"L{start}-{end}" for start, end in reference["lines"]) or "-"
            symbols = ", ".join(f"`{symbol}`" for symbol in reference["symbols"]) or "-"
            queries = ", ".join(f"`{query}`" for query in reference["queries"]) or "-"
            out.append(f"| {ref_id} | `{reference['path']}` | {lines} | {symbols} | {queries} |")
    return out


def _routes_table(a: dict, limit: int = 40) -> str:
    rows = a.get("routes") or []
    if not rows:
        return ""
    lines = ["| method | path | handler | file |", "|---|---|---|---|"]
    for r in rows[:limit]:
        lines.append(f"| {r['method']} | `{r['path']}` | `{r['handler'] or '?'}` | `{r['file']}`:{r['line']} |")
    if len(rows) > limit:
        lines.append(f"| … | 총 {len(rows)}개 중 {limit}개 표시 | | |")
    return "\n".join(lines)


def gen_overview(c: Collected, model: str | None) -> str:
    mats = c.materials
    wanted = [i for i, m in enumerate(mats) if m.type != "doc" or (m.path == c.analysis.get("readme"))]
    routes = _routes_table(c.analysis, 25)
    idx = _budget_indices(mats, wanted, routes)
    user = (
        "프로젝트 정보:\n\n" + _numbered(mats, idx)
        + (f"\n\n라우트 표 (코드에서 추출):\n{routes}" if routes else "")
        + "\n\n작성할 것:\n"
          "## 이 프로젝트는\n이 프로젝트가 무엇이고 누가 왜 쓰는지 2~4문장. 근거 번호 표기.\n\n"
          "## 기능 목록\n사용자 관점의 기능을 항목당 한 줄로 5~10개. 각 항목 끝에 근거 번호.\n"
          "위 두 절만 출력합니다. 제목은 그대로 씁니다."
    )
    return llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                    model=model, temperature=0.2, num_predict=900)


def gen_doc_summary(c: Collected, i: int, model: str | None) -> str:
    m = c.materials[i]
    user = (f"문서 하나를 요약합니다.\n\n[{i + 1}] {m.label}\n{m.text}\n\n"
            "작성할 것: 이 문서가 무엇을 다루고 신입 개발자가 왜 읽어야 하는지 2~3문장. "
            f"각 문장 끝에 [{i + 1}] 을 붙입니다. 제목 없이 문장만 출력합니다.")
    return llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                    model=model, temperature=0.2, num_predict=300)


# ── 조립 ─────────────────────────────────────────────────────

def _entry_sections(c: Collected) -> tuple[str, str]:
    a = c.analysis
    if not a["entry_points"]:
        return "진입점 후보를 찾지 못했습니다 (파일명 규칙·main 표식 기준).", ""
    ep_lines = ["| 파일 | 판정 근거 |", "|---|---|"]
    for e in a["entry_points"]:
        ep_lines.append(f"| `{e['path']}` | {e['reason']} |")
    fn_parts = []
    for e in a["entry_points"]:
        fn_parts.append(f"### `{e['path']}`")
        if e.get("routes"):
            fn_parts.append("| method | path | handler |")
            fn_parts.append("|---|---|---|")
            for r in e["routes"]:
                fn_parts.append(f"| {r['method']} | `{r['path']}` | `{r['handler'] or '?'}` (L{r['line']}) |")
            fn_parts.append("")
        if e.get("routers"):
            fn_parts.append("포함된 라우터: " + ", ".join(
                f"`{r['router']}`" + (f" (prefix `{r['prefix']}`)" if r["prefix"] else "") for r in e["routers"]))
            fn_parts.append("")
        syms = [s for s in e.get("symbols", []) if s["kind"] in ("function", "method", "class")]
        if syms:
            for s in syms[:60]:
                doc = f" — {s['doc']}" if s.get("doc") else ""
                qualified = f"**`{s['symbol']}`** — " if "." in s["symbol"] else ""
                fn_parts.append(f"- L{s['line_start']} {qualified}`{s['signature']}`{doc}")
            if len(syms) > 60:
                fn_parts.append(f"- … 총 {len(syms)}개 중 60개 표시")
        elif e["path"].endswith(".py"):
            fn_parts.append("- (함수 정의 없음 또는 파싱 실패)")
        else:
            fn_parts.append("- (Python 이외 파일은 함수 목록을 추출하지 않습니다)")
        fn_parts.append("")
    return "\n".join(ep_lines), "\n".join(fn_parts).rstrip()


def _split_overview(text: str) -> tuple[str, str]:
    """LLM 출력에서 '## 이 프로젝트는' 과 '## 기능 목록' 본문을 분리."""
    m1 = re.search(r"##\s*이 프로젝트는\s*\n(.*?)(?=\n##\s|\Z)", text, re.S)
    m2 = re.search(r"##\s*기능 목록\s*\n(.*?)(?=\n##\s|\Z)", text, re.S)
    ov = (m1.group(1) if m1 else text).strip()
    feats = (m2.group(1) if m2 else "").strip() or "문서에서 확인되지 않음"
    return ov, feats


def assemble(c: Collected, overview: str, doc_summaries: dict[int, str]) -> str:
    a = c.analysis
    ov, feats = _split_overview(overview)
    ep_table, fn_list = _entry_sections(c)
    out = [f"# {a['name']}", "", "## 이 프로젝트는", "", ov, ""]
    out += _readme_sections(c)
    out += ["", "## 문서 요약", ""]
    if doc_summaries:
        for i, s in doc_summaries.items():
            out.append(f"- **`{c.materials[i].path}`** — {s.strip()}")
    else:
        out.append("요약할 문서를 찾지 못했습니다.")
    if c.truncated:
        out.append(f"- (예산 때문에 요약하지 않은 문서: {', '.join(f'`{t}`' for t in c.truncated)})")
    out += ["", "## 진입점", "", ep_table, "", "## 진입점별 함수 목록", "", fn_list or "(없음)",
            "", "## 기능 목록", "", feats]
    return "\n".join(out).rstrip() + "\n"


# ── 저장 / 조회 ──────────────────────────────────────────────

def _dir() -> Path:
    d = CFG.briefings_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(project_id: str, ext: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", project_id)
    return _dir() / f"{safe}.{ext}"


def md_path(project_id: str) -> Path:
    return _path(project_id, "md")


def to_contexts(c: Collected) -> list[dict]:
    return [{"path": m.path or "", "type": m.type, "line_start": m.line_start, "line_end": m.line_end,
             "section": None, "text": m.text, "score": 1.0} for m in c.materials]


def save(project_id: str, text: str, c: Collected, *, commit: str | None, model: str,
         elapsed_s: float) -> dict:
    refs = build_references(to_contexts(c), answer=text, cited_only=True, include_text=False)
    rec = {
        "ok": True, "project_id": project_id, "briefing": text, "model": model,
        "references": refs["references"], "reference_files": refs["reference_files"], "cited": refs["cited"],
        "structure": {k: c.analysis[k] for k in ("name", "total_files", "total_dirs", "key_dirs", "entry_points",
                                                  "docs", "configs", "ext_counts")},
        "routes": c.analysis.get("routes", []),
        "readme_summary": c.readme_summary,
        "feature_grounding": c.feature_grounding,
        "commit": commit,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "materials": [m.label for m in c.materials], "truncated": c.truncated,
        "elapsed_s": round(elapsed_s, 1), "md_path": str(_path(project_id, "md")),
    }
    _path(project_id, "json").write_text(json.dumps(rec, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    front = ["---", f"project_id: {project_id}", f"generated_at: {rec['generated_at']}",
             f"commit: {commit or '(없음)'}", f"model: {model}", "---", ""]
    body = [text, "", "## 근거", ""]
    for r in rec["reference_files"]:
        spans = ", ".join(f"L{a}-{b}" for a, b in r["lines"]) or "-"
        body.append(f"- `{r['path']}`  [{','.join(map(str, r['citations']))}]  {spans}")
    _path(project_id, "md").write_text("\n".join(front + body) + "\n", encoding="utf-8")
    return rec


def load(project_id: str) -> dict | None:
    p = _path(project_id, "json")
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def build(project_root: str, project_id: str, *, model: str | None = None,
          commit: str | None = None) -> dict:
    """수집 → 결정적 분석 → LLM 요약(개요 1회 + 문서별 1회) → 조립 → 저장."""
    t0 = time.perf_counter()
    chosen = llm.resolve_model(model, purpose="briefing")
    c = collect(project_root)
    if not c.materials and not c.analysis.get("entry_points"):
        return {"ok": False, "reason": "no_material", "message": "프로젝트 문서·진입점을 찾을 수 없습니다"}
    c.readme_summary = gen_readme_summary(c, chosen) if c.analysis.get("readme") else {
        "project_summary": "", "core_features": [], "status": "no_readme"
    }
    c.feature_grounding = (ground_features(project_id, c.readme_summary, chosen)
                           if c.readme_summary.get("core_features") else [])
    overview = gen_overview(c, chosen) if c.materials else "## 이 프로젝트는\n문서에서 확인되지 않음\n\n## 기능 목록\n문서에서 확인되지 않음"
    summaries: dict[int, str] = {}
    for i, m in enumerate(c.materials):
        if m.type == "doc":
            try:
                summaries[i] = gen_doc_summary(c, i, chosen)
            except Exception as e:                 # 문서 하나의 실패가 브리핑 전체를 막지 않게
                summaries[i] = f"(요약 실패: {type(e).__name__}) [{i + 1}]"
    text = assemble(c, overview, summaries)
    return save(project_id, text, c, commit=commit, model=chosen, elapsed_s=time.perf_counter() - t0)
