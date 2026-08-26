"""
구 gold(RAG_TEST_*.md, rag_lab eval.py 형식) → JSONL suite 변환.

    python -m vss.eval.convert_gold RAG_TEST_fastapi_cli.md evaluation/suites/fastapi-cli-full.jsonl --prefix fastcli

유형 매핑: 1~4 = 답 있음 / 5 = 답 없음(no_evidence). 태그는 유형으로 근사합니다.
    1 함수·심볼 설명 → exact_symbol, semantic
    2 흐름·진입점   → architecture, semantic
    3 코드 위치     → code_vs_docs, semantic
    4 문서·설정     → semantic
    5 답 없음       → no_evidence
변환 뒤 `python -m vss.eval validate <matrix>` 로 실제 파일·줄 범위를 검증하세요.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_LOC = re.compile(r"^\s*-\s+(?P<path>[\w./\\-]+\.\w+)(?:\s+(?P<a>\d+)\s*~\s*(?P<b>\d+)\s*줄|\s+(?P<c>\d+)\s*줄)?", re.M)
_TAGS = {1: ["exact_symbol", "semantic", "korean"], 2: ["architecture", "semantic", "korean"],
         3: ["code_vs_docs", "semantic", "korean"], 4: ["semantic", "korean"], 5: ["no_evidence", "korean"]}


def parse_gold_md(text: str) -> list[dict]:
    blocks = re.split(r"(?m)^## Q", text)[1:]
    out = []
    for i, b in enumerate(blocks, 1):
        m_t = re.search(r"(?m)^- 유형:\s*(\d)", b)
        m_q = re.search(r"(?m)^- 질문:\s*(.+?)\s*$", b)
        if not (m_t and m_q):
            continue
        qtype = int(m_t.group(1))
        m_loc = re.search(r"(?m)^- 정답 위치:(.*)$", b)
        inline = (m_loc.group(1).strip() if m_loc else "")
        no_answer = (qtype == 5) or ("없음" in inline)
        locs = []
        if not no_answer and m_loc:
            tail = b[m_loc.end():]
            stop = re.search(r"(?m)^- \S+:", tail)
            region = tail[:stop.start()] if stop else tail
            for mm in _LOC.finditer(region):
                a = mm.group("a") or mm.group("c")
                bb = mm.group("b") or mm.group("c")
                g = {"path": mm.group("path").replace("\\", "/")}
                if a and bb:
                    g["line_start"], g["line_end"] = int(a), int(bb)
                locs.append(g)
        m_note = re.search(r"(?m)^- 한 줄 요약:\s*(.+?)\s*$", b)
        out.append({"n": i, "type": qtype, "question": m_q.group(1).strip(), "answerable": not no_answer,
                    "gold": locs, "note": m_note.group(1).strip() if m_note else None})
    return out


def convert(src: Path, dst: Path, prefix: str) -> int:
    qs = parse_gold_md(src.read_text(encoding="utf-8"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with dst.open("w", encoding="utf-8") as f:
        for q in qs:
            if q["answerable"] and not q["gold"]:
                continue        # 답 있음인데 위치가 파싱되지 않은 문항은 제외 (검증에서 걸립니다)
            rec = {"id": f"{prefix}-q{q['n']:03d}", "question": q["question"], "answerable": q["answerable"],
                   "type": q["type"], "gold": q["gold"] if q["answerable"] else [],
                   "tags": _TAGS.get(q["type"], ["semantic", "korean"])}
            if q.get("note"):
                rec["note"] = q["note"]
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("dst"); ap.add_argument("--prefix", default="q")
    a = ap.parse_args(argv)
    n = convert(Path(a.src), Path(a.dst), a.prefix)
    print(f"{n} 문항 → {a.dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
