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

from . import analysis, llm
from .chunker import read_text
from .config import CFG
from .references import build_references

BUDGET_TOKENS = 6_000
LIMIT_README = 3_000
LIMIT_DOC_CHARS = 3_500
LIMIT_DOC_FILES = 6
LIMIT_CONFIG = 600
LIMIT_ENTRY_LINES = 40


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
    out = [f"# {a['name']}", "", "## 이 프로젝트는", "", ov, "", "## 문서 요약", ""]
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
