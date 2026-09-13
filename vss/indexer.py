"""
전체 인덱싱 — 수집 → 청킹 → 임베딩 → 저장(빌드) → BM25 → 승격(원자적 교체) → 완료 훅(브리핑).

상태의 정본은 저장소입니다. 이 모듈은 진행률을 메모리(JOBS)에만 두고, 완료 이력을 data/index_log.jsonl 에
append 합니다(이력이지 정본이 아닙니다). 서버가 재시작되면 저장소의 building 잔재가 중단을 말해 줍니다.

⚠ 선삭제 금지: 임베딩이 전부 성공하기 전에는 기존 인덱스를 건드리지 않습니다.
⚠ 실패한 build 는 자동으로 지우지 않습니다. `python -m vss.cli repair` 로 명시적으로 지웁니다.

증분 (2026-09-08): 같은 이름의 완성 인덱스가 있고 fingerprint 가 같고 파일 해시 manifest 가 저장소와 맞으면,
안 바뀐 파일의 청크·벡터는 active 에서 새 빌드로 **복사**하고 바뀐 파일만 청킹·임베딩합니다. 빌드→승격 순서는 같습니다.
청크는 파일 단위로 독립이라(chunk_text 의 입력은 내용·경로·profile 뿐) 파일 내용이 같으면 청크·벡터도 같습니다.
force=True 는 전체 인덱싱입니다.
"""

from __future__ import annotations

import hashlib
import json
import re
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
from .config import CFG, CHUNKER_RANK, _norm_pid, alias_map, normalize_fingerprint, resolve_profile
from .embedder import embed_many
from .search import invalidate_bm25, invalidate_symbols
from .store import VectorStore, get_store

STALE_AFTER = 300.0          # heartbeat 가 이만큼 끊기면 running 을 믿지 않습니다
JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

# 자동 선택의 "더 새것" 순서는 config.CHUNKER_RANK (재정렬 auto 도 같은 표를 본다).


def git_head(root: str | Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def git_log(root: str | Path, limit: int = 20) -> list[dict]:
    """최근 커밋 목록. git 레포가 아니거나 실패하면 빈 목록입니다.

    ⚠ `--depth 1` 로 clone 된 레포(POST /index 의 remote 경로)는 커밋이 1개만 나옵니다.
    """
    sep = "\x1f"
    try:
        # encoding 을 명시한다 — text=True 만 쓰면 로케일(윈도우 cp949)로 디코딩하다
        # 한글 커밋 메시지에서 죽고, stdout 이 None 이 되어 500 으로 떨어진다 (2026-09-02 실측).
        out = subprocess.run(
            ["git", "-C", str(root), "log", f"-{max(1, int(limit))}",
             f"--format=%H{sep}%h{sep}%an{sep}%aI{sep}%s"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    except Exception:
        return []
    if out.returncode != 0 or not out.stdout:
        return []
    rows = []
    for line in out.stdout.splitlines():
        p = line.split(sep)
        if len(p) == 5:
            rows.append({"sha": p[0], "short": p[1], "author": p[2], "date": p[3], "message": p[4]})
    return rows


def git_dirty(root: str | Path) -> bool | None:
    try:
        # 한글 파일명이 섞이면 로케일 디코딩이 죽어 dirty 가 조용히 None 이 된다 — encoding 을 못 박는다
        out = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10)
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


# ── 증분 인덱싱 재료: 파일 해시 manifest ──────────────────────────
#   승격한 인덱스가 어떤 파일(내용 sha256)로 만들어졌는지를 data/manifests/<pid>.json 에 남깁니다.
#   정본은 여전히 저장소입니다 — manifest 는 저장소의 indexed_at·chunks 와 맞을 때만 쓰고, 없거나 안 맞으면 전체 인덱싱입니다.
#   저장소 meta 에 넣지 않는 이유: pgvector 는 질의마다 active revision 의 meta 를 읽는데(_active) 3천 파일이면 300KB 입니다.

def manifest_path(project_id: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", project_id)
    return CFG.data_path() / "manifests" / f"{safe}.json"


def file_hashes(root: Path, files: list[Path]) -> dict[str, str]:
    """수집된 파일의 {상대경로(POSIX): sha256}. 청크의 path 와 같은 표기라 manifest 와 청크가 경로로 이어집니다."""
    out: dict[str, str] = {}
    for f in files:
        h = hashlib.sha256()
        with f.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        out[f.relative_to(root).as_posix()] = h.hexdigest()
    return out


def _load_manifest(project_id: str, info: Mapping) -> dict | None:
    """저장소의 active 인덱스와 맞는 manifest 만 돌려줍니다 (indexed_at·chunks 로 대조). 아니면 None."""
    try:
        m = json.loads(manifest_path(project_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if m.get("indexed_at") != info.get("indexed_at") or m.get("chunks") != info.get("chunks"):
        return None
    return m if isinstance(m.get("files"), dict) else None


def _save_manifest(project_id: str, record: dict) -> None:
    p = manifest_path(project_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def plan_incremental(project_id: str, store: VectorStore, fp: dict, hashes: Mapping[str, str]) -> dict | None:
    """증분이 가능하면 계획(바뀐·지운 경로)을, 아니면 None(전체 인덱싱)을 돌려줍니다.

    조건 셋: 같은 이름의 완성 인덱스가 있다 · 그 fingerprint 가 이번 profile 과 같다(다르면 한 인덱스에 두 청커가
    섞인다 — 불변 조건 6) · 저장소와 맞는 manifest 가 있다. 이름을 바꾼 파일은 지운 경로 + 새 경로로 잡힙니다.
    """
    info = store.project_info(project_id)
    if not info or normalize_fingerprint(info.get("fingerprint")) != fp:
        return None
    m = _load_manifest(project_id, info)
    if m is None:
        return None
    old = m["files"]
    changed = sorted(p for p, h in hashes.items() if old.get(p) != h)
    deleted = sorted(p for p in old if p not in hashes)
    return {"changed": changed, "deleted": deleted, "unchanged": len(hashes) - len(changed)}


def _run(project_root: str, project_id: str, store: VectorStore, fp: dict,
         on_done: Callable | None, extra_meta: dict | None, *, full: bool = False,
         briefing: str = "auto") -> None:
    root = Path(project_root).resolve()
    t0 = time.time()
    build: str | None = None
    try:
        files = collect_files(root, fp)
        # briefing 을 비워 두는 이유: JOBS 는 같은 이름의 지난 작업 값을 그대로 물려받는다 — 지난 run 의 "ready" 가
        # 이번 run 의 상태처럼 보이면 안 된다. 이번 run 이 정하기 전까지는 None 이다.
        _job(project_id, state="running", processed=0, total=len(files), chunk_count=0,
             error=None, project_root=str(root), fingerprint=fp, started_at=t0, mode=None,
             briefing=None, briefing_error=None)
        hashes = file_hashes(root, files)
        plan = None if full else plan_incremental(project_id, store, fp, hashes)
        mode = "incremental" if plan is not None else "full"
        skip: set[str] = set(plan["changed"]) | set(plan["deleted"]) if plan is not None else set()
        work = [f for f in files if f.relative_to(root).as_posix() in skip] if plan is not None else files
        _job(project_id, mode=mode, total=len(work))
        meta = {"project_root": str(root), "commit": git_head(root), "dirty": git_dirty(root),
                "started_at": t0, "mode": mode}
        meta.update(extra_meta or {})
        build = store.begin_build(project_id, fingerprint=fp, meta=meta)
        _job(project_id, build=build)

        total_chunks = 0
        buf: list[dict] = []
        bm25_docs: list[dict] = []          # BM25 는 메모리에 모아 승격 뒤 파일로 교체합니다
        last_report = 0.0
        use_bm25 = bool(fp.get("use_bm25"))
        reused = 0
        if plan is not None:
            # 안 바뀐 파일의 청크·벡터는 active 에서 빌드로 복사합니다 (임베딩 없음). 바뀐·지운 경로만 뺍니다.
            copied = store.copy_chunks(project_id, build, skip_paths=skip)
            reused = total_chunks = len(copied)
            if use_bm25:
                bm25_docs.extend({"_id": c["_id"], "path": c["path"], "section": c.get("section"),
                                  "symbol": c.get("symbol"), "text": c["text"]} for c in copied)
            _job(project_id, chunk_count=total_chunks)

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

        for i, f in enumerate(work, start=1):
            buf.extend(chunk_file(f, root, fp))
            if len(buf) >= 64:
                flush()
            now = time.time()
            if now - last_report >= 2.0 or i == len(work):
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
                     "bm25_count": total_chunks if use_bm25 else None,
                     "mode": mode,
                     "incremental": ({"changed_files": len(plan["changed"]), "deleted_files": len(plan["deleted"]),
                                      "unchanged_files": plan["unchanged"], "reused_chunks": reused,
                                      "rebuilt_chunks": total_chunks - reused} if plan is not None else None)}
        store.promote(project_id, build, meta=done_meta)
        final_bm25 = lexical.index_path(project_id)
        if staged_bm25 is not None:
            final_bm25.parent.mkdir(parents=True, exist_ok=True)
            staged_bm25.replace(final_bm25)
        elif final_bm25.exists():
            final_bm25.unlink()             # 벡터 전용 프로필로 교체했을 때 옛 역색인이 남지 않게
        invalidate_bm25(project_id)
        invalidate_symbols(project_id)      # 청크 수가 같아도 내용이 바뀌었을 수 있다
        build = None
        try:                                # manifest 는 다음 증분의 재료일 뿐이라 실패해도 인덱스는 done 입니다
            _save_manifest(project_id, {"project_id": project_id, "indexed_at": done_meta["indexed_at"],
                                        "chunks": total_chunks, "fingerprint": fp, "files": hashes})
        except OSError as e:
            print(f"!! manifest 저장 실패 (다음 인덱싱은 전체): {e}")

        rec = _job(project_id, state="done", processed=len(work), total=len(work),
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
    # 증분이면 기본(auto)은 이전 브리핑을 그대로 둡니다("kept") — 커밋마다 LLM 을 수십 번 부르지 않기 위해.
    # "always" 는 증분이어도 매번 만듭니다. 이전 브리핑은 캐시에 있고, POST /briefing {force} 로 언제든 다시 만듭니다.
    if on_done and (plan is None or briefing == "always"):
        try:
            _job(project_id, briefing="generating")
            r = on_done(project_id, str(root), done_meta.get("commit"))
            _job(project_id, briefing="ready" if (r or {}).get("ok") else "failed",
                 briefing_error=None if (r or {}).get("ok") else (r or {}).get("reason"))
        except Exception as e:
            _job(project_id, briefing="failed", briefing_error=f"{type(e).__name__}: {e}")
            print(f"!! 브리핑 생성 실패 (인덱스는 정상): {e}")
    elif on_done:
        _job(project_id, briefing="kept", briefing_error=None)


# 아직 끝나지 않은 상태. 같은 이름의 두 번째 인덱싱을 막는 자리(`already_running`)와 삭제가 같은 값을 봅니다.
RUNNING_STATES = ("running", "indexing_lexical", "promoting")

BRIEFING_POLICIES = ("auto", "always", "never")


def start_index(project_root: str, project_id: str, *, profile: Mapping | None = None,
                blocking: bool = False, force: bool = False, on_done: Callable | None = None,
                extra_meta: dict | None = None, store: VectorStore | None = None,
                briefing: str = "auto") -> dict:
    """인덱싱 시작. 기본은 비동기(즉시 반환). 같은 project_id 가 running 이면 거부합니다.

    같은 이름·같은 fingerprint 의 완성 인덱스와 manifest 가 있으면 증분(바뀐 파일만 임베딩), 아니면 전체입니다.
    force=True 는 전체 인덱싱이고, stale 이 아닌 running 도 밀어냅니다.
    briefing: 완료 훅(on_done) 을 언제 부르나 — "auto"(전체 때만, 증분은 이전 브리핑 유지) · "always"(매번) · "never".
    """
    if briefing not in BRIEFING_POLICIES:
        raise ValueError(f"briefing 은 {BRIEFING_POLICIES} 중 하나: {briefing!r}")
    if briefing == "never":
        on_done = None
    cur = JOBS.get(project_id) or {}
    if cur.get("state") in RUNNING_STATES:
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
        _run(str(root), project_id, st, fp, on_done, extra_meta, full=force, briefing=briefing)
        return {"accepted": True, **status(project_id, st)}
    threading.Thread(target=_run, args=(str(root), project_id, st, fp, on_done, extra_meta),
                     kwargs={"full": force, "briefing": briefing}, daemon=True).start()
    return {"accepted": True, "project_id": project_id, "state": "running", "fingerprint": fp}


def _as_dict(v) -> dict | None:
    """chroma 는 dict meta 를 JSON 문자열로 담으므로 읽을 때 되돌립니다."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


def _running_index(name: str) -> str | None:
    """`name` 으로 물었을 때 **지금 도는** 작업의 실제 인덱스 이름. 여럿이면 늦게 시작한 것.

    커밋별 이름(`<repo>@<branch>--<청커>-<sha7>`)으로 인덱싱해도 프론트는 `<repo>` 나 `<repo>@<branch>` 로 묻습니다.
    JOBS 만 훑으므로 저장소를 건드리지 않습니다 — 프론트가 진행률을 0.1초 간격으로 폴링합니다.
    """
    key = _norm_pid(name)
    prefixes = (f"{key}--", f"{key}@")
    hits = [(j.get("started_at") or 0, pid) for pid, j in list(JOBS.items())
            if j.get("state") in RUNNING_STATES and (_norm_pid(pid) == key or _norm_pid(pid).startswith(prefixes))]
    return max(hits)[1] if hits else None


def status(project_id: str, store: VectorStore | None = None) -> dict:
    """진행률(메모리) + 저장소 상태를 합칩니다.

    이름이 그대로 없으면 이 레포의 인덱스를 찾습니다 — **도는 작업 먼저**, 그다음 완성 인덱스(`resolve_index`).
    도는 작업을 저장소보다 먼저 보는 이유: 옛 인덱스가 남아 있으면 방금 시작한 작업이 `done` 으로 보입니다.
    `project_id` 는 물어본 이름 그대로고, 실제로 답한 인덱스는 `index_id` 입니다.
    """
    st = store or get_store()
    name = project_id
    if project_id not in JOBS:
        name = _running_index(project_id) or (
            project_id if st.project_info(project_id) else (resolve_index(project_id, st)[0] or project_id))
    job = dict(JOBS.get(name) or {})
    info = st.project_info(name)
    if job.get("state") in ("running", "indexing_lexical", "promoting"):
        age = time.time() - (job.get("heartbeat") or 0)
        if age > STALE_AFTER:
            job["state"] = "aborted"
            job["error"] = f"stale running (heartbeat {age:.0f}s ago)"
    if not job:
        job = {"state": "done" if info else "none"}
    out = {**job, "project_id": project_id, "index_id": name}
    if info:
        out["index"] = {"chunks": info.get("chunks"), "fingerprint": info.get("fingerprint"),
                        "commit": info.get("commit"), "dirty": info.get("dirty"),
                        "indexed_at": info.get("indexed_at"),
                        "project_root": info.get("project_root"), "bm25_count": info.get("bm25_count"),
                        # mode: 이 active 인덱스가 전체(full)로 만들어졌나 증분(incremental)으로 만들어졌나.
                        # incremental: 증분이면 바뀐/지운/안 바뀐 파일 수와 복사/재생성 청크 수. 전체면 None.
                        "mode": info.get("mode"), "incremental": _as_dict(info.get("incremental"))}
    out["incomplete"] = [i for i in st.incomplete() if i.get("target") == name]
    return out


def index_candidates(name: str, store: VectorStore | None = None) -> list[str]:
    """`<name>--...` · `<name>@...` 꼴로 이 레포에 속한 **완성된** 인덱스 이름들, 새것부터.

    이름이 곧 인덱스인 경우(변형 없이 한 번만 인덱싱한 레포)도 자기 자신을 후보로 넣습니다.
    `st.projects()` 는 승격이 끝난 것만 돌려주므로 빌드 중이거나 실패한 잔재는 애초에 후보가 아닙니다
    (불변 조건 2·3 — 저장소가 상태의 정본).
    """
    st = store or get_store()
    key = _norm_pid(name)
    # 구분자까지 붙여 비교하므로 `api-test` 가 `api-test-2` 를 잡지 않습니다.
    # `@` 를 넣는 이유: 브랜치를 안 고른 요청(`vision`)도 브랜치가 이름에 든 인덱스(`vision@main--…`)에 닿아야 합니다.
    prefixes = (f"{key}--", f"{key}@")
    cands = [p for p in st.projects() if _norm_pid(p).startswith(prefixes) or _norm_pid(p) == key]

    def key(pid: str):
        info = st.project_info(pid) or {}
        rank = CHUNKER_RANK.get((info.get("fingerprint") or {}).get("chunker"), 0)
        return (rank, str(info.get("indexed_at") or ""), pid)

    return sorted(cands, key=key, reverse=True)


def resolve_index(project_id: str | None, store: VectorStore | None = None) -> tuple[str | None, str]:
    """요청이 보낸 이름 → 실제로 검색할 인덱스 이름과 **왜 그것이 뽑혔는지**.

    alias  `.env` 의 VSS_PROJECT_ALIASES 가 손으로 고정한 것. 언제나 이깁니다.
    exact  그 이름의 인덱스가 저장소에 실제로 있음 (`cli--ast-v2` 를 직접 보낸 경우).
    auto   `<repo>--*` 중 청커 세대가 가장 새것. 같으면 indexed_at 최신.
    none   후보가 없음. 받은 이름을 그대로 돌려주고 search 가 ProjectNotFound 를 냅니다 —
           비슷한 이름으로 몰래 바꿔 주는 폴백은 두지 않습니다.
    """
    if not project_id:
        return project_id, "none"
    key = _norm_pid(project_id)
    for k, v in alias_map().items():
        if k == key:
            return v, "alias"
    st = store or get_store()
    if project_id in st.projects():
        return project_id, "exact"
    cands = index_candidates(project_id, st)
    if cands:
        return cands[0], "auto"
    return project_id, "none"


def repo_map(store: VectorStore | None = None) -> dict[str, dict]:
    """짧은 레포 이름 → 지금 그 이름이 닿는 인덱스. 프론트가 `GET /projects` 에서 고를 목록입니다."""
    st = store or get_store()
    # `<repo>--<변형>` 이면 앞부분이 레포 이름이고, `--` 가 없으면 인덱스 이름이 곧 레포 이름이다
    # (변형 없이 한 번만 인덱싱한 레포. 이걸 빼면 프론트 목록에서 통째로 사라진다 — 2026-09-02).
    repos = {p.split("--", 1)[0] for p in st.projects()}
    repos |= set(alias_map())
    out = {}
    for name in sorted(repos):
        index_id, why = resolve_index(name, st)
        out[name] = {"index_id": index_id, "resolved_by": why,
                     "candidates": index_candidates(name, st)}
    return out


def repo_list(store: VectorStore | None = None, *, commits: int = 0) -> list[dict]:
    """프론트가 쓰기 좋은 축약본 — 레포 하나 = 항목 하나. 배열이라 그대로 순회하면 됩니다.

    인덱싱된 레포와 (VSS_REPOS_DIR 이 있으면) 아직 인덱싱 안 된 레포를 한 목록에 담습니다.
    commits>0 이면 그 레포의 최근 커밋을 함께 냅니다 (git 이 없으면 빈 목록).
    인덱스 선택 과정과 저장 메타데이터는 내부에 유지하되, 목록에 필요 없는
    resolved_by/candidates/indexed_at/dirty 는 응답에 싣지 않습니다.
    """
    st = store or get_store()
    out: list[dict] = []
    for name, m in repo_map(st).items():
        info = st.project_info(m["index_id"]) or {}
        root = info.get("project_root")
        head = git_head(root) if root and Path(root).is_dir() else None
        indexed = info.get("commit")
        out.append({
            "name": name,
            "indexed": True,
            "index_id": m["index_id"],
            "indexed_commit": indexed,          # 이 인덱스가 만들어진 시점의 커밋
            "head_commit": head,                # 지금 디스크의 HEAD
            "stale": (head != indexed) if (head and indexed) else None,
            "chunks": info.get("chunks"),
            "chunker": (info.get("fingerprint") or {}).get("chunker"),
            "path": root,
            "commits": git_log(root, commits) if (commits and root) else [],
        })
    for r in (unindexed_repos(st) or []):
        out.append({
            "name": r["name"], "indexed": False, "index_id": None,
            "indexed_commit": None, "head_commit": r["commit"],
            "stale": None, "chunks": None, "chunker": None, "path": r["path"],
            "commits": git_log(r["path"], commits) if commits else [],
        })
    return sorted(out, key=lambda r: r["name"])


def _staleness(info: Mapping) -> dict:
    """인덱스의 commit 과 디스크의 현재 HEAD 를 비교. 둘 중 하나라도 모르면 stale=None 이다."""
    root = info.get("project_root")
    head = git_head(root) if root and Path(root).is_dir() else None
    indexed = info.get("commit")
    return {"head_commit": head,
            "stale": (head != indexed) if (head and indexed) else None}


def index_files(project_id: str, store: VectorStore | None = None, *, symbols: bool = False) -> list[dict]:
    """인덱스에 **실제로 들어간** 파일 목록. 제외 규칙이 먹었는지도 여기서 드러납니다.

    저장된 청크를 훑어 path 로 묶습니다 (인덱싱 당시의 디스크가 아니라 인덱스 자신이 기준 — 불변 조건 3).
    symbols=True 면 파일별 심볼 이름을 함께 싣습니다. 응답이 커지니 요청이 있을 때만 씁니다.
    """
    st = store or get_store()
    agg: dict[str, dict] = {}
    for c in st.iter_chunks(project_id):
        f = agg.setdefault(c["path"], {"path": c["path"], "type": c.get("type") or "code",
                                       "chunks": 0, "line_max": 0})
        f["chunks"] += 1
        f["line_max"] = max(f["line_max"], int(c.get("line_end") or 0))
        if symbols:
            for s in str(c.get("symbol") or "").split(","):
                s = s.strip()
                if s and not s.startswith("(") and s not in f.setdefault("symbols", []):
                    f["symbols"].append(s)
    return sorted(agg.values(), key=lambda f: f["path"])


def unindexed_repos(store: VectorStore | None = None) -> list[dict] | None:
    """VSS_REPOS_DIR 아래에 있지만 인덱스가 하나도 없는 레포. 설정이 없으면 None (키를 안 내보냅니다).

    P 의 스냅샷이 붙으면 이 경로가 바뀝니다 — 그때는 값만 갈아 끼우면 됩니다.
    """
    if not CFG.repos_dir:
        return None
    base = Path(CFG.repos_dir).expanduser()
    if not base.is_dir():
        return None
    st = store or get_store()
    indexed = {_norm_pid(p.split("--", 1)[0]) for p in st.projects()}
    out = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if _norm_pid(d.name) in indexed:
            continue
        out.append({"name": d.name, "path": str(d), "git": (d / ".git").is_dir(),
                    "commit": git_head(d), "dirty": git_dirty(d)})
    return out


def exists(project_id: str, store: VectorStore | None = None) -> dict:
    st = store or get_store()
    info = st.project_info(project_id)
    name = project_id
    if not info:                        # 커밋별 이름으로 들어간 인덱스도 찾습니다 (status 와 같은 규칙, 도는 작업은 안 봅니다)
        name = resolve_index(project_id, st)[0] or project_id
        info = st.project_info(name)
    return {"project_id": project_id, "index_id": name, "exists": bool(info),
            "chunks": (info or {}).get("chunks", 0), "commit": (info or {}).get("commit")}


def list_projects(store: VectorStore | None = None, *, only_current: bool = False) -> list[dict]:
    """인덱스 하나 = 항목 하나.

    `current` 는 "그 레포 이름으로 물으면 지금 이 인덱스가 답한다" 는 뜻입니다 — 같은 레포의 옛 세대
    (`--ast` 옆의 `--ast-v2`)는 False 가 됩니다. only_current=True 면 그것만 남깁니다.
    """
    st = store or get_store()
    current = {m["index_id"] for m in repo_map(st).values()}
    out = []
    for pid in st.projects():
        info = st.project_info(pid) or {}
        fp = info.get("fingerprint") or {}
        out.append({"project_id": pid, "state": "done", "chunks": info.get("chunks"),
                    # dirty: 인덱싱 시점에 코퍼스가 미커밋이었는가. None 이면 git 레포가 아니거나 확인 실패.
                    # 이 값이 True 인 인덱스는 commit 해시가 실제 내용을 가리키지 않는다 (측정 비교 불가).
                    "commit": info.get("commit"), "dirty": info.get("dirty"),
                    "indexed_at": info.get("indexed_at"),
                    "project_root": info.get("project_root"),
                    "use_bm25": bool(fp.get("use_bm25")), "bm25_docs": lexical.doc_count(pid),
                    "context_header": bool(fp.get("context_header")), "chunker": fp.get("chunker"),
                    "note": info.get("note"),
                    # head_commit: 지금 디스크의 HEAD. 인덱스의 commit 과 다르면 코퍼스가 앞서 간 것이다.
                    # 둘 중 하나라도 모르면 stale 은 None — "낡았다" 고 단정하지 않는다.
                    **_staleness(info),
                    # current: 이 레포 이름으로 물으면 지금 이 인덱스가 답한다 (옛 세대는 False)
                    "current": pid in current,
                    "briefing": (JOBS.get(pid) or {}).get("briefing")})
    if only_current:
        out = [p for p in out if p["current"]]
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


# ── 인덱스 삭제 ──────────────────────────────────────────────
#   저장소의 벡터와 **그 인덱스가 만든 사이드카 파일**을 함께 지웁니다 (md 결정 2026-09-10).
#   지우지 않는 것 셋 — 소스 폴더(`~/repos/<레포>` 는 레포 하나를 여러 인덱스가 같이 씁니다),
#   `data/index/`(chroma 는 전 프로젝트가 한 sqlite 를 씁니다), `data/index_log.jsonl`(전역 append 이력).
#   질의 로그(`rag.query_log`)는 저장 계층 밖의 모듈이라 부르는 쪽이 따로 지웁니다.

def _within(path: Path, root: Path) -> Path | None:
    """`root` 아래이면서 **우리가 만든 그 경로 그대로**인 것만 돌려줍니다.

    담기(containment)만 보면 부족합니다. 윈도우는 끝의 점을 떼고 대소문자를 무시하므로
    `runs/...` 가 `runs` 로, `runs/ALPHA` 가 `runs/alpha` 로 접히는데 둘 다 root 안이라 통과해 버립니다.
    접히면 마지막 조각의 이름이 달라지므로 그것을 봅니다 — 남의 폴더를 rmtree 하는 유일한 길이 이것입니다.
    """
    try:
        p = path.resolve()
        p.relative_to(root)
    except (ValueError, OSError):
        return None
    return p if p.name == path.name else None


def delete_index(project_id: str, store: VectorStore | None = None) -> dict:
    """인덱스 하나를 지웁니다 — 저장소의 벡터 + 그 인덱스의 사이드카 파일.

    이름은 **정확히 일치해야 합니다**. `resolve_index` 를 태우지 않습니다 — alias 가 언제나 이기고
    auto 가 형제 인덱스를 골라서 엉뚱한 것을 지웁니다. 없는 이름은 오류가 아니라 `existed: False` 입니다(멱등).

    지우는 순서가 곧 안전 순서입니다. 고아가 됐을 때 **거짓말하는 파일은 브리핑 하나뿐**이라
    (`GET /briefing` 은 저장소를 안 보고 파일만 읽습니다) 그것부터 지웁니다. BM25·manifest 는 남아도
    저장소의 건수·`indexed_at` 과 비교돼 걸러집니다. 중간에 죽어도 거짓말이 안 남는 순서입니다.
    """
    from .briefing_pipeline import lock_is_busy, safe_id
    from .store.chroma import is_internal

    st = store or get_store()
    root = CFG.data_path()
    safe = safe_id(project_id)
    if not safe or not safe.strip("."):          # ".", "..", "..." — safe_id 가 점을 남기고, 점뿐인 조각은 경로가 된다
        raise ValueError(f"지울 수 없는 project_id 입니다: {project_id!r}")
    # chroma 의 내부 이름을 그대로 받으면 남의 인덱스를 지웁니다 — `building-X` 를 지우라는 요청이
    # drop 안에서 X 의 **돌고 있는 빌드**를 지우고, 같은 이름의 BM25 파일이 X 의 staging 이기도 합니다.
    # 이 이름들은 `GET /health`·`/projects` 의 incomplete 목록에 그대로 나오므로 손으로 복사해 넣기 쉽습니다.
    if is_internal(project_id):
        raise ValueError(f"저장소 내부 이름은 지울 수 없습니다 (미완성 빌드는 repair 로): {project_id!r}")

    out: dict = {"project_id": project_id, "existed": bool(st.project_info(project_id)),
                 "removed": [], "briefing_busy": False, "errors": []}

    def rm(p: Path) -> None:
        target = _within(p, root)
        if target is None:
            out["errors"].append(f"data/ 밖이라 건너뜁니다: {p}")
            return
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            else:
                return
            out["removed"].append(target.relative_to(root).as_posix())
        except OSError as e:
            out["errors"].append(f"{target.name}: {e}")

    # ① 브리핑 — 생성 중이면 손대지 않습니다. 지워 봐야 그 run 이 끝나면서 같은 파일을 다시 발행합니다.
    b = CFG.briefings_dir()
    if lock_is_busy(project_id):
        out["briefing_busy"] = True
    else:
        # lock 파일은 지우지 않습니다 — `lock_is_busy` 가 False 를 냈다는 건 lock 이 없거나 죽은 것을 이미
        # 치웠다는 뜻입니다. 여기서 또 지우면 검사와 삭제 사이에 시작한 run 의 lock 을 뺏습니다.
        for p in (b / f"{safe}.json", b / f"{safe}.md", b / "runs" / safe,
                  b / "runs" / f"{safe}.status.json", b / "stage_cache" / safe):
            rm(p)

    # ② 저장소 — `GET /index/exists` 가 보는 곳입니다. 여기가 지워져야 삭제된 것입니다.
    #    `existed` 와 무관하게 부릅니다. `project_info` 는 promote 를 마친 인덱스만 보므로, 중단된 빌드
    #    (`building-<pid>`·retired revision)는 existed=False 인데도 저장소에 남아 있습니다. drop 은 그것까지 지웁니다.
    try:
        st.drop(project_id)
    except Exception as e:
        # 같은 이름으로 DELETE 두 개가 동시에 오면 chroma 의 `if name in self._names()` 뒤에서 진 쪽이 터집니다.
        # 없어진 것이 확인되면 성공입니다 — 여기서 500 을 내면 module 이 "VSS 서버가 죽었다" 로 읽습니다.
        if st.project_info(project_id):
            raise
        out["errors"].append(f"drop: {type(e).__name__}: {e} (이미 지워져 있었습니다)")
    if out["existed"]:
        out["removed"].append(f"store:{st.kind}")

    # ③ 파생 파일 — 남아도 무해하지만 크기가 청크 수에 비례합니다.
    for p in (lexical.index_path(project_id), lexical.staging_path(project_id), manifest_path(project_id)):
        rm(p)
        rm(p.with_name(p.name + ".tmp"))         # 원자 교체 중 죽으면 남는 것

    # ④ 메모리 — JOBS 를 안 비우면 status 가 지운 이름을 done·chunk_count 째로 계속 돌려줍니다(저장소는 비었는데).
    #    **도는 중이면 남깁니다.** JOBS 항목이 같은 이름의 동시 인덱싱을 막는 유일한 자리라(`already_running`),
    #    지우면 지금 도는 스레드 위로 두 번째 run 이 accepted 되어 한 이름에 writer 가 둘이 됩니다.
    invalidate_bm25(project_id)
    invalidate_symbols(project_id)
    with _JOBS_LOCK:
        if (JOBS.get(project_id) or {}).get("state") not in RUNNING_STATES:
            JOBS.pop(project_id, None)
    return out


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
