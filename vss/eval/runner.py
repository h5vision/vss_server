"""
평가 실행 — matrix(JSON) × suite(JSONL) → data/evaluation/runs/<run_id>.json + reports/<run_id>.md

matrix 형식 (rag_lab 계약을 단순화: 인덱스는 미리 만들어 둔 project_id 를 명시합니다)
{
  "schema_version": "2.0",
  "name": "api-test-v1",
  "repository": "/srv/repos/api_test",            # commit 기록용 (없어도 됨)
  "suite": "../suites/api-test.jsonl",
  "search_profiles": [
    {"name": "vector", "use_bm25": false, "pool": 20, "top_k": 4, "threshold": 0.54},
    {"name": "hybrid", "use_bm25": true,  "pool": 20, "top_k": 4, "threshold": 0.54}
  ],
  "cells": [
    {"project_id": "api-test--ast", "label": "ast+header", "search_profile": "vector", "modes": ["retrieval", "pipeline"]},
    {"project_id": "api-test--ast", "label": "ast+header", "search_profile": "hybrid", "modes": ["retrieval", "pipeline"]}
  ]
}

모드
  retrieval  벡터(+BM25 RRF) 순위만. threshold 미적용. Hit@k · MRR
  pipeline   search() 전체(threshold 포함). has_evidence · NO_EVIDENCE recall · 최종 contexts 기준 Hit@k

비교 규칙: repository commit · suite hash · 인덱스 fingerprint 가 같을 때만 셀끼리 비교합니다. 결과에 전부 기록합니다.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import lexical, search as search_mod
from ..config import CFG
from ..embedder import embed_one
from ..indexer import git_head
from ..store import VectorStore, get_store
from .metrics import by_tag, summarize
from .suite import ValidationError, canonical_hash, first_gold_rank, load_questions


def _read_matrix(path: str | Path) -> tuple[Path, dict]:
    p = Path(path).resolve()
    m = json.loads(p.read_text(encoding="utf-8"))
    return p, m


def validate_matrix(path: str | Path, *, store: VectorStore | None = None) -> dict:
    p, m = _read_matrix(path)
    errors: list[str] = []
    for k in ("name", "suite", "search_profiles", "cells"):
        if k not in m:
            errors.append(f"matrix 에 {k} 가 없습니다")
    if errors:
        raise ValidationError(errors)
    suite_path = (p.parent / m["suite"]).resolve()
    repo = m.get("repository")
    questions = load_questions(suite_path, repository=repo if repo and Path(repo).is_dir() else None)
    profiles = {sp["name"]: sp for sp in m["search_profiles"]}
    st = store or get_store()
    available = set(st.projects())
    for i, c in enumerate(m["cells"], 1):
        if c.get("search_profile") not in profiles:
            errors.append(f"cells[{i}] search_profile 미등록: {c.get('search_profile')}")
        if not c.get("project_id"):
            errors.append(f"cells[{i}] project_id 없음")
        elif c["project_id"] not in available:
            errors.append(f"cells[{i}] 인덱스 없음: {c['project_id']} (available={sorted(available)})")
        for mode in c.get("modes", ["retrieval"]):
            if mode not in ("retrieval", "pipeline"):
                errors.append(f"cells[{i}] 알 수 없는 mode: {mode}")
    if errors:
        raise ValidationError(errors)
    return {"matrix": p, "questions": questions, "suite_path": suite_path,
            "suite_hash": canonical_hash(questions), "profiles": profiles}


def _retrieval_rows(st: VectorStore, project_id: str, profile: dict, search: dict, questions: list[dict]) -> list[dict]:
    idx = None
    if search.get("use_bm25"):
        idx = lexical.BM25.load(lexical.index_path(project_id))
        if idx is None or len(idx.doc_ids) != st.count(project_id):
            raise RuntimeError(f"{project_id}: BM25 가 없거나 청크 수와 다릅니다 (python -m vss.cli bm25)")
    rows = []
    for q in questions:
        t0 = time.perf_counter()
        vec = embed_one(q["question"], model=profile["embed_model"], expected_dim=int(profile["embed_dim"]))
        hits = st.query(project_id, vec, int(search.get("pool", CFG.fusion_pool)))
        if idx is not None:
            lex = idx.search(q["question"], int(search.get("pool", CFG.fusion_pool)))
            fused = lexical.rrf_fuse(hits, lex, k=int(search.get("rrf_k", CFG.rrf_k)))
            by_id = {h["_id"]: h for h in hits}
            missing = [cid for cid, _ in lex if cid not in by_id]
            by_id.update(st.get_by_ids(project_id, missing[:int(search.get("pool", CFG.fusion_pool))]))
            hits = sorted((by_id[cid] for cid in fused if cid in by_id), key=lambda h: -fused[h["_id"]])
        rows.append({"id": q["id"], "question": q["question"], "answerable": q["answerable"], "tags": q["tags"],
                     "rank": first_gold_rank(hits, q["gold"]) if q["answerable"] else None,
                     "top_score": max((h["score"] for h in hits), default=None),
                     "ranked1_path": hits[0]["path"] if hits else None,
                     "top_paths": [h["path"] for h in hits[:5]],
                     "ms": round((time.perf_counter() - t0) * 1000, 1)})
    return rows


def _pipeline_rows(st: VectorStore, project_id: str, search: dict, questions: list[dict]) -> list[dict]:
    rows = []
    for q in questions:
        t0 = time.perf_counter()
        r = search_mod.search(q["question"], project_id, top_k=search.get("top_k"),
                              threshold=search.get("threshold"), store=st, search_profile=search)
        if search.get("use_bm25") and r.get("all_hits") and not r.get("bm25_active"):
            raise RuntimeError(f"{project_id}: hybrid 셀인데 bm25_active=false 입니다")
        ctx = r["contexts"]
        rows.append({"id": q["id"], "question": q["question"], "answerable": q["answerable"], "tags": q["tags"],
                     "rank": first_gold_rank(ctx, q["gold"]) if q["answerable"] else None,
                     "has_evidence": r["has_evidence"], "reason": r["reason"], "top_score": r["top_score"],
                     "bm25_active": r.get("bm25_active", False),
                     "ranked1_path": ctx[0]["path"] if ctx else None,
                     "passed_paths": [c["path"] for c in ctx],
                     "ms": round((time.perf_counter() - t0) * 1000, 1)})
    return rows


def run_matrix(path: str | Path, *, store: VectorStore | None = None, note: str | None = None) -> dict:
    st = store or get_store()
    v = validate_matrix(path, store=st)
    p, m = _read_matrix(path)
    questions = v["questions"]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + canonical_hash(m)[:6]
    repo = m.get("repository")
    result = {
        "run_id": run_id, "matrix": m["name"], "matrix_hash": canonical_hash(m), "note": note,
        "repository": repo, "commit": git_head(repo) if repo and Path(repo).is_dir() else None,
        "suite": str(v["suite_path"]), "suite_hash": v["suite_hash"], "questions": len(questions),
        "store": st.kind, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cells": [],
    }
    for c in m["cells"]:
        pid = c["project_id"]
        info = st.project_info(pid) or {}
        profile = info.get("fingerprint") or {}
        search = dict(v["profiles"][c["search_profile"]])
        cell = {"project_id": pid, "label": c.get("label") or pid, "search_profile": c["search_profile"],
                "search": search, "index_fingerprint": profile, "index_commit": info.get("commit"),
                "chunks": info.get("chunks"), "modes": {}}
        for mode in c.get("modes", ["retrieval"]):
            t0 = time.perf_counter()
            rows = (_retrieval_rows(st, pid, profile, search, questions) if mode == "retrieval"
                    else _pipeline_rows(st, pid, search, questions))
            cell["modes"][mode] = {"summary": summarize(rows, mode), "by_tag": by_tag(rows, mode),
                                   "rows": rows, "elapsed_s": round(time.perf_counter() - t0, 1)}
            print(f"  {cell['label']:24s} {c['search_profile']:7s} {mode:9s} "
                  f"Hit@3={_pct(cell['modes'][mode]['summary'].get('hit@3'))} "
                  f"MRR={_num(cell['modes'][mode]['summary'].get('mrr'))}")
        result["cells"].append(cell)
    result["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out_dir = CFG.eval_dir() / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{run_id}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    result["path"] = str(out)
    report = write_report(result)
    result["report"] = str(report)
    return result


def _pct(v):
    return "—" if v is None else f"{v:.1%}"


def _num(v):
    return "—" if v is None else f"{v:.3f}"


def render_report(result: dict) -> str:
    n_ans = None
    lines = [f"# RAG 실험 보고서 — {result['matrix']}", "",
             f"- run_id: `{result['run_id']}`", f"- commit: `{result.get('commit')}`",
             f"- suite: `{Path(result['suite']).name}` (hash `{result['suite_hash']}`, 문항 {result['questions']})",
             f"- store: `{result.get('store')}`" + (f"  note: {result['note']}" if result.get("note") else ""), "",
             "| cell | search | mode | n | Hit@1 | Hit@3 | Hit@5 | MRR | 답가능 통과 | no-evidence recall | p95 ms |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for c in result["cells"]:
        for mode, m in c["modes"].items():
            s = m["summary"]
            n_ans = s.get("answerable")
            lines.append(f"| `{c['label']}` | {c['search_profile']} | {mode} | {n_ans} | {_pct(s.get('hit@1'))} | "
                         f"{_pct(s.get('hit@3'))} | {_pct(s.get('hit@5'))} | {_num(s.get('mrr'))} | "
                         f"{_pct(s.get('answerable_gate_recall'))} | {_pct(s.get('no_evidence_recall'))} | "
                         f"{s.get('latency_ms', {}).get('p95')} |")
    if n_ans:
        lines += ["", f"> 답 있는 문항 n={n_ans}. 문항 하나가 움직이는 폭은 {1 / n_ans:.1%} 입니다. 그보다 작은 차이는 노이즈입니다."]
    lines += ["", "## 조건", ""]
    for c in result["cells"]:
        lines.append(f"- `{c['label']}` ({c['project_id']}, {c.get('chunks')}청크, commit `{(c.get('index_commit') or '')[:8]}`): "
                     f"fingerprint `{json.dumps(c['index_fingerprint'], ensure_ascii=False)}`; search `{json.dumps(c['search'])}`")
    lines += ["", "## 실패·거절 관측", ""]
    for c in result["cells"]:
        for mode, m in c["modes"].items():
            miss = [r for r in m["rows"] if r["answerable"] and not r.get("rank")]
            fp = [r for r in m["rows"] if not r["answerable"] and r.get("has_evidence")]
            fn = [r for r in m["rows"] if r["answerable"] and mode == "pipeline" and not r.get("has_evidence")]
            if not (miss or fp or fn):
                continue
            lines.append(f"### {c['label']} / {c['search_profile']} / {mode}")
            for r in miss:
                lines.append(f"- 정답 미검색: `{r['id']}` — 1위 `{r.get('ranked1_path')}`")
            for r in fn:
                lines.append(f"- 답 가능한데 임계값 미달: `{r['id']}` — top {r.get('top_score')}")
            for r in fp:
                lines.append(f"- 잘못된 근거 통과: `{r['id']}` — `{r.get('ranked1_path')}` (top {r.get('top_score')})")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(result: dict, output: str | Path | None = None) -> Path:
    out = Path(output) if output else CFG.eval_dir() / "reports" / f"{result['run_id']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(result), encoding="utf-8")
    return out


def list_runs() -> list[dict]:
    d = CFG.eval_dir() / "runs"
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for c in r.get("cells", []):
            for mode, m in c.get("modes", {}).items():
                s = m.get("summary", {})
                out.append({"run_id": r["run_id"], "matrix": r["matrix"], "suite_hash": r.get("suite_hash"),
                            "commit": r.get("commit"), "cell": c["label"], "search": c["search_profile"],
                            "mode": mode, "n": s.get("answerable"), "hit@1": s.get("hit@1"),
                            "hit@3": s.get("hit@3"), "hit@5": s.get("hit@5"), "mrr": s.get("mrr"),
                            "no_evidence_recall": s.get("no_evidence_recall"),
                            "fingerprint": c.get("index_fingerprint"), "started_at": r.get("started_at")})
    return out
