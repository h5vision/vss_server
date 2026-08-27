"""python -m vss.eval validate|run|report|runs|sweep <인자>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import runner, sweep as sweep_mod
from .suite import ValidationError


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vss.eval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate"); p.add_argument("matrix")
    p = sub.add_parser("run"); p.add_argument("matrix"); p.add_argument("--note")
    p = sub.add_parser("report"); p.add_argument("result")
    sub.add_parser("runs")
    p = sub.add_parser("sweep", help="저장된 run 으로 임계값 후보별 confusion matrix (값은 바꾸지 않습니다)")
    p.add_argument("run", help="run_id (앞부분만도 됨) 또는 run JSON 경로")
    p.add_argument("--min", type=float, default=0.30); p.add_argument("--max", type=float, default=0.80)
    p.add_argument("--step", type=float, default=0.02)
    p.add_argument("--detail", action="store_true", help="셀별 전체 표까지 출력")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "validate":
            v = runner.validate_matrix(a.matrix)
            print(f"OK  문항 {len(v['questions'])}  suite_hash {v['suite_hash']}")
            return 0
        if a.cmd == "run":
            r = runner.run_matrix(a.matrix, note=a.note)
            print(f"\n결과: {r['path']}\n보고서: {r['report']}")
            return 0
        if a.cmd == "report":
            r = json.loads(Path(a.result).read_text(encoding="utf-8"))
            print(runner.render_report(r))
            return 0
        if a.cmd == "runs":
            for row in runner.list_runs():
                print(f"{row['run_id']}  {row['matrix']:24s} {row['cell']:20s} {row['search']:7s} {row['mode']:9s} "
                      f"n={row['n']}  H@1={runner._pct(row['hit@1'])} H@3={runner._pct(row['hit@3'])} "
                      f"MRR={runner._num(row['mrr'])}  suite={row['suite_hash']}")
            return 0
        if a.cmd == "sweep":
            run = sweep_mod.load_run(a.run)
            print(sweep_mod.render(run, lo=a.min, hi=a.max, step=a.step, detail=a.detail))
            return 0
    except ValidationError as e:
        for err in e.errors:
            print("ERR ", err)
        for w in e.warnings:
            print("WARN", w)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
