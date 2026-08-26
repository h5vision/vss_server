"""
전체 인덱싱 — 수집 → 청킹 → 임베딩 → 저장(빌드) → BM25 → 승격(원자적 교체) → 완료 훅(브리핑).

상태의 정본은 저장소입니다. 이 모듈은 진행률을 메모리(JOBS)에만 두고, 완료 이력을 data/index_log.jsonl 에
append 합니다(이력이지 정본이 아닙니다). 서버가 재시작되면 저장소의 building 잔재가 중단을 말해 줍니다.

⚠ 선삭제 금지: 임베딩이 전부 성공하기 전에는 기존 인덱스를 건드리지 않습니다.
⚠ 실패한 build 는 자동으로 지우지 않습니다. `python -m vss.cli repair` 로 명시적으로 지웁니다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from . import lexical
from .chunker import chunk_file, collect_files
from .config import CFG, normalize_fingerprint, resolve_profile
from .embedder import embed_many
from .search import invalidate_bm25
from .store import VectorStore, get_store

STALE_AFTER = 300.0          # heartbeat 가 이만큼 끊기면 running 을 믿지 않습니다
JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def git_head(root: str | Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def git_dirty(root: str | Path) -> bool | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                             capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip()) if out.returncode == 0 else None
    except Exception:
        return None


def _job(project_id: str, **fields) -> dict:
    with _JOBS_LOCK:
        j = JOBS.setdefault(project_id, {"project_id": project_id})
        j.update(fields)
        j["heartbeat"] = time.time()
        return dict(j)


def _log(record: dict) -> None:
    try:
        p = CFG.data_path() / "index_log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:                      # 이력 기록 실패가 인덱싱을 죽이면 안 됩니다
        print(f"!! index_log 기록 실패: {e}")


def _run(project_root: str, project_id: str, store: VectorStore, fp: dict,
         on_done: Callable | None, extra_meta: dict | None) -> None:
    root = Path(project_root).resolve()
    t0 = time.time()
    build: str | None = None
    try:
        files = collect_files(root, fp)
        _job(project_id, state="running", processed=0, total=len(files), chunk_count=0,
             error=None, project_root=str(root), fingerprint=fp, started_at=t0)
        meta = {"project_root": str(root), "commit": git_head(root), "dirty": git_dirty(root),
                "started_at": t0}
        meta.update(extra_meta or {})
        build = store.begin_build(project_id, fingerprint=fp, meta=meta)
        _job(project_id, build=build)

        total_chunks = 0
        buf: list[dict] = []
        bm25_docs: list[dict] = []          # BM25 는 메모리에 모아 승격 뒤 파일로 교체합니다
        last_report = 0.0
        use_bm25 = bool(fp.get("use_bm25"))

        def flush():
            nonlocal buf, total_chunks
            if not buf:
                return
            vecs = embed_many([c["text"] for c in buf], model=str(fp["embed_model"]),
                              expected_dim=int(fp["embed_dim"]))
            store.add(build, buf, vecs, project_id=project_id)
            if use_bm25:
                from .store.base import chunk_id
                for i, c in enumerate(buf):
                    bm25_docs.append({"_id": chunk_id(project_id, c, i), "path": c["path"],
                                      "section": c.get("section"), "symbol": c.get("symbol"),
                                      "text": c["text"]})
            total_chunks += len(buf)
            buf = []
            # 임베딩(배치 여러 번)이 길어져도 heartbeat 가 갱신되게 — stale 오판 → 동시 빌드 레이스 방지
            _job(project_id, chunk_count=total_chunks)

        for i, f in enumerate(files, start=1):
            buf.extend(chunk_file(f, root, fp))
            if len(buf) >= 64:
                flush()
            now = time.time()
            if now - last_report >= 2.0 or i == len(files):
                _job(project_id, processed=i, chunk_count=total_chunks)
                last_report = now
        flush()

        staged_bm25 = None
        if use_bm25:
            _job(project_id, state="indexing_lexical", chunk_count=total_chunks)
            staged_bm25 = lexical.staging_path(project_id)
            idx = lexical.build(project_id, bm25_docs, path=staged_bm25, expected_count=total_chunks)
            if len(idx.doc_ids) != total_chunks:
                raise RuntimeError(f"BM25 문서 수 불일치: bm25={len(idx.doc_ids)}, chunks={total_chunks}")

        _job(project_id, state="promoting", chunk_count=total_chunks)
        done_meta = {"commit": git_head(root), "dirty": git_dirty(root), "files": len(files),
                     "indexed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "elapsed_s": round(time.time() - t0, 1),
                     "bm25_count": total_chunks if use_bm25 else None}
        store.promote(project_id, build, meta=done_meta)
        final_bm25 = lexical.index_path(project_id)
        if staged_bm25 is not None:
            final_bm25.parent.mkdir(parents=True, exist_ok=True)
            staged_bm25.replace(final_bm25)
        elif final_bm25.exists():
            final_bm25.unlink()             # 벡터 전용 프로필로 교체했을 때 옛 역색인이 남지 않게
        invalidate_bm25(project_id)
        build = None

        rec = _job(project_id, state="done", processed=len(files), total=len(files),
                   chunk_count=total_chunks, error=None, **done_meta)
        _log({"event": "index_done", **rec})
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        rec = _job(project_id, state="failed", error=err, elapsed_s=round(time.time() - t0, 1))
        _log({"event": "index_failed", **rec})
        if build and hasattr(store, "mark_failed"):
            try:
                store.mark_failed(build, err)
            except Exception:
                pass
        print(f"!! 인덱싱 실패: {err}")
        if build:
            print(f"   기존 인덱스는 그대로입니다. 임시 빌드 '{build}' 가 남았습니다 (python -m vss.cli repair).")
        raise

    # 완료 훅 — 브리핑. 실패해도 인덱싱은 done 인 채로 둡니다.
    if on_done:
        try:
            _job(project_id, briefing="generating")
            r = on_done(project_id, str(root), done_meta.get("commit"))
            _job(project_id, briefing="ready" if (r or {}).get("ok") else "failed",
                 briefing_error=None if (r or {}).get("ok") else (r or {}).get("reason"))
        except Exception as e:
            _job(project_id, briefing="failed", briefing_error=f"{type(e).__name__}: {e}")
            print(f"!! 브리핑 생성 실패 (인덱스는 정상): {e}")


def start_index(project_root: str, project_id: str, *, profile: Mapping | None = None,
                blocking: bool = False, force: bool = False, on_done: Callable | None = None,
                extra_meta: dict | None = None, store: VectorStore | None = None) -> dict:
    """전체 인덱싱 시작. 기본은 비동기(즉시 반환). 같은 project_id 가 running 이면 거부합니다."""
    cur = JOBS.get(project_id) or {}
    if cur.get("state") in ("running", "indexing_lexical", "promoting"):
        age = time.time() - (cur.get("heartbeat") or 0)
        if age < STALE_AFTER and not force:
            return {"accepted": False, "reason": "already_running", "project_id": project_id,
                    "heartbeat_age_s": round(age, 1)}
    root = Path(project_root).resolve()
    if not root.is_dir():
        return {"accepted": False, "reason": "not_a_directory", "path": str(root)}
    fp = resolve_profile(profile)
    st = store or get_store()
    if blocking:
        _run(str(root), project_id, st, fp, on_done, extra_meta)
        return {"accepted": True, **status(project_id, st)}
    threading.Thread(target=_run, args=(str(root), project_id, st, fp, on_done, extra_meta),
                     daemon=True).start()
    return {"accepted": True, "project_id": project_id, "state": "running", "fingerprint": fp}


def status(project_id: str, store: VectorStore | None = None) -> dict:
    """진행률(메모리) + 저장소 상태를 합칩니다."""
    st = store or get_store()
    job = dict(JOBS.get(project_id) or {})
    info = st.project_info(project_id)
    if job.get("state") in ("running", "indexing_lexical", "promoting"):
        age = time.time() - (job.get("heartbeat") or 0)
        if age > STALE_AFTER:
            job["state"] = "aborted"
            job["error"] = f"stale running (heartbeat {age:.0f}s ago)"
    if not job:
        job = {"project_id": project_id, "state": "done" if info else "none"}
    out = {"project_id": project_id, **job}
    if info:
        out["index"] = {"chunks": info.get("chunks"), "fingerprint": info.get("fingerprint"),
                        "commit": info.get("commit"), "indexed_at": info.get("indexed_at"),
                        "project_root": info.get("project_root"), "bm25_count": info.get("bm25_count")}
    out["incomplete"] = [i for i in st.incomplete() if i.get("target") == project_id]
    return out


def exists(project_id: str, store: VectorStore | None = None) -> dict:
    st = store or get_store()
    info = st.project_info(project_id)
    return {"project_id": project_id, "exists": bool(info),
            "chunks": (info or {}).get("chunks", 0), "commit": (info or {}).get("commit")}


def list_projects(store: VectorStore | None = None) -> list[dict]:
    st = store or get_store()
    out = []
    for pid in st.projects():
        info = st.project_info(pid) or {}
        fp = info.get("fingerprint") or {}
        out.append({"project_id": pid, "state": "done", "chunks": info.get("chunks"),
                    "commit": info.get("commit"), "indexed_at": info.get("indexed_at"),
                    "project_root": info.get("project_root"),
                    "use_bm25": bool(fp.get("use_bm25")), "bm25_docs": lexical.doc_count(pid),
                    "context_header": bool(fp.get("context_header")), "chunker": fp.get("chunker"),
                    "briefing": (JOBS.get(pid) or {}).get("briefing")})
    return out


def rebuild_bm25(project_id: str, store: VectorStore | None = None) -> dict:
    """완성 인덱스에서 BM25 역색인만 다시 만듭니다 (페이지 순회 + 건수 검증 + 원자 교체)."""
    st = store or get_store()
    n = st.count(project_id)
    staged = lexical.staging_path(project_id)
    idx = lexical.build(project_id, st.iter_chunks(project_id), path=staged, expected_count=n)
    final = lexical.index_path(project_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    staged.replace(final)
    invalidate_bm25(project_id)
    return {"project_id": project_id, "bm25_docs": len(idx.doc_ids), "chunks": n}


def repair(store: VectorStore | None = None, *, apply: bool = False) -> list[dict]:
    """미완성 build 목록을 보여주고, apply 면 지웁니다. 서버가 인덱싱 중이면 실행하지 마세요."""
    st = store or get_store()
    items = st.incomplete()
    if apply:
        for it in items:
            st.abandon_build(it.get("target") or it["name"],
                             it["name"].split("#")[-1] if "#" in it["name"] else it["name"])
    staging = list(CFG.bm25_dir().glob("building-*.json")) if CFG.bm25_dir().exists() else []
    for s in staging:
        if apply:
            s.unlink()
        items.append({"name": s.name, "kind": "bm25_staging"})
    return items
