"""
명령줄 — python -m vss.cli <명령>

  health                         Ollama · 저장소 · 설정 확인
  projects [--json]              완성 인덱스 목록 (--json: README 갱신용 스냅샷)
  index <경로|--git URL> --project <id> [--force] [--no-briefing] [--context-header on|off] [--bm25 on|off] [--exclude "a,b/**"]
  status --project <id>
  search "<질문>" --project <id> [--top-k 4] [--threshold 0.54] [--bm25 on|off]
  ask "<질문>" --project <id> [--model m] [--no-rag] [--json]
  briefing --project <id> [--force] [--model m]
  bm25 --project <id>            역색인 재구축
  repair [--apply]               미완성 빌드 정리
  doctor                         인덱스 ↔ BM25 ↔ 설정 대조
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path

from . import briefing, chat, embedder, indexer, llm, search as search_mod
from .config import CFG
from .store import get_store


def _onoff(v: str | None) -> bool | None:
    if v is None:
        return None
    return v.lower() in ("1", "on", "true", "yes")


def cmd_health(a) -> int:
    print(f"store   : {CFG.store}  data_dir={CFG.data_path()}")
    print(f"Ollama  : {CFG.ollama_url}")
    try:
        models = embedder.health()
        print(f"모델    : {models}")
        need = [CFG.embed_model, CFG.chat_model]
        missing = [m for m in need if m not in models and m.split(':')[0] not in {x.split(':')[0] for x in models}]
        if missing:
            print(f"  ⚠ 없는 모델: {missing}  →  ollama pull ...")
        v = embedder.embed_one("health check")
        print(f"임베딩  : dim={len(v)} OK")
    except Exception as e:
        print(f"  !! Ollama 실패: {e}")
    try:
        st = get_store()
        print(f"인덱스  : {st.projects()}")
        inc = st.incomplete()
        if inc:
            print(f"  ⚠ 미완성 빌드: {[i['name'] for i in inc]}")
    except Exception as e:
        print(f"  !! 저장소 실패: {e}")
    print(f"설정    : chat={CFG.chat_model} top_k={CFG.top_k} threshold={CFG.score_threshold} "
          f"chunker={CFG.chunker} header={CFG.context_header} bm25={CFG.use_bm25} exclude='{CFG.exclude_globs}'")
    return 0


def cmd_projects(a) -> int:
    rows = indexer.list_projects()
    if getattr(a, "json", False):
        print(json.dumps({"host": socket.gethostname(), "store": CFG.store, "projects": rows}, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("(인덱스 없음)")
    for r in rows:
        print(f"{r['project_id']:32s} {r['chunks'] or 0:>7,}청크  bm25={'on' if r['use_bm25'] else 'off'}"
              f"({r['bm25_docs'] or 0})  header={'on' if r['context_header'] else 'off'}  "
              f"chunker={r['chunker']}  commit={(r['commit'] or '')[:8]}  {r['indexed_at'] or ''}")
    return 0


def cmd_index(a) -> int:
    root = a.path
    if a.git:
        dest = CFG.data_path() / "repos" / a.project
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"git clone {a.git} → {dest}")
            subprocess.run(["git", "clone", "--depth", "1", a.git, str(dest)], check=True)
        root = str(dest)
    if not root:
        print("경로 또는 --git 이 필요합니다")
        return 2
    profile = {}
    if a.context_header is not None:
        profile["context_header"] = _onoff(a.context_header)
    if a.bm25 is not None:
        profile["use_bm25"] = _onoff(a.bm25)
    if a.exclude is not None:
        profile["exclude_globs"] = a.exclude
    if a.chunker:
        profile["chunker"] = a.chunker
    hook = None if a.no_briefing else (lambda pid, r, commit: briefing.build(r, pid, model=a.model, commit=commit))
    r = indexer.start_index(root, a.project, profile=profile, blocking=True, force=a.force, on_done=hook)
    print(json.dumps({k: v for k, v in r.items() if k not in ("fingerprint",)}, ensure_ascii=False, indent=2, default=str))
    return 0 if r.get("accepted") and r.get("state") == "done" else 1


def cmd_status(a) -> int:
    print(json.dumps(indexer.status(a.project), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_search(a) -> int:
    sp = {}
    if a.bm25 is not None:
        sp["use_bm25"] = _onoff(a.bm25)
    r = search_mod.search(a.question, a.project, top_k=a.top_k, threshold=a.threshold, search_profile=sp)
    print(f"has_evidence={r['has_evidence']}  top_score={r['top_score']}  threshold={r['threshold']}  "
          f"bm25_active={r['bm25_active']}  timing={r['timing']}")
    if r["bm25_active"]:
        print("(순서는 RRF 융합 순위이며 점수순이 아닙니다. 판정은 벡터 점수 기준)")
    passed_ids = {h["_id"] for h in r["contexts"]}
    for i, h in enumerate(r["all_hits"], 1):
        mark = "✓" if h["_id"] in passed_ids else "✗"
        loc = f"L{h['line_start']}-{h['line_end']}" if h.get("line_start") else (h.get("section") or "")
        print(f" {mark} {i:2d}. {h['score']:.4f}  {h['path']}  {loc}  {h.get('symbol') or ''}")
    return 0


def cmd_ask(a) -> int:
    body = {"project_id": a.project, "message": a.question, "rag": not a.no_rag, "model_id": a.model,
            "top_k": a.top_k, "threshold": a.threshold}
    if a.json:
        code, payload = chat.collect(body)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if code == 200 else 1
    for ev in chat.run_chat(body):
        if ev["event"] == "meta":
            d = ev["data"]
            print(f"[meta] has_evidence={d.get('has_evidence')} top={d.get('top_score')} model={d.get('model')} "
                  f"stage={d.get('stage', {}).get('label')}")
        elif ev["event"] == "delta":
            sys.stdout.write(ev["data"]["text"])
            sys.stdout.flush()
        elif ev["event"] == "done":
            d = ev["data"]
            print("\n\n[done] no_evidence=", d["no_evidence"], " cited=", d["cited"],
                  " timing=", d["metadata"]["timing"])
            for r in d["reference_files"]:
                print(f"  - {r['path']} {r['citations']} {r['lines']}")
        elif ev["event"] == "error":
            print("\n[error]", ev["data"])
            return 1
    return 0


def cmd_briefing(a) -> int:
    cached = briefing.load(a.project)
    if cached and not a.force:
        print(cached["briefing"])
        print(f"\n(캐시: {cached['generated_at']}, --force 로 재생성)")
        return 0
    st = get_store()
    info = st.project_info(a.project) or {}
    root = a.path or info.get("project_root")
    if not root:
        print("project_root 를 알 수 없습니다. 경로를 함께 주세요: briefing --project x --path <경로>")
        return 2
    rec = briefing.build(root, a.project, model=a.model, commit=info.get("commit"))
    if not rec.get("ok"):
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 1
    print(rec["briefing"])
    print(f"\n(저장: {rec['md_path']}, {rec['elapsed_s']}s, model={rec['model']})")
    return 0


def cmd_bm25(a) -> int:
    print(json.dumps(indexer.rebuild_bm25(a.project), ensure_ascii=False))
    return 0


def cmd_repair(a) -> int:
    items = indexer.repair(apply=a.apply)
    if not items:
        print("미완성 빌드 없음")
    for it in items:
        print(("삭제: " if a.apply else "발견: ") + json.dumps(it, ensure_ascii=False, default=str))
    if items and not a.apply:
        print("지우려면 --apply (서버가 인덱싱 중이 아닐 때만)")
    return 0


def cmd_doctor(a) -> int:
    from . import lexical
    st = get_store()
    bad = 0
    print(f"store={st.kind}  CFG.fingerprint={CFG.fingerprint()}")
    for pid in st.projects():
        info = st.project_info(pid) or {}
        fp = info.get("fingerprint") or {}
        n = info.get("chunks")
        bm = lexical.doc_count(pid)
        flags = []
        if fp.get("use_bm25") and bm != n:
            flags.append(f"🔴 BM25 문서 수 {bm} ≠ 청크 {n} → python -m vss.cli bm25 --project {pid}")
            bad += 1
        if not fp.get("use_bm25") and bm:
            flags.append("🟡 벡터 전용 프로필인데 BM25 파일이 남아 있음")
        drift = {k: (fp.get(k), v) for k, v in CFG.fingerprint().items() if fp.get(k) != v}
        if drift:
            flags.append(f"🟡 다음 전체 인덱싱 기본값과 다름(서빙 오류 아님): {drift}")
        print(f"{pid:32s} {n or 0:>7,}청크  bm25={bm}  " + (" | ".join(flags) if flags else "✅"))
    for it in st.incomplete():
        print(f"🟠 미완성 빌드: {it}")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vss.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health").set_defaults(fn=cmd_health)
    p = sub.add_parser("projects"); p.set_defaults(fn=cmd_projects)
    p.add_argument("--json", action="store_true", help="README 상태 구역용 스냅샷 (노트북에서 data/ec2/projects.json 으로 저장)")
    p = sub.add_parser("index"); p.set_defaults(fn=cmd_index)
    p.add_argument("path", nargs="?"); p.add_argument("--git"); p.add_argument("--project", required=True)
    p.add_argument("--force", action="store_true"); p.add_argument("--no-briefing", action="store_true")
    p.add_argument("--context-header"); p.add_argument("--bm25"); p.add_argument("--exclude")
    p.add_argument("--chunker", choices=["ast-v1", "line-window-v1"]); p.add_argument("--model")
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status); p.add_argument("--project", required=True)
    p = sub.add_parser("search"); p.set_defaults(fn=cmd_search)
    p.add_argument("question"); p.add_argument("--project", required=True)
    p.add_argument("--top-k", type=int); p.add_argument("--threshold", type=float); p.add_argument("--bm25")
    p = sub.add_parser("ask"); p.set_defaults(fn=cmd_ask)
    p.add_argument("question"); p.add_argument("--project"); p.add_argument("--model")
    p.add_argument("--no-rag", action="store_true"); p.add_argument("--json", action="store_true")
    p.add_argument("--top-k", type=int); p.add_argument("--threshold", type=float)
    p = sub.add_parser("briefing"); p.set_defaults(fn=cmd_briefing)
    p.add_argument("--project", required=True); p.add_argument("--path"); p.add_argument("--force", action="store_true")
    p.add_argument("--model")
    p = sub.add_parser("bm25"); p.set_defaults(fn=cmd_bm25); p.add_argument("--project", required=True)
    p = sub.add_parser("repair"); p.set_defaults(fn=cmd_repair); p.add_argument("--apply", action="store_true")
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
