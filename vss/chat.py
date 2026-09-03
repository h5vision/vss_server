"""
질의 오케스트레이션 — 검색 → 프롬프트 → LLM(스트리밍) → 출처 확정. 서버 안에서 한 번에 처리합니다.

run_chat() 은 이벤트 generator 입니다. 서버는 stream=true 면 SSE 로 흘려보내고, 아니면 모아서 JSON 하나로 답합니다.
  meta            {request_id, project_id, model, rag, has_evidence, top_score, threshold, reason, stage,
                   sources(light), references(미리보기), reference_files, search_profile, serving_profile, timing}
  delta           {text}
  done            {answer, references, reference_files, cited, no_evidence, source(P 호환), metadata}
  error           {code, message}

대화 히스토리는 받아도 프롬프트에 넣지 않습니다 (0턴 결정 — 근거가 컨텍스트 밖으로 밀리는 것을 막기 위해).
선택 코드(context)는 프롬프트에 넣고, 검색 임베딩에는 질문 + 코드 앞부분을 씁니다.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

from . import llm, prompt as prompt_mod, querylog, search as search_mod
from .config import CFG
from .indexer import resolve_index
from .references import build_references
from .store import ProjectNotFound, get_store


def selected_code(context) -> str | None:
    if not context:
        return None
    if isinstance(context, str):
        return context if context.strip() else None
    if isinstance(context, list):
        parts = []
        for it in context:
            if isinstance(it, str):
                parts.append(it)
            elif isinstance(it, dict):
                t = it.get("text") or it.get("content") or it.get("code") or ""
                p = it.get("path") or it.get("file")
                parts.append(f"# {p}\n{t}" if p else t)
        s = "\n\n".join(x for x in parts if x and x.strip())
        return s or None
    if isinstance(context, dict):
        return selected_code([context])
    return None


def _light(contexts: list[dict]) -> list[dict]:
    return [{k: v for k, v in c.items() if k != "text"} for c in contexts]


def run_chat(body: dict) -> Iterator[dict]:
    t_start = time.perf_counter()
    req_id = str(body.get("client_request_id") or uuid.uuid4().hex[:12])
    question = (body.get("message") or body.get("query") or "").strip()
    project_id = body.get("project_id")
    use_rag = body.get("rag", True) is not False
    model = llm.resolve_model(body.get("model_id") or body.get("model"))
    code = selected_code(body.get("context"))

    contexts: list[dict] = []
    r: dict = {}
    timing: dict = {}
    # 클라이언트는 레포명(`api_test`)만 보내고, 어느 인덱스가 답하는지는 서버가 정합니다.
    # 응답에는 받은 이름(project_id)·실제로 검색한 인덱스(index_id)·그 근거(resolved_by)를 싣습니다.
    index_id, resolved_by = (None, "none")

    def _log(outcome: str, *, metadata: dict | None = None, error_code: str | None = None) -> None:
        """질의 로그 한 행 (vss/querylog.py). DSN 이 비면 no-op 이고 실패해도 답변을 막지 않습니다.

        `rag:false` 는 남기지 않습니다 (md 결정 2026-09-02) — 여기서 거릅니다.
        metadata 를 받으면 **응답에 실려 나간 그 dict** 를 그대로 씁니다. 다시 계산하지 않습니다.
        """
        if not use_rag:
            return
        base = metadata if metadata is not None else {
            "request_id": req_id, "project_id": project_id, "index_id": index_id,
            "resolved_by": resolved_by, "model": model,
            "has_evidence": r.get("has_evidence"), "top_score": r.get("top_score"),
            "threshold": r.get("threshold"), "reason": r.get("reason"),
            "timing": {**timing, "total_ms": round((time.perf_counter() - t_start) * 1000, 1)},
        }
        querylog.write(querylog.from_metadata(base, question=question, outcome=outcome,
                                              error_code=error_code))

    if not question:
        _log("error", error_code="bad_request")
        yield {"event": "error", "data": {"code": "bad_request", "message": "message 가 비어 있습니다"}}
        return

    if use_rag:
        if not project_id or project_id in ("__auto__", "auto", "default"):
            _log("error", error_code="bad_request")
            yield {"event": "error", "data": {"code": "bad_request",
                                              "message": "project_id 가 필요합니다 (GET /projects 로 확인)"}}
            return
        index_id, resolved_by = resolve_index(project_id, get_store())
        try:
            embed_text = question if not code else f"{question}\n{code[:400]}"
            r = search_mod.search(question, index_id, top_k=body.get("top_k"),
                                  threshold=body.get("threshold"), store=get_store(),
                                  search_profile={k: body[k] for k in ("use_bm25", "pool") if k in body},
                                  embed_text=embed_text)
        except ProjectNotFound as e:
            _log("error", error_code="project_not_found")
            yield {"event": "error", "data": {"code": "project_not_found", "message": str(e)}}
            return
        except Exception as e:
            _log("error", error_code="retrieval_failed")
            yield {"event": "error", "data": {"code": "retrieval_failed", "message": f"{type(e).__name__}: {e}"}}
            return
        contexts = r["contexts"]
        pre = build_references(contexts, answer=None, cited_only=False, include_text=False)
        n_files = len(pre["reference_files"])
        stage = {"retrieved": len(contexts), "files": n_files, "top_score": r["top_score"],
                 "threshold": r["threshold"],
                 "label": (f"근거 {len(contexts)}건 확인" + (f" ({n_files}개 파일)" if n_files > 1 else ""))
                 if r["has_evidence"] else "관련 근거를 찾지 못했습니다"}
        timing = dict(r.get("timing") or {})
        # pre_llm_ms = 요청 시작 ~ LLM 호출 직전 누적. embed_ms·search_ms 를 **포함**하므로 더하지 마십시오.
        # prompt_ms 는 프롬프트 조립만 재며, 조립은 112줄이라 이 시점(meta)에는 아직 없습니다.
        timing["pre_llm_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
        yield {"event": "meta", "data": {
            "request_id": req_id, "project_id": project_id, "index_id": index_id,
            "resolved_by": resolved_by, "model": model, "rag": True,
            "has_evidence": r["has_evidence"], "top_score": r["top_score"], "threshold": r["threshold"],
            "reason": r["reason"], "stage": stage, "sources": _light(contexts),
            "references": pre["references"], "reference_files": pre["reference_files"],
            "search_profile": r["search_profile"], "serving_profile": r["serving_profile"],
            "bm25_active": r.get("bm25_active", False), "timing": timing}}
        if not r["has_evidence"]:
            # 근거가 없으면 LLM 을 부르지 않습니다 (FN-B06). 답할 재료가 없습니다.
            no_ev_meta = {"request_id": req_id, "status": "completed", "rag_provider": "vss",
                          "project_id": project_id, "index_id": index_id, "resolved_by": resolved_by,
                          "model": None, "has_evidence": False,
                          "reason": r["reason"], "top_score": r["top_score"], "threshold": r["threshold"],
                          "history_used": 0,
                          "timing": {**timing, "total_ms": round((time.perf_counter() - t_start) * 1000, 1)}}
            _log("no_evidence", metadata=no_ev_meta)
            yield {"event": "done", "data": {
                "answer": "NO_EVIDENCE", "references": [], "reference_files": [], "cited": [],
                "no_evidence": True, "source": [], "sources": [],
                "metadata": no_ev_meta}}
            return
        t_prompt = time.perf_counter()
        messages = prompt_mod.render_prompt(question, contexts, selected_code=code)
        timing["prompt_ms"] = round((time.perf_counter() - t_prompt) * 1000, 1)
    else:
        t_prompt = time.perf_counter()
        messages = prompt_mod.render_plain_prompt(question, selected_code=code)
        # rag=false 경로는 meta 를 조립 뒤에 내보내므로 두 값이 다 있습니다.
        timing = {"prompt_ms": round((time.perf_counter() - t_prompt) * 1000, 1),
                  "pre_llm_ms": round((time.perf_counter() - t_start) * 1000, 1)}
        yield {"event": "meta", "data": {"request_id": req_id, "project_id": project_id, "model": model,
                                         "rag": False, "has_evidence": None,
                                         "stage": {"label": "검색 없이 답변 생성 중"}, "sources": [],
                                         "references": [], "reference_files": [],
                                         "timing": timing}}

    yield {"event": "stage", "data": {"label": "답변 생성 중..."}}
    answer_parts: list[str] = []
    t_llm = time.perf_counter()
    ttft = None
    stats: dict = {}
    try:
        for ev in llm.chat_stream(messages, model=model):
            if "delta" in ev:
                if ttft is None:
                    ttft = round((time.perf_counter() - t_llm) * 1000, 1)
                answer_parts.append(ev["delta"])
                yield {"event": "delta", "data": {"text": ev["delta"]}}
            elif ev.get("done"):
                stats = ev.get("stats") or {}
    except llm.LLMError as e:
        _log("error", error_code="llm_failed")
        yield {"event": "error", "data": {"code": "llm_failed", "message": str(e),
                                          "partial": "".join(answer_parts)}}
        return
    answer = "".join(answer_parts)
    gen_ms = round((time.perf_counter() - t_llm) * 1000, 1)

    if use_rag:
        final = prompt_mod.finalize(answer, contexts, cited_only=body.get("cited_only", True),
                                    include_text=bool(body.get("include_text")))
    else:
        final = {"answer": answer, "references": [], "reference_files": [], "cited": [], "no_evidence": False}
    tok_s = None
    if stats.get("eval_count") and stats.get("eval_duration"):
        tok_s = round(stats["eval_count"] / (stats["eval_duration"] / 1e9), 1)
    metadata = {
        "request_id": req_id, "status": "completed", "rag_provider": "vss" if use_rag else "none",
        "project_id": project_id, "index_id": index_id if use_rag else None,
        "resolved_by": resolved_by if use_rag else None, "model": model,
        # has_evidence 는 끝까지 **검색 기준**입니다 (top_score >= threshold — 불변 조건 5).
        # 검색은 근거를 찾았는데 모델이 NO_EVIDENCE 를 낸 경우 이 값은 true 이고 최상위 no_evidence 도 true 입니다.
        # 둘은 모순이 아니라 서로 다른 단계를 말합니다. 화면 분기는 no_evidence 로 합니다 (docs/API.md).
        # (여기 있던 bool(contexts) 는 102줄에서 이미 return 했으므로 항상 True 인 죽은 표현이었습니다.)
        "has_evidence": r.get("has_evidence") if use_rag else None,
        "reason": r.get("reason") if use_rag else None, "top_score": r.get("top_score") if use_rag else None,
        "threshold": r.get("threshold") if use_rag else None, "history_used": 0,
        # r.get("timing") 이 아니라 timing 을 씁니다 — 그래야 prompt_ms·pre_llm_ms 가 최종 응답까지 옵니다
        # (예전에는 검색 timing 만 실려서 docs/API.md 예시의 prompt_ms 가 실제로는 없었습니다).
        "timing": {**timing, "ttft_ms": ttft, "gen_ms": gen_ms,
                   "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
                   "decode_tok_s": tok_s, "eval_count": stats.get("eval_count")},
    }
    _log("answered", metadata=metadata)
    yield {"event": "done", "data": {**final,
                                     "source": prompt_mod.legacy_sources(contexts) if not final["no_evidence"] else [],
                                     "sources": _light(contexts), "metadata": metadata}}


def collect(body: dict) -> tuple[int, dict]:
    """비스트리밍: 이벤트를 모아 JSON 하나로. (HTTP 코드, payload)"""
    meta: dict = {}
    for ev in run_chat(body):
        if ev["event"] == "meta":
            meta = ev["data"]
        elif ev["event"] == "error":
            code = {"bad_request": 400, "project_not_found": 404, "retrieval_failed": 503,
                    "llm_failed": 502}.get(ev["data"]["code"], 500)
            return code, {"error": ev["data"], "request_id": meta.get("request_id")}
        elif ev["event"] == "done":
            d = dict(ev["data"])
            d["has_evidence"] = meta.get("has_evidence")
            d["stage"] = meta.get("stage")
            d["search_profile"] = meta.get("search_profile")
            d["serving_profile"] = meta.get("serving_profile")
            return 200, d
    return 500, {"error": {"code": "no_result", "message": "응답이 만들어지지 않았습니다"}}
