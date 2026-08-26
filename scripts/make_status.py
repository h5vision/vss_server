"""
STATUS.md 생성 — 인덱스 목록(저장소) + 평가 run 이력(data/evaluation/runs) + 최근 index_log. 손으로 고치지 않습니다.

    python scripts/make_status.py            # STATUS.md 갱신
    python scripts/make_status.py --csv out.csv   # 발표 그래프용 표
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vss.config import CFG  # noqa: E402
from vss.eval.runner import list_runs  # noqa: E402


def pct(v):
    return "—" if v is None else f"{v:.1%}"


def num(v):
    return "—" if v is None else f"{v:.3f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "STATUS.md"))
    ap.add_argument("--csv")
    ap.add_argument("--no-store", action="store_true", help="저장소 접속 없이 평가 이력만")
    a = ap.parse_args(argv)

    lines = ["<!-- 자동 생성: python scripts/make_status.py — 손으로 고치지 마세요 -->",
             f"# STATUS — {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M %Z')}", "",
             f"store=`{CFG.store}` · chat=`{CFG.chat_model}` · 기본 fingerprint=`{json.dumps(CFG.fingerprint(), ensure_ascii=False)}`", ""]

    if not a.no_store:
        try:
            from vss.indexer import list_projects
            from vss.store import get_store
            st = get_store()
            rows = list_projects(st)
            lines += ["## 인덱스", "", "| project_id | 청크 | chunker | header | bm25 | commit | indexed_at |", "|---|---:|---|---|---|---|---|"]
            for r in rows:
                lines.append(f"| `{r['project_id']}` | {r['chunks'] or 0:,} | {r['chunker']} | {'on' if r['context_header'] else 'off'} | "
                             f"{'on' if r['use_bm25'] else 'off'}({r['bm25_docs'] or 0}) | `{(r['commit'] or '')[:8]}` | {r['indexed_at'] or ''} |")
            inc = st.incomplete()
            if inc:
                lines += ["", "⚠ 미완성 빌드: " + ", ".join(i["name"] for i in inc)]
            lines.append("")
        except Exception as e:
            lines += [f"(저장소 접속 실패: {e})", ""]

    runs = list_runs()
    lines += ["## 평가 이력 (data/evaluation/runs)", ""]
    if not runs:
        lines.append("(run 없음)")
    else:
        lines += ["| 시작 | run_id | matrix | cell | search | mode | n | Hit@1 | Hit@3 | Hit@5 | MRR | no-evidence recall | suite |",
                  "|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|"]
        for r in runs[-60:]:
            lines.append(f"| {(r['started_at'] or '')[:16]} | `{r['run_id']}` | {r['matrix']} | {r['cell']} | {r['search']} | {r['mode']} | "
                         f"{r['n']} | {pct(r['hit@1'])} | {pct(r['hit@3'])} | {pct(r['hit@5'])} | {num(r['mrr'])} | "
                         f"{pct(r['no_evidence_recall'])} | `{r['suite_hash']}` |")
        lines += ["", "> 같은 matrix·suite hash·commit 끼리만 비교합니다. n 이 답 있는 문항 수이고 1/n 보다 작은 차이는 노이즈입니다."]

    log = CFG.data_path() / "index_log.jsonl"
    if log.exists():
        recs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()][-10:]
        lines += ["", "## 최근 인덱싱 이력 (data/index_log.jsonl)", ""]
        for r in recs:
            lines.append(f"- {r.get('indexed_at') or ''} `{r.get('project_id')}` {r.get('event')} "
                         f"chunks={r.get('chunk_count')} elapsed={r.get('elapsed_s')}s {('error=' + str(r.get('error'))) if r.get('error') else ''}")

    Path(a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"→ {a.out}")
    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["started_at", "run_id", "matrix", "cell", "search", "mode", "n", "hit@1", "hit@3",
                                              "hit@5", "mrr", "no_evidence_recall", "suite_hash", "commit"])
            w.writeheader()
            for r in runs:
                w.writerow({k: r.get(k) for k in w.fieldnames})
        print(f"→ {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
