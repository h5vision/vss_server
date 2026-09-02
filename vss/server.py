"""
vss_server HTTP API — 표준 라이브러리 ThreadingHTTPServer. 서버 하나가 검색·프롬프트·LLM 호출·출처·브리핑·인덱싱을 맡습니다.

  GET  /health                       상태 · 설정 · 인덱스 목록
  GET  /projects                     완성 인덱스 목록 (프론트는 여기서 exact project_id 를 고릅니다)
  GET  /index/status?project_id=     진행률
  GET  /index/exists?project_id=     미인덱싱 감지
  GET  /briefing?project_id=         브리핑 JSON (404 = 아직 없음)
  GET  /briefing.md?project_id=      브리핑 Markdown 원문
  GET  /v1/models                    Ollama 모델 목록
  POST /v1/chat                      {project_id, message, stream?, context?, history?, top_k?, threshold?, model_id?, rag?}
  POST /search                       {query, project_id, top_k?, threshold?, use_bm25?}
  POST /prompt                       {query, project_id, ...}  → messages + 미리보기 출처 (디버그·평가용)
  POST /finalize                     {answer, sources}
  POST /index                        {project_root, project_id, force?, profile?, briefing?}
  POST /briefing                     {project_id, model?, force?}
  POST /bm25                         {project_id}  역색인 재구축

인증: VSS_TOKEN 또는 --token 이 설정되면 X-VSS-Token 헤더를 검사합니다.
"""

from __future__ import annotations

import argparse
import json
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import briefing, chat, embedder, indexer, llm, prompt as prompt_mod, search as search_mod
from .config import CFG, alias_map
from .references import build_references
from .store import ProjectNotFound, get_store

TOKEN: str | None = None
_CORS = {"Access-Control-Allow-Origin": "*",
         "Access-Control-Allow-Headers": "Content-Type, X-VSS-Token, Authorization",
         "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}


def _briefing_hook(model: str | None):
    def cb(project_id: str, root: str, commit: str | None) -> dict:
        return briefing.build(root, project_id, model=model, commit=commit)
    return cb


class Handler(BaseHTTPRequestHandler):
    server_version = "vss-server/0.1"

    # ── 공통 ─────────────────────────────────────────────────
    def _headers(self, code: int, ctype: str, length: int | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k, v in _CORS.items():
            self.send_header(k, v)

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._headers(code, "application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str, ctype: str = "text/markdown"):
        body = text.encode("utf-8")
        self._headers(code, f"{ctype}; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, events):
        self._headers(200, "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for ev in events:
                data = json.dumps(ev["data"], ensure_ascii=False, default=str)
                self.wfile.write(f"event: {ev['event']}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_invalid_json": True}

    def _auth_ok(self) -> bool:
        if not TOKEN:
            return True
        auth = self.headers.get("Authorization") or ""
        return self.headers.get("X-VSS-Token") == TOKEN or auth == f"Bearer {TOKEN}"

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")

    def do_OPTIONS(self):
        self._headers(204, "text/plain", 0)
        self.end_headers()

    # ── GET ──────────────────────────────────────────────────
    def do_GET(self):
        try:
            if not self._auth_ok():
                return self._send(401, {"error": "unauthorized"})
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/") or "/"
            pid = (q.get("project_id") or [None])[0]
            st = get_store()

            if path in ("/", "/health", "/v1/health"):
                return self._send(200, {
                    "ok": True, "store": st.kind, "ollama": CFG.ollama_url,
                    "chat_model": CFG.chat_model, "embed_model": CFG.embed_model,
                    "projects": st.projects(), "incomplete": st.incomplete(),
                    "project_aliases": alias_map(),      # 프론트가 보내는 이름 → 실제로 답하는 인덱스
                    "defaults": {"fingerprint": CFG.fingerprint(), "top_k": CFG.top_k,
                                 "threshold": CFG.score_threshold, "fusion_pool": CFG.fusion_pool},
                    "data_dir": str(CFG.data_path()),
                })
            if path in ("/projects", "/v1/projects"):
                # repos: 프론트가 보낼 짧은 이름 → 지금 그 이름이 닿는 인덱스. 인덱싱만 해도 여기가 따라 옵니다.
                return self._send(200, {"projects": indexer.list_projects(st), "incomplete": st.incomplete(),
                                        "repos": indexer.repo_map(st)})
            if path == "/v1/models":
                return self._send(200, {"models": llm.models(), "default": CFG.chat_model})
            if path == "/index/status":
                if not pid:
                    return self._send(400, {"error": "project_id required"})
                return self._send(200, indexer.status(pid, st))
            if path == "/index/exists":
                if not pid:
                    return self._send(400, {"error": "project_id required"})
                return self._send(200, indexer.exists(pid, st))
            if path == "/briefing":
                if not pid:
                    return self._send(400, {"error": "project_id required"})
                index_id, _ = indexer.resolve_index(pid, st)            # 조회는 자동 선택을 탄다 (인덱싱 경로는 아니다)
                rec = briefing.load(index_id)
                if not rec:
                    return self._send(404, {"ok": False, "reason": "not_generated",
                                            "project_id": pid, "index_id": index_id})
                return self._send(200, {**rec, "project_id": pid, "index_id": index_id})
            if path == "/briefing.md":
                if not pid:
                    return self._send_text(400, "project_id required", "text/plain")
                p = briefing.md_path(indexer.resolve_index(pid, st)[0])
                if not p.exists():
                    return self._send_text(404, f"브리핑이 아직 없습니다: {pid}", "text/plain")
                return self._send_text(200, p.read_text(encoding="utf-8"))
            return self._send(404, {"error": "not found", "path": path})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    # ── POST ─────────────────────────────────────────────────
    def do_POST(self):
        try:
            if not self._auth_ok():
                return self._send(401, {"error": "unauthorized"})
            path = urlparse(self.path).path.rstrip("/")
            body = self._body()
            if body.get("_invalid_json"):
                return self._send(400, {"error": "invalid JSON body"})
            st = get_store()

            if path in ("/v1/chat", "/chat"):
                if body.get("stream"):
                    return self._sse(chat.run_chat(body))
                code, payload = chat.collect(body)
                return self._send(code, payload)

            if path in ("/search", "/v1/search"):
                query = body.get("query") or body.get("message")
                pid = body.get("project_id")
                if not query or not pid:
                    return self._send(400, {"error": "query, project_id required"})
                index_id, resolved_by = indexer.resolve_index(pid, st)
                r = search_mod.search(query, index_id, top_k=body.get("top_k"), threshold=body.get("threshold"),
                                      store=st, search_profile={k: body[k] for k in ("use_bm25", "pool") if k in body})
                if not body.get("include_all_hits"):
                    r.pop("all_hits", None)
                return self._send(200, {**r, "project_id": pid, "index_id": index_id,
                                        "resolved_by": resolved_by})

            if path == "/prompt":
                t0 = time.perf_counter()
                query = body.get("query") or body.get("message")
                pid = body.get("project_id")
                if not query or not pid:
                    return self._send(400, {"error": "query, project_id required"})
                code = chat.selected_code(body.get("context"))
                index_id, resolved_by = indexer.resolve_index(pid, st)
                r = search_mod.search(query, index_id, top_k=body.get("top_k"), threshold=body.get("threshold"),
                                      store=st, search_profile={k: body[k] for k in ("use_bm25", "pool") if k in body},
                                      embed_text=(f"{query}\n{code[:400]}" if code else None))
                r.pop("all_hits", None)
                pre = build_references(r["contexts"], answer=None, cited_only=False,
                                       include_text=bool(body.get("include_text")))
                timing = dict(r.get("timing") or {})
                timing["total_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                return self._send(200, {
                    "has_evidence": r["has_evidence"], "project_id": pid, "index_id": index_id,
                    "resolved_by": resolved_by,
                    "messages": prompt_mod.render_prompt(query, r["contexts"], selected_code=code),
                    "sources": r["contexts"] if body.get("light") is False else chat._light(r["contexts"]),
                    "references": pre["references"], "reference_files": pre["reference_files"],
                    "top_score": r["top_score"], "threshold": r["threshold"], "reason": r["reason"],
                    "serving_profile": r["serving_profile"], "search_profile": r["search_profile"],
                    "bm25_active": r.get("bm25_active", False), "timing": timing})

            if path == "/finalize":
                answer, sources = body.get("answer"), body.get("sources")
                if answer is None or sources is None:
                    return self._send(400, {"error": "answer, sources required"})
                return self._send(200, prompt_mod.finalize(answer, sources, cited_only=body.get("cited_only", True),
                                                           include_text=bool(body.get("include_text"))))

            if path == "/index":
                root, pid = body.get("project_root"), body.get("project_id")
                if not root or not pid:
                    return self._send(400, {"error": "project_root, project_id required"})
                hook = _briefing_hook(body.get("model")) if body.get("briefing", True) else None
                r = indexer.start_index(root, pid, profile=body.get("profile"), force=bool(body.get("force")),
                                        on_done=hook, store=st,
                                        extra_meta={"note": body["note"]} if body.get("note") else None)
                return self._send(202 if r.get("accepted") else 409, r)

            if path == "/briefing":
                pid = body.get("project_id")
                if not pid:
                    return self._send(400, {"error": "project_id required"})
                index_id, _ = indexer.resolve_index(pid, st)
                cached = briefing.load(index_id)
                if cached and not body.get("force"):
                    return self._send(200, {**cached, "cached": True, "project_id": pid, "index_id": index_id})
                info = st.project_info(index_id)
                root = body.get("project_root") or (info or {}).get("project_root")
                if not root:
                    return self._send(404, {"ok": False, "reason": "project_root_unknown",
                                            "message": "인덱싱된 프로젝트가 아니면 project_root 를 함께 주세요"})
                rec = briefing.build(root, index_id, model=body.get("model"), commit=(info or {}).get("commit"))
                return self._send(200 if rec.get("ok") else 422, {**rec, "project_id": pid, "index_id": index_id})

            if path == "/bm25":
                pid = body.get("project_id")
                if not pid:
                    return self._send(400, {"error": "project_id required"})
                return self._send(200, indexer.rebuild_bm25(pid, st))

            return self._send(404, {"error": "not found", "path": path})
        except ProjectNotFound as e:
            return self._send(404, {"error": str(e)})
        except embedder.EmbeddingError as e:
            return self._send(503, {"error": str(e)})
        except llm.LLMError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def main(argv=None):
    global TOKEN
    ap = argparse.ArgumentParser(description="vss_server HTTP API")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--token", default=CFG.token or None)
    ap.add_argument("--no-warmup", action="store_true")
    args = ap.parse_args(argv)
    TOKEN = args.token or None

    print("=" * 60)
    print(f"  vss_server      http://{args.host}:{args.port}")
    print(f"  store           {CFG.store}   data_dir={CFG.data_path()}")
    print(f"  Ollama          {CFG.ollama_url}   chat={CFG.chat_model}   embed={CFG.embed_model}")
    print(f"  auth            {'ON (X-VSS-Token)' if TOKEN else 'OFF'}")
    print("=" * 60)
    if not args.no_warmup:
        print("  워밍업 중...")
        try:
            t = time.perf_counter()
            st = get_store()
            print(f"    인덱스 로드   {(time.perf_counter() - t) * 1000:>7.0f} ms  {st.projects()}")
            inc = st.incomplete()
            if inc:
                print("    ⚠ 미완성 빌드가 있습니다 (조회 대상 아님):", [i["name"] for i in inc])
                print("       진행 중이 아니라면:  python -m vss.cli repair --apply")
        except Exception as e:
            print(f"    !! 저장소 로드 실패: {e}")
        try:
            t = time.perf_counter()
            embedder.embed_one("warmup")
            print(f"    임베딩 워밍업 {(time.perf_counter() - t) * 1000:>7.0f} ms")
            t = time.perf_counter()
            llm.warmup()
            print(f"    생성 모델 워밍업 {(time.perf_counter() - t) * 1000:>5.0f} ms  ({CFG.chat_model})")
        except Exception as e:
            print(f"    !! Ollama 워밍업 실패: {e}")
    print("=" * 60)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
