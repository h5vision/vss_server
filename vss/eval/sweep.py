"""임계값 sweep — 이미 저장된 run 을 다시 세어 threshold 후보별 confusion matrix 를 냅니다.

재인덱싱도 재질의도 하지 않습니다. run 에 기록된 문항별 `top_score` 와 `answerable` 만 쓰는 산수입니다
(근거 있음 판정은 `top_score >= threshold` 비교 하나 — 불변 조건 5).

⚠ 값을 바꾸지 않습니다. 표만 냅니다. 임계값 변경은 md 가 DECISIONS 에 남기고 `.env` 를 고칩니다.

읽는 법
  gate_recall   답이 있는 문항 중 임계값을 통과한 비율. 낮으면 "근거를 찾지 못했습니다" 화면이 헛되이 뜬다
  no_ev_recall  답이 없는 문항(hard negative) 중 제대로 막은 비율. 낮으면 근거 없이 답하려 든다
  hit@3(gate)   통과 + 정답이 top-3 안. 파이프라인 전체의 성적이고 임계값이 낮을수록 무조건 좋아지므로
                단독 목적함수가 될 수 없다. no_ev_recall 과의 균형점을 봐야 한다
  bal_acc       (gate_recall + no_ev_recall) / 2. 둘을 같은 무게로 볼 때의 최적점
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import CFG
from .suite import ValidationError

# 통과/차단 비율은 분모가 작으면 문항 하나에 크게 흔들립니다. 그 아래면 경고를 붙입니다.
MIN_CLASS = 10


def load_run(ref: str | Path) -> dict:
    """run_id(앞부분만도 됨) 또는 파일 경로로 run JSON 을 읽습니다."""
    p = Path(ref)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    d = CFG.eval_dir() / "runs"
    hits = sorted(d.glob(f"{ref}*.json")) if d.exists() else []
    if not hits:
        raise ValidationError([f"run 을 찾을 수 없습니다: {ref}  (python -m vss.eval runs 로 확인)"])
    return json.loads(hits[-1].read_text(encoding="utf-8"))


def cells(run: dict) -> list[dict]:
    """셀마다 임계값과 무관한 행(retrieval 우선)을 꺼냅니다.

    retrieval 모드는 threshold 를 적용하지 않은 순위 결과라 sweep 의 기준으로 맞습니다.
    없으면 pipeline 행을 씁니다 — top_score 자체는 어느 모드에서나 pool 의 최대 벡터 점수입니다.
    """
    out = []
    for c in run.get("cells", []):
        modes = c.get("modes", {})
        mode = "retrieval" if "retrieval" in modes else next(iter(modes), None)
        if not mode:
            continue
        out.append({"label": c.get("label") or c.get("project_id"), "project_id": c.get("project_id"),
                    "search": c.get("search_profile"), "mode": mode, "rows": modes[mode].get("rows", [])})
    return out


def confusion(rows: list[dict], t: float) -> dict:
    tp = fn = fp = tn = hit3 = 0
    for r in rows:
        score = r.get("top_score")
        passed = score is not None and score >= t
        if r.get("answerable"):
            if passed:
                tp += 1
                rank = r.get("rank")
                if rank and rank <= 3:
                    hit3 += 1
            else:
                fn += 1
        elif passed:
            fp += 1
        else:
            tn += 1
    pos, neg = tp + fn, tn + fp
    gate = tp / pos if pos else None
    noev = tn / neg if neg else None
    return {"threshold": t, "tp": tp, "fn": fn, "fp": fp, "tn": tn,
            "answerable": pos, "no_answer": neg,
            "gate_recall": gate, "no_ev_recall": noev,
            "hit@3": hit3 / pos if pos else None,
            "bal_acc": ((gate + noev) / 2) if (gate is not None and noev is not None) else None}


def grid(lo: float = 0.30, hi: float = 0.80, step: float = 0.02) -> list[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 4) for i in range(n + 1)]


def sweep_cell(rows: list[dict], thresholds: list[float]) -> list[dict]:
    return [confusion(rows, t) for t in thresholds]


def best(table: list[dict]) -> dict | None:
    """bal_acc 최대점. 동점이면 임계값이 높은 쪽(= 근거 없음을 더 막는 쪽)을 고릅니다."""
    scored = [r for r in table if r["bal_acc"] is not None]
    if not scored:
        return None
    top = max(r["bal_acc"] for r in scored)
    return [r for r in scored if r["bal_acc"] == top][-1]


def nearest(table: list[dict], t: float) -> dict:
    return min(table, key=lambda r: abs(r["threshold"] - t))


def _p(v) -> str:
    return "—" if v is None else f"{v:5.1%}"


def render(run: dict, *, lo=0.30, hi=0.80, step=0.02, detail: bool = False,
           current: float | None = None) -> str:
    cur = CFG.score_threshold if current is None else current
    ts = grid(lo, hi, step)
    lines = [f"임계값 sweep — run {run.get('run_id')}  matrix={run.get('matrix')}  store={run.get('store')}",
             f"  suite_hash={run.get('suite_hash')}  commit={(run.get('commit') or '-')[:8]}  현재 임계값={cur}",
             "  ⚠ 표만 냅니다. 값은 바꾸지 않습니다.", ""]
    summary = []
    for c in cells(run):
        table = sweep_cell(c["rows"], ts)
        b, now = best(table), nearest(table, cur)
        name = f"{c['label']} · {c['search']}"
        summary.append((name, now, b, table[0]))
        if detail:
            lines.append(f"[{name}]  답있음 {now['answerable']}문항 · 답없음 {now['no_answer']}문항")
            if now["answerable"] < MIN_CLASS or now["no_answer"] < MIN_CLASS:
                lines.append(f"  ⚠ 분모가 {MIN_CLASS}문항 미만입니다 — 문항 하나가 크게 흔들립니다. 판정에 쓰지 마세요")
            lines.append("   thr    통과/답있음   막음/답없음   gate_recall  no_ev_recall  hit@3(gate)  bal_acc")
            for r in table:
                mark = " ←현재" if r is now else (" ←최적" if r is b else "")
                lines.append(f"  {r['threshold']:.2f}   {r['tp']:>3}/{r['answerable']:<3}      "
                             f"{r['tn']:>3}/{r['no_answer']:<3}      {_p(r['gate_recall'])}       "
                             f"{_p(r['no_ev_recall'])}      {_p(r['hit@3'])}      {_p(r['bal_acc'])}{mark}")
            lines.append("")

    lines.append("요약 — 셀별 현재값 vs bal_acc 최적값")
    lines.append("  셀                                현재 thr  gate   no_ev  hit@3 | 최적 thr  gate   no_ev  hit@3  (bal_acc)")
    for name, now, b, _ in summary:
        if b is None:
            lines.append(f"  {name:32s}  (답없음 문항이 없어 계산 불가)")
            continue
        lines.append(f"  {name:32s}  {now['threshold']:.2f}   {_p(now['gate_recall'])} {_p(now['no_ev_recall'])} {_p(now['hit@3'])} | "
                     f"  {b['threshold']:.2f}   {_p(b['gate_recall'])} {_p(b['no_ev_recall'])} {_p(b['hit@3'])}  ({_p(b['bal_acc'])})")
    warn = {n for n, now, _, _ in summary if now["answerable"] < MIN_CLASS or now["no_answer"] < MIN_CLASS}
    if warn:
        lines.append("")
        lines.append(f"  ⚠ 분모 {MIN_CLASS}문항 미만이라 판정에 쓸 수 없는 셀: {', '.join(sorted(warn))}")
    return "\n".join(lines)
