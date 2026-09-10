from __future__ import annotations

import io
import itertools
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


class FakeBriefingModel:
    def __init__(self):
        self.calls = []
        self.kwargs = []           # 호출마다 받은 키워드 인자 (think 등)
        self.fail_final = False
        self.wide = False          # True 면 주제 분석마다 긴 조건 10개 → final 입력이 예산을 넘는다
        self.plan_paths = ["app.py"]
        self.bad_claim = False     # True 면 주제 분석마다 근거 id 가 틀린 claim 을 하나 더 낸다

    def __call__(self, messages, **kwargs):
        req = json.loads(messages[-1]["content"])
        self.calls.append(req)
        self.kwargs.append(kwargs)
        stage, body = req["stage"], req["input"]
        ids = body.get("evidence_ids") or [e["id"] for e in body.get("evidence", [])]
        if stage == "plan":
            data = {"topics": [{"title": "주문 처리", "questions": ["주문을 어떻게 저장하는가?"],
                                 "paths": list(self.plan_paths), "queries": ["create_order"], "representative": True}]}
        elif stage == "final":
            if self.fail_final:
                return {"content": "{bad", "done_reason": "length", "stats": {"eval_count": 2500}}
            c = {"text": "분석 결과를 종합한 주문 서버입니다.", "evidence_ids": ids[:1]}
            data = {k: [c] for k in ("overview", "features", "flow", "reading")}
            data["unknowns"] = []
        else:
            c = {"text": "주문을 저장합니다.", "evidence_ids": ids[:1]}
            conditions = [{"text": "요청 검증 후에만 저장합니다.", "evidence_ids": ids[:1]}]
            if self.wide and stage == "topic":                # 계수를 실측값으로 낮춘 뒤(0.7토큰/자)에도 final 상한을 넘게 길게
                conditions = [{"text": f"조건 {i} " + "가" * 300, "evidence_ids": ids[:1]} for i in range(10)]
            data = {"claims": [c], "conditions": conditions,
                    "flow": [c], "reading": [c], "compact": [c], "unknowns": [], "followup_queries": []}
            if self.bad_claim and stage == "topic":
                data["claims"].append({"text": "근거가 없는 주장입니다.", "evidence_ids": [ids[0], 999]})
            data = {k: v for k, v in data.items() if k in req["output_format"]}   # 요구한 형식의 키만 답한다 (문서용 형식이 작다)
        return {"content": json.dumps(data, ensure_ascii=False), "done_reason": "stop",
                "stats": {"prompt_eval_count": 500, "eval_count": 200}}


class FakeStore:
    def project_info(self, pid):
        return {"commit": "abc", "dirty": False}
    def index_fingerprint(self, pid):
        return {}


class BriefingPipelineTest(unittest.TestCase):
    def setUp(self):
        # Roundtrip tests reload vss modules; resolve current instances each time.
        from vss import briefing, briefing_pipeline as pipeline, briefing_survey as survey, llm, config
        self.briefing, self.p, self.s, self.llm = briefing, pipeline, survey, llm
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        (self.root / "app.py").write_text(
            '\ufefffrom storage import save\n\ndef create_order(req):\n'
            '    if not req:\n        raise ValueError("empty")\n    return save(req)\n', encoding="utf-8")
        (self.root / "storage.py").write_text('def save(req):\n    return {"saved": req}\n', encoding="utf-8")
        (self.root / "pyproject.toml").write_text('[project.scripts]\norders = "app:create_order"\n', encoding="utf-8")
        self.model = FakeBriefingModel()
        self.data = Path(self.tmp.name) / "data"
        patches = [mock.patch.object(config.CFG, "data_dir", self.data),
                   mock.patch.object(config.CFG, "num_ctx", 8192),
                   mock.patch.object(llm, "models", return_value=["test:latest"]),
                   mock.patch.object(llm, "loaded_names", return_value=["test:latest"]),
                   mock.patch.object(llm, "chat_result", side_effect=self.model),
                   mock.patch("vss.store.get_store", return_value=FakeStore()),
                   mock.patch.object(survey, "git_state", return_value={"commit": None, "dirty": None})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def build(self, pid="demo"):
        return self.briefing.build(str(self.root), pid, model="test:latest")

    def test_without_readme_final_is_last_and_grounded(self):
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        stages = [r["stage"] for r in self.model.calls]
        self.assertEqual(stages[-1], "final")
        self.assertIn("topic", stages)
        final_input = self.model.calls[-1]["input"]
        self.assertTrue(any(t.get("conditions") for t in final_input["topics"]))
        self.assertIn("## 이 프로젝트는", rec["briefing"])
        self.assertNotIn("```mermaid", rec["briefing"])
        self.assertFalse(rec["rag"]["enabled"])
        self.assertEqual(self.briefing.md_path("demo").read_text(encoding="utf-8"), rec["briefing"])
        for c in rec["references"]:
            self.assertTrue((self.root / c["path"]).is_file())
            self.assertGreaterEqual(c["line_start"], 1)
        self.assertEqual(self.briefing.generation_status("demo")["state"], "ready")

    def test_long_readme_reads_late_usage_heading(self):
        (self.root / "README.md").write_text("# Project\n" + "unrelated introduction\n" * 700
                                            + "## Usage\nRun orders to create an order.\n", encoding="utf-8")
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        state = json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))
        self.assertTrue(any("Run orders" in e["text"] for e in state["evidence"]))
        for req in self.model.calls:
            messages = [{"role": "system", "content": self.p.SYSTEM}, {"role": "user", "content": json.dumps(req, ensure_ascii=False)}]
            self.assertLessEqual(self.s.tokens(json.dumps(messages, ensure_ascii=False)) + 128,
                                 4500 if req["stage"] == "final" else 5000)

    def test_bad_final_retains_previous_and_stage_cache_reused(self):
        first = self.build()
        self.assertTrue(first["ok"], first)
        old = self.briefing.load("demo")
        # Remove final cache only; completed source analysis must be reused.
        cache = self.data / "briefings" / "stage_cache" / "demo"
        for file in cache.rglob("*.json"):                      # 2026-09-09 부터 <digest>/ 하위
            if "overview" in json.loads(file.read_text(encoding="utf-8"))["result"]:
                file.unlink()
        self.model.calls.clear()
        self.model.fail_final = True
        second = self.build()
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "output_truncated")
        self.assertEqual(self.briefing.load("demo")["run_id"], old["run_id"])
        self.assertEqual({c["stage"] for c in self.model.calls}, {"final"})

    def test_unloaded_model_never_generates_or_overwrites(self):
        with mock.patch.object(self.llm, "models", return_value=[]):
            rec = self.build()
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["reason"], "model_not_loaded")
        self.assertFalse(self.model.calls)

    def test_invalid_evidence_rejected(self):
        with self.assertRaises(self.p.StageError):
            self.p.validate_analysis({"claims": [{"text": "invented", "evidence_ids": [99]}]}, {1})
        with self.assertRaises(self.p.StageError):
            self.p.validate_analysis({"claims": [{"text": "invented", "evidence_ids": [True]}]}, {1})

    def test_validate_normalizes_ids_and_drops_only_bad_claims(self):
        # 2026-09-09: "1"·1.0 은 손실 없이 정수로, 허용 밖 id 가 든 claim 은 통째로, 배열 아닌 키는 빈 값 — 나머지는 살린다
        out = self.p.validate_analysis({
            "claims": [{"text": "a", "evidence_ids": ["1"]}, {"text": "b", "evidence_ids": [1.0, 2]},
                       {"text": "c", "evidence_ids": [1, 999]}, {"text": "d", "evidence_ids": [True]},
                       {"text": "e", "evidence_ids": []}],
            "conditions": "not-a-list"}, {1, 2})
        self.assertEqual([(c["text"], c["evidence_ids"]) for c in out["claims"]], [("a", [1]), ("b", [1, 2])])
        self.assertEqual(out["conditions"], [])
        self.assertEqual(out["_dropped"], {"claims": 3, "ids": 2, "keys": ["conditions"], "truncated": []})
        many = {"claims": [{"text": f"c{i}", "evidence_ids": [1]} for i in range(15)]}
        out = self.p.validate_analysis(many, {1})
        self.assertEqual(len(out["claims"]), 12)
        self.assertEqual(out["_dropped"]["truncated"], ["claims"])
        clean = self.p.validate_analysis({"claims": [{"text": "ok", "evidence_ids": [1]}]}, {1})
        self.assertNotIn("_dropped", clean)                                    # 무손실이면 기록 없음

    def test_dropped_claims_mark_partial_and_survive_cache(self):
        self.model.bad_claim = True
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["quality_status"], "partial")
        self.assertNotIn("근거가 없는 주장", rec["briefing"])
        state = json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))
        drops = [p for p in state["problems"] if p["reason"] == "claims_dropped"]
        self.assertTrue(drops and all(p["claims"] == 1 for p in drops))
        self.model.calls.clear()
        rec = self.build()                                                     # 전부 캐시에서 — 손실 기록도 따라온다
        self.assertTrue(rec["ok"], rec)
        self.assertFalse(self.model.calls)
        self.assertEqual(rec["quality_status"], "partial")
        state = json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))
        self.assertTrue(any(p["reason"] == "claims_dropped" for p in state["problems"]))

    def test_plan_unknown_paths_dropped_but_topic_kept(self):
        self.model.plan_paths = ["app.py", "src/nope.py", "./storage.py", "app.py"]
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        topic = next(a for a in rec["topics"] if a["topic"]["title"] == "주문 처리")
        self.assertEqual(topic["topic"]["paths"], ["app.py", "storage.py"])
        plan_metric = next(m for m in rec["metrics"] if m.get("stage") == "plan")
        self.assertEqual(plan_metric["dropped"], {"paths": 1, "topics": 0})
        self.assertEqual(rec["quality_status"], "complete")                    # 경로 제거는 내용 손실이 아니다

    def test_time_budget_skips_topics_but_final_still_runs(self):
        # _now() 가 불릴 때마다 100초 흐른다. 문서 배치 하나는 들어가고, plan·주제는 예산 초과로 건너뛰며, final 은 예약 몫(120초)으로 돈다
        (self.root / "README.md").write_text("# Project\nRun orders to create an order.\n", encoding="utf-8")
        clock = itertools.count(0, 100)
        with mock.patch.object(self.p, "_now", side_effect=lambda: next(clock)), \
             mock.patch.object(self.p.CFG, "briefing_time_budget", 600):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["quality_status"], "partial")
        self.assertTrue(rec["topics"] and all(a.get("error") == "time_budget" for a in rec["topics"]))
        self.assertEqual(sum(1 for c in self.model.calls if c["stage"] == "topic"), 0)
        self.assertEqual(self.model.calls[-1]["stage"], "final")
        final_metric = [m for m in rec["metrics"] if m.get("stage") == "final" and "attempt" in m][-1]
        self.assertEqual(final_metric["timeout_s"], self.p.FINAL_RESERVE_S)
        state = json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))
        self.assertTrue(any(p["reason"] == "time_budget" for p in state["problems"]))
        self.assertTrue(all(m["timeout_s"] <= self.p.CFG.chat_timeout * 4 for m in rec["metrics"] if "timeout_s" in m))

    def test_weak_candidates_do_not_call_model(self):
        # 경로·심볼·호출 대상 일치가 없고 본문 한 줄("error")만 맞는 고정 주제는 호출 없이 weak_candidates 로 끝난다
        root = Path(self.tmp.name) / "weak"
        root.mkdir()
        (root / "notes.py").write_text("VALUE = 1  # error handling comes later\n", encoding="utf-8")
        (root / "README.md").write_text("# Notes\nA small helper module for testing.\n", encoding="utf-8")
        self.model.plan_paths = ["notes.py"]
        rec = self.briefing.build(str(root), "weak", model="test:latest")
        self.assertTrue(rec["ok"], rec)
        by = {a["topic"]["title"]: a for a in rec["topics"]}
        self.assertEqual(by["주문 처리"]["status"], "analyzed")                # 경로 일치 → 호출
        for title in ("실행과 진입점", "데이터와 외부 의존성", "설정과 제약"):
            self.assertEqual(by[title].get("error"), "weak_candidates", title)
        self.assertEqual(sum(1 for c in self.model.calls if c["stage"] == "topic"), 1)

    def test_call_target_match_is_a_strong_candidate(self):
        # 설정·의존성 파일이 없어도 user.has_permission()·store.save() 호출이 있으면 그 고정 주제는 조사한다
        root = Path(self.tmp.name) / "svc"
        root.mkdir()
        (root / "service.py").write_text(
            "import store\n\ndef handle(req, user):\n    if not user.has_permission('write'):\n"
            "        raise PermissionError('denied')\n    return store.save(req)\n", encoding="utf-8")
        self.model.plan_paths = ["service.py"]
        rec = self.briefing.build(str(root), "svc", model="test:latest")
        self.assertTrue(rec["ok"], rec)
        by = {a["topic"]["title"]: a for a in rec["topics"]}
        self.assertEqual(by["설정과 제약"]["status"], "analyzed")
        self.assertEqual(by["데이터와 외부 의존성"]["status"], "analyzed")
        self.assertEqual(by["실행과 진입점"].get("error"), "weak_candidates")

    def test_readme_head_then_other_docs_priority_sections(self):
        # 긴 README 가 배치를 다 차지해 docs/guide.md 의 Usage 가 빠지던 순서를 고친다. README 첫 문단과 뒤쪽 Usage 는 여전히 읽는다
        (self.root / "README.md").write_text("# Project\nThis project handles orders.\n" + "filler line\n" * 700
                                            + "## Usage\nRun orders to create an order.\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.md").write_text("# Guide\nguide intro\n## Usage\nAuth usage: send the token header.\n",
                                                     encoding="utf-8")
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        state = json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))
        docs = [e for e in state["evidence"] if e["type"] == "doc"]
        def first(needle):
            return next(e["id"] for e in docs if needle in e["text"])
        self.assertLess(first("handles orders"), first("Auth usage"))
        self.assertLess(first("Run orders"), first("Auth usage"))
        tail = next(e["id"] for e in docs if e["path"] == "README.md" and e["section"].endswith("(계속)"))
        self.assertLess(first("Auth usage"), tail)                             # 다른 문서의 Usage 가 README 나머지보다 먼저
        self.assertLessEqual(len(state["documents"]), self.p.CFG.briefing_doc_batches)

    # ── 진입점·라우트·함수 헤더 (2026-09-09, md 결정: CHARTER 범위 3 복원) ──
    _FASTAPI_MAIN = (
        "from fastapi import FastAPI\napp = FastAPI()\n\n"
        "def _helper(x: int) -> int:\n    \"\"\"Double the value.\"\"\"\n    return x * 2\n\n"
        "@app.get(\"/pay\")\ndef pay(req):\n    return _helper(1)\n\n"
        "@app.post(\n    \"/refund\",\n)\ndef refund(\n    req,\n    user,\n) -> int:\n    \"\"\"Refund a payment.\n\n    Details.\"\"\"\n    return 0\n"
        "app.include_router(users.router, prefix=\"/v1\")\n")

    def test_entry_headers_and_routes_in_briefing(self):
        (self.root / "main.py").write_text(self._FASTAPI_MAIN, encoding="utf-8")
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        md = rec["briefing"]
        self.assertIn("## 진입점", md)
        self.assertIn("`main.py`:L2 — 파일명 규칙(main.py)", md)                 # 한국어 reason, 마커 줄
        self.assertNotIn("filename_candidate", md)
        self.assertIn("### `main.py`", md)
        self.assertIn("- L4 `def _helper(x: int) -> int:` — Double the value.", md)
        self.assertNotIn("`def pay(req):`", md)                                   # 라우트 핸들러는 헤더 목록에서 제외
        self.assertIn("## 라우트·등록", md)
        self.assertIn("- `GET /pay` → `pay` (main.py:8)", md)
        self.assertIn("- `POST /refund` → `refund` (main.py:12)", md)
        self.assertIn("- `include_router`(`users.router`, `/v1`) (main.py:23) — 정적 후보", md)
        first = rec["structure"]["entry_points"][0]
        self.assertEqual(first["path"], "main.py")
        self.assertIn("pay", [s["symbol"] for s in first["symbols"]])              # JSON 은 상한·제외 없이 전부
        http = [r for r in rec["routes"] if r.get("kind") == "http"]
        self.assertEqual([(r["method"], r["url"], r["symbol"]) for r in http], [("GET", "/pay", "pay"), ("POST", "/refund", "refund")])
        self.assertTrue(all(k in http[0] for k in ("path", "line", "symbol", "registration", "arguments", "candidate")))

    def test_mock_patch_is_not_a_route_and_test_app_is_flagged(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_pay.py").write_text(
            "from unittest import mock\nfrom fastapi import FastAPI\napp = FastAPI()\n\n"
            "@mock.patch(\"app.save\")\ndef test_x(m):\n    pass\n\n@app.get(\"/t\")\ndef t():\n    return 1\n", encoding="utf-8")
        s = self.s.Survey(str(self.root))
        self.assertFalse([r for r in s.interfaces if "patch" in r["registration"]])
        route = next(r for r in s.interfaces if r.get("kind") == "http")
        self.assertEqual((route["url"], route["test"]), ("/t", True))
        self.assertFalse(any(e["path"].startswith("tests/") for e in s.entries))
        rec = self.build()
        self.assertNotIn("`GET /t`", rec["briefing"])                                     # 테스트 라우트는 본문에서 접힌다
        self.assertIn("- 테스트 파일의 라우트·등록 1개는 생략", rec["briefing"])
        self.assertTrue(any(r.get("url") == "/t" and r.get("test") for r in rec["routes"]))  # JSON 에는 그대로

    def test_bom_file_routes_are_extracted(self):
        (self.root / "svc.py").write_text("﻿from fastapi import FastAPI\napp = FastAPI()\n\n@app.post(\"/x\")\ndef x():\n    return 1\n",
                                          encoding="utf-8")
        s = self.s.Survey(str(self.root))
        self.assertEqual([r["url"] for r in s.interfaces if r.get("kind") == "http"], ["/x"])

    def test_typer_command_registration_is_kept(self):
        (self.root / "cli.py").write_text("import typer\napp = typer.Typer()\n\n@app.command()\ndef serve(port: int = 8000):\n"
                                          "    \"\"\"Run the server.\"\"\"\n    pass\n", encoding="utf-8")
        s = self.s.Survey(str(self.root))
        cmd = next(r for r in s.interfaces if r.get("kind") == "command")
        self.assertEqual((cmd["registration"], cmd["symbol"], cmd["line"]), ("app.command", "serve", 4))
        rec = self.build()
        self.assertIn("- `app.command` → `serve` (cli.py:4) — 정적 후보", rec["briefing"])

    def test_multiline_signature_and_docstring(self):
        (self.root / "main.py").write_text(self._FASTAPI_MAIN, encoding="utf-8")
        s = self.s.Survey(str(self.root))
        by = {x["symbol"]: x for x in s.symbols if x["path"] == "main.py"}
        self.assertEqual(by["refund"]["signature"], "def refund( req, user, ) -> int:")
        self.assertEqual(by["refund"]["doc"], "Refund a payment.")
        self.assertEqual(by["_helper"]["signature"], "def _helper(x: int) -> int:")
        router = next(r for r in s.interfaces if r.get("kind") == "router")
        self.assertEqual((router["router"], router["prefix"], router["arguments"]), ("users.router", "/v1", ["users.router", "/v1"]))

    def test_rag_all_hits_are_read_candidates_with_local_text(self):
        # 2026-09-09: threshold 미달(contexts 비어 있음)이어도 all_hits 를 읽을 위치 후보로 쓴다. 근거 본문은 로컬 원문이고
        # 경로 밖 후보는 버린다. 질의는 한국어 질문 + 식별자
        fake = {"contexts": [], "reason": "below_threshold", "all_hits": [
            {"path": "storage.py", "line_start": 1, "line_end": 2, "text": "FAKE INDEX TEXT"},
            {"path": "../outside.py", "line_start": 1, "line_end": 3, "text": "FAKE INDEX TEXT"}]}
        with mock.patch.object(self.s, "git_state", return_value={"commit": "abc", "dirty": False}), \
             mock.patch("vss.search.search", return_value=fake) as search:
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertTrue(rec["rag"]["enabled"])
        state = json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))
        self.assertTrue(state["retrieval"])
        self.assertTrue(all((r["candidates"], r["adopted"], r["reason"]) == (2, 1, "below_threshold") for r in state["retrieval"]))
        texts = [e["text"] for e in state["evidence"]]
        self.assertTrue(any("def save(req)" in t for t in texts))
        self.assertFalse(any("FAKE INDEX TEXT" in t for t in texts))
        queries = [c.args[0] for c in search.call_args_list]
        self.assertIn("주문을 어떻게 저장하는가?", queries)
        self.assertIn("create_order", queries)

    def test_briefing_body_uses_reader_wording_not_codes(self):
        (self.root / "web.js").write_text("export const x = 1;\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Project\nRun orders to create an order.\n", encoding="utf-8")
        self.model.bad_claim = True
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        md = rec["briefing"]
        for code in ("text_only_language", "unverified_or_mismatched_revision", "filename_candidate",
                     "document_budget_omitted", "context_budget_exceeded", "claims_dropped", "단계 기록", "RAG 사용 안 함"):
            self.assertNotIn(code, md, code)
        self.assertIn("- Python 이외 코드 파일 1개는 본문 검색만 했습니다", md)
        self.assertIn("- 부분 결과입니다 — 근거 확인 안 된 설명 제외.", md)
        self.assertEqual(rec["quality_status"], "partial")
        self.assertTrue(rec["problems"] and all(p["reason"] == "claims_dropped" for p in rec["problems"]))
        self.model.bad_claim = False
        rec = self.build("demo-clean")
        self.assertTrue(rec["ok"], rec)
        self.assertNotIn("부분 결과", rec["briefing"])
        self.assertEqual(rec["problems"], [])

    # ── run·캐시 정리 (2026-09-09, md 결정: 최근 3 + 발행 run, 현재 digest 캐시만) ──
    def test_busy_request_leaves_no_run_dir(self):
        self._lock()
        with mock.patch.object(self.p, "_boot_id", return_value="boot-A"), mock.patch.object(self.p, "_pid_alive", return_value=True):
            rec = self.build()
        self.assertEqual(rec["reason"], "briefing_busy")
        self.assertFalse((self.data / "briefings" / "runs" / "demo").exists())

    def test_old_runs_pruned_but_published_and_recent_kept(self):
        runs = self.data / "briefings" / "runs" / "demo"
        for name in ("20200101T000000-aaaaaaaa", "20200102T000000-bbbbbbbb"):
            (runs / name).mkdir(parents=True)
            (runs / name / "failure.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(self.p.CFG, "briefing_keep_runs", 1):
            first = self.build()
            self.assertTrue(first["ok"], first)
            self.assertEqual([d.name for d in runs.iterdir()], [first["run_id"]])          # 가짜 옛 run 둘은 지워졌다
            self.model.fail_final = True
            for file in (self.data / "briefings" / "stage_cache" / "demo").rglob("*.json"):
                if "overview" in json.loads(file.read_text(encoding="utf-8"))["result"]:
                    file.unlink()
            second = self.build()
            third = self.build()
        self.assertFalse(second["ok"] or third["ok"])
        names = sorted(d.name for d in runs.iterdir())
        self.assertIn(first["run_id"], names)                                              # 발행 run 은 keep 과 무관하게 보호
        self.assertIn(third["run_id"], names)                                              # 최근 1개
        self.assertNotIn(second["run_id"], names)
        self.assertEqual(self.briefing.load("demo")["run_id"], first["run_id"])
        st = self.briefing.generation_status("demo")
        self.assertEqual(st["cleanup"]["runs_removed"], 1)

    def test_cache_layout_and_old_flat_cache_removed(self):
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        cache = self.data / "briefings" / "stage_cache" / "demo"
        digests = [d for d in cache.iterdir() if d.is_dir()]
        self.assertEqual(len(digests), 1)
        self.assertTrue(list(digests[0].glob("*.json")))
        (cache / "old-flat.json").write_text("{}", encoding="utf-8")
        (cache / "deadbeefdeadbeef").mkdir()
        (cache / "deadbeefdeadbeef" / "x.json").write_text("{}", encoding="utf-8")
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(sorted(p.name for p in cache.iterdir()), [digests[0].name])
        self.assertEqual(self.briefing.generation_status("demo")["cleanup"]["cache_removed"], 2)

    def test_survey_written_once_and_analysis_json_holds_only_dynamic_parts(self):
        written = []
        original = self.p.atomic_json
        def counting(path, obj):
            written.append(Path(path).name)
            return original(path, obj)
        with mock.patch.object(self.p, "atomic_json", side_effect=counting):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(written.count("survey.json"), 1)
        self.assertGreater(written.count("analysis.json"), 3)
        run_dir = Path(rec["analysis_path"]).parent
        survey = json.loads((run_dir / "survey.json").read_text(encoding="utf-8"))
        state = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
        self.assertTrue(survey["connections"] and survey["symbols"] and survey["files"])
        for key in ("connections", "symbols", "files", "survey"):
            self.assertNotIn(key, state)
        self.assertEqual(state["survey_path"], str(run_dir / "survey.json"))
        self.assertTrue(state["evidence"] and "problems" in state)

    def test_metrics_record_char_counts_for_calibration(self):
        # ⑨-a: 호출마다 ASCII·그 밖 글자 수를 남긴다. 추정식과 맞아야 EC2 의 prompt_eval_count 로 계수를 풀 수 있다
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        calls = [m for m in rec["metrics"] if "attempt" in m]
        self.assertTrue(calls)
        for m in calls:
            self.assertIsInstance(m["chars_ascii"], int)
            self.assertIsInstance(m["chars_other"], int)
            self.assertGreater(m["chars_other"], 0)                                      # 한국어 지시문이 들어 있다
            cfg = self.p.CFG
            self.assertEqual(m["input_estimate"], int(m["chars_ascii"] / cfg.briefing_chars_per_token_ascii
                                                      + m["chars_other"] * cfg.briefing_tokens_per_char_other) + 1 + 128)
        docs = [m for m in calls if m["stage"] == "documents"]
        self.assertTrue(all(m["num_predict"] == self.p.DOC_NUM_PREDICT for m in docs) if docs else True)

    def test_documents_use_compact_output_format(self):
        # ⑪ (2026-09-09): 문서 호출은 claims·conditions·compact 만 요구하고 출력 상한 1500 — 흐름·읽을 위치는 안 시킨다
        (self.root / "README.md").write_text("# Project\nRun orders to create an order.\n## Usage\nSee docs.\n", encoding="utf-8")
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        docs = [c for c in self.model.calls if c["stage"] == "documents"]
        self.assertTrue(docs)
        self.assertEqual(set(docs[0]["output_format"]), {"claims", "conditions", "compact", "unknowns"})
        topics = [c for c in self.model.calls if c["stage"] == "topic"]
        self.assertIn("flow", topics[0]["output_format"])                                   # 주제 조사는 그대로
        self.assertTrue(all(m["num_predict"] == 1500 for m in rec["metrics"] if m.get("stage") == "documents" and "attempt" in m))
        state = json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))
        self.assertTrue(state["documents"])
        self.assertEqual(state["documents"][0]["analysis"]["flow"], [])                      # 빠진 배열은 빈 값
        self.assertTrue(state["documents"][0]["analysis"]["compact"])                         # plan·final 이 쓰는 요약은 있다

    def test_changelog_docs_read_last_and_topics_ordered_by_relevance(self):
        # 2026-09-09 ⑩: release-notes 류는 절 제목("Features")과 무관하게 맨 뒤 / 주제 순서는 대표 → 모델 선택 → 고정(의존성·설정)
        (self.root / "README.md").write_text("# Project\nOrders service.\n## Usage\nRun orders.\n", encoding="utf-8")
        (self.root / "release-notes.md").write_text("# Release Notes\n## 0.2\n### Features\nAdded X.\n## 0.1\n### Features\nAdded Y.\n",
                                                    encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.md").write_text("# Guide\nintro\n## Config\nSet the token.\n", encoding="utf-8")
        s = self.s.Survey(str(self.root))
        order = [(x["path"], x["heading"]) for x in s.sections()]
        self.assertTrue(all(p != "release-notes.md" for p, _ in order[:4]))
        self.assertEqual([p for p, _ in order][-4:], ["release-notes.md"] * 4)
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        titles = [a["topic"]["title"] for a in rec["topics"]]
        self.assertEqual(titles[0], "실행과 진입점")                                      # 대표 주제
        self.assertEqual(titles[1], "주문 처리")                                          # 모델이 고른 대표 주제
        self.assertEqual(titles[-2:], ["데이터와 외부 의존성", "설정과 제약"])              # 고정 일반 주제는 맨 뒤
        self.assertEqual([a["topic"]["id"] for a in rec["topics"]][:2], ["T1", "T2"])

    # ── ⑫ (2026-09-09) ──
    def test_no_call_starts_with_less_than_min_call_time(self):
        # 남은 시간이 final 몫 + 60초 아래면 새 호출을 시작하지 않는다 (30초 제한으로 시작해 시간 초과로 버리지 않게)
        p = self.p.Pipeline(str(self.root), "demo", "test:latest", None)
        with mock.patch.object(self.p, "_now", return_value=p.started + 600 - self.p.FINAL_RESERVE_S - 30), \
             mock.patch.object(self.p.CFG, "briefing_time_budget", 600):
            p.deadline = p.started + 600
            self.assertTrue(p.over_budget())
        with mock.patch.object(self.p, "_now", return_value=p.started + 600 - self.p.FINAL_RESERVE_S - 90):
            self.assertFalse(p.over_budget())
            self.assertGreaterEqual(p.call_timeout_for(False), self.p.MIN_CALL_S)

    def test_merge_failure_keeps_both_parts(self):
        # 병합 호출이 시간 초과여도 주제는 실패하지 않고 두 부분 분석을 이어 붙인다
        self.model.wide = True                                     # 조건이 길어 한 주제가 두 pack 으로 갈리고 병합이 필요해진다
        base = self.model
        def flaky(messages, **kwargs):
            req = json.loads(messages[-1]["content"])
            if req["stage"] == "topic_merge":
                base.calls.append(req)
                raise TimeoutError("timed out")
            return base(messages, **kwargs)
        with mock.patch.object(self.llm, "chat_result", side_effect=flaky):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        merges = [c for c in self.model.calls if c["stage"] == "topic_merge"]
        if merges:                                                  # 병합이 일어난 주제는 전부 analyzed 로 남아야 한다
            self.assertTrue(all(a["status"] == "analyzed" for a in rec["topics"] if a["reads"] and len(a.get("used_ids", [])) > 1))
            self.assertTrue(any(p["stage"] == "topic_merge" and p["reason"] == "llm_timeout" for p in rec["problems"]))

    def test_unknowns_deduplicated_by_prefix(self):
        base = self.model
        def with_unknowns(messages, **kwargs):
            r = base(messages, **kwargs)
            data = json.loads(r["content"])
            if "unknowns" in data:
                data["unknowns"] = ["get_import_data 함수의 내부 구현이 제공된 근거에 없습니다.",
                                    "get_import_data 함수의 내부 구현"]
            r["content"] = json.dumps(data, ensure_ascii=False)
            return r
        with mock.patch.object(self.llm, "chat_result", side_effect=with_unknowns):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        section = rec["briefing"].split("## 확인이 필요한 사항")[1].split("## 근거")[0]
        self.assertEqual(section.count("get_import_data 함수의 내부 구현"), 1)

    def test_changelog_limited_to_one_batch(self):
        (self.root / "README.md").write_text("# Project\nOrders service.\n## Usage\nRun orders.\n", encoding="utf-8")
        body = "".join(f"## 0.{i}\n### Features\n" + f"Added feature number {i} with a fairly long description line.\n" * 12 for i in range(120))
        (self.root / "release-notes.md").write_text("# Release Notes\n" + body, encoding="utf-8")
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        state = json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))
        ev = {e["id"]: e["path"] for e in state["evidence"]}
        with_notes = [b for b in state["documents"] if any(ev[i] == "release-notes.md" for i in b["evidence_ids"])]
        self.assertEqual(len(with_notes), 1)
        self.assertTrue(any(ev[i] == "README.md" for b in state["documents"] for i in b["evidence_ids"]))

    def test_survey_resolve_path(self):
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "util.py").write_text("x = 1\n", encoding="utf-8")
        for d in ("a", "b"):
            (self.root / d).mkdir()
            (self.root / d / "dup.py").write_text("y = 2\n", encoding="utf-8")
        s = self.s.Survey(str(self.root))
        self.assertEqual(s.resolve_path("./app.py"), "app.py")
        self.assertEqual(s.resolve_path("pkg\\util.py"), "pkg/util.py")
        self.assertEqual(s.resolve_path("util.py"), "pkg/util.py")
        self.assertIsNone(s.resolve_path("dup.py"))                            # 둘 이상이면 추측하지 않는다
        self.assertIsNone(s.resolve_path("nope.py"))
        self.assertIsNone(s.resolve_path(""))

    def test_background_duplicate_and_completion(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        actual = self.briefing.build
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            try:
                return actual(*args, **kwargs)
            finally:
                finished.set()
        with mock.patch.object(self.briefing, "build", side_effect=delayed):
            job = self.briefing.start_background(str(self.root), "demo", model="test:latest")
            try:
                self.assertTrue(job["accepted"])
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.briefing.generation_status("demo")["state"], "queued")
                duplicate = self.briefing.start_background(str(self.root), "demo", model="test:latest")
                self.assertEqual(duplicate["reason"], "briefing_busy")
            finally:
                release.set()
                self.assertTrue(finished.wait(5))
        self.assertEqual(self.briefing.generation_status("demo")["state"], "ready")

    def test_actual_context_overflow_does_not_publish(self):
        def oversized(*args, **kwargs):
            result = self.model(*args, **kwargs)
            result["stats"]["prompt_eval_count"] = 8000
            return result
        with mock.patch.object(self.llm, "chat_result", side_effect=oversized):
            rec = self.build()
        self.assertFalse(rec["ok"])
        self.assertIsNone(self.briefing.load("demo"))

    def test_call_cap_is_enforced(self):
        with mock.patch.object(self.p, "MAX_CALLS", 2):
            rec = self.build()
        self.assertLessEqual(len(self.model.calls), 2)
        self.assertFalse(rec["ok"])

    def test_private_agent_docs_excluded(self):
        (self.root / "adocs").mkdir()
        (self.root / "adocs" / "notes.md").write_text("private")
        (self.root / "AGENTS.md").write_text("private")
        survey = self.s.Survey(str(self.root))
        self.assertNotIn("AGENTS.md", survey.files)
        self.assertNotIn("adocs/notes.md", survey.files)

    def test_survey_bom_symbols_and_no_path_escape(self):
        s = self.s.Survey(str(self.root))
        self.assertTrue(any(r["symbol"] == "create_order" for r in s.symbols))
        self.assertTrue(s.commands)
        self.assertTrue(any(r["target"] == "save" for r in s.connections))
        self.assertIsNone(s.add("../outside.py"))
        self.assertIsNone(s.add("app.py", 999))
        e = s.add("app.py", 2, 1000)
        self.assertLessEqual(e["line_end"], len(s.sources["app.py"]))

    def test_source_mutation_after_scan_publishes_partial_with_warning(self):
        # md 결정 2026-09-09: 생성 중(scan 뒤) 바뀐 소스는 분석을 섞지 않으므로 버리지 않고 표시해 발행한다
        original = self.p.Pipeline.final
        def final(p):
            result = original(p)
            (self.root / "storage.py").write_text("# changed\n", encoding="utf-8")
            return result
        with mock.patch.object(self.p.Pipeline, "final", final):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["quality_status"], "partial")
        sc = next(p for p in rec["problems"] if p["reason"] == "source_changed")
        self.assertEqual((sc["count"], sc["paths"]), (1, ["storage.py"]))
        self.assertIn("생성 중 소스가 바뀌어 일부 줄 번호가 어긋날 수 있습니다 (파일 1개)", rec["briefing"])
        self.assertEqual(self.briefing.load("demo")["run_id"], rec["run_id"])
        self.assertTrue(any("def save(req)" in e["text"] for e in json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))["evidence"]))

    def test_source_mutation_during_scan_rescans_or_fails_early(self):
        # scan 도중 바뀌면(섞인 버전) 바로 다시 읽는다. 다시 읽어도 또 바뀌면 LLM 호출 전에 source_unstable
        original = self.s.Survey._scan
        calls = []
        def mutating(survey):
            original(survey)
            calls.append(1)
            if len(calls) <= self.mutations:          # 매번 다른 내용이어야 두 번째 시나리오에서도 해시가 바뀐다
                (self.root / "storage.py").write_text(
                    f"def save(req):\n    return {len(calls)}  # {itertools.count().__class__.__name__}-{id(calls)}-{len(calls)}-{self.mutations}\n",
                    encoding="utf-8")
        self.mutations = 1
        with mock.patch.object(self.s.Survey, "_scan", mutating):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(len(calls), 2)                                            # 한 번 다시 읽었다
        self.assertEqual(rec["quality_status"], "complete")                        # 재scan 뒤에는 바뀐 게 없다
        texts = [e["text"] for e in json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))["evidence"]]
        self.assertTrue(any("return 1" in t for t in texts))                       # 새 내용으로 분석했다
        calls.clear()
        self.mutations = 99
        self.model.calls.clear()
        with mock.patch.object(self.s.Survey, "_scan", mutating):
            rec = self.build("demo-unstable")
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["reason"], "source_unstable")
        self.assertFalse(self.model.calls)

    def test_revision_mismatch_disables_rag(self):
        with mock.patch.object(self.s, "git_state", return_value={"commit": "new", "dirty": False}), \
             mock.patch("vss.search.search") as search:
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        search.assert_not_called()
        self.assertEqual(rec["rag"]["reason"], "unverified_or_mismatched_revision")

    def test_matching_revision_uses_current_search(self):
        with mock.patch.object(self.s, "git_state", return_value={"commit": "abc", "dirty": False}), \
             mock.patch("vss.search.search", return_value={"contexts": [], "reason": "below_threshold"}) as search:
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertTrue(search.called)
        self.assertTrue(all(call.kwargs["top_k"] == 5 for call in search.call_args_list))

    def test_final_compacts_conditions_before_failing(self):
        # 주제 4개 × 조건 10개(각 100자 한글)는 final 상한 4500 을 넘는다. 실패 대신 조건 3개로 줄여 통과해야 한다
        self.model.wide = True
        rec = self.build()
        self.assertTrue(rec["ok"], rec)
        final_input = self.model.calls[-1]["input"]
        self.assertEqual(self.model.calls[-1]["stage"], "final")
        self.assertTrue(all(len(t.get("conditions", [])) <= 3 for t in final_input["topics"]))
        self.assertTrue(any(t.get("conditions") for t in final_input["topics"]))          # 조건을 통째로 지우지는 않는다
        state = json.loads(Path(rec["analysis_path"]).read_text(encoding="utf-8"))
        compacted = [p for p in state["problems"] if p["stage"] == "final"]
        self.assertEqual(compacted[0]["reason"], "compacted")
        self.assertIn("conditions_3", compacted[0]["steps"])
        self.assertEqual(rec["quality_status"], "partial")

    def test_topic_timeout_fails_only_that_topic(self):
        base = self.model
        def flaky(messages, **kwargs):
            req = json.loads(messages[-1]["content"])
            if req["stage"] == "topic" and req["input"]["topic"]["id"] == "T1":
                base.calls.append(req)
                raise TimeoutError("timed out")
            return base(messages, **kwargs)
        with mock.patch.object(self.llm, "chat_result", side_effect=flaky), \
             mock.patch.object(self.p.CFG, "briefing_time_budget", 0):      # 예산 없음 → 호출 상한은 chat_timeout×4 그대로
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["quality_status"], "partial")
        t1 = next(a for a in rec["topics"] if a["topic"]["id"] == "T1")
        self.assertEqual(t1["error"], "llm_timeout")
        # 재시도도, 보완 라운드도 없이 한 번만 기다린다
        self.assertEqual(sum(1 for c in base.calls if c["stage"] == "topic" and c["input"]["topic"]["id"] == "T1"), 1)
        self.assertEqual(base.calls[-1]["stage"], "final")
        self.assertTrue(all(m["timeout_s"] == self.p.CFG.chat_timeout * 4 for m in rec["metrics"] if "timeout_s" in m))

    def test_briefing_think_is_sent_and_keyed_into_cache(self):
        # 브리핑 전용 think (2026-09-09): 기본 false 가 호출에 실리고 metric 에 남는다. 값을 바꾸면 캐시가 재사용되지 않는다
        with mock.patch.object(self.p.CFG, "briefing_think", "false"):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertTrue(self.model.kwargs)
        self.assertTrue(all(k.get("think") is False for k in self.model.kwargs))
        self.assertTrue(all(m["think"] is False for m in rec["metrics"] if "attempt" in m))
        first = len(self.model.calls)
        with mock.patch.object(self.p.CFG, "briefing_think", "low"):          # gpt-oss 식 문자열은 그대로 실린다
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(len(self.model.calls), first * 2)                    # 실효값이 캐시 키에 들어가 전부 다시 생성
        self.assertTrue(all(k.get("think") == "low" for k in self.model.kwargs[first:]))
        with mock.patch.object(self.p.CFG, "briefing_think", ""), mock.patch.object(self.p.CFG, "think", ""):
            rec = self.build()                                                # 비면 VSS_THINK 를 따른다 (여기선 안 실림)
        self.assertTrue(rec["ok"], rec)
        self.assertTrue(all(k.get("think") is None for k in self.model.kwargs[first * 2:]))

    def test_unsupported_think_value_fails_fast(self):
        # 모델이 think 값을 거부하면 조용히 바꿔 보내지 않고 run 전체를 think_unsupported 로 끝낸다
        def rejecting(messages, **kwargs):
            raise self.llm.LLMError('Ollama HTTP 400: b\'{"error":"\\"test:latest\\" does not support thinking"}\'')
        with mock.patch.object(self.p.CFG, "briefing_think", "low"), \
             mock.patch.object(self.llm, "chat_result", side_effect=rejecting):
            rec = self.build()
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["reason"], "think_unsupported")
        self.assertIn("VSS_BRIEFING_THINK", rec["message"])
        self.assertIsNone(self.briefing.load("demo"))

    def test_transient_transport_error_is_retried_once(self):
        # Ollama 500 한 번(runner 재기동 등)은 같은 입력으로 한 번 더 시도해 넘어간다. run 은 정상 완료
        base, seen = self.model, []
        def flaky(messages, **kwargs):
            req = json.loads(messages[-1]["content"])
            if req["stage"] == "topic" and req["input"]["topic"]["id"] == "T1" and not seen:
                seen.append(req)
                raise self.llm.LLMError("Ollama HTTP 500: b'runner crashed'")
            return base(messages, **kwargs)
        with mock.patch.object(self.llm, "chat_result", side_effect=flaky):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["quality_status"], "complete")
        t1 = next(a for a in rec["topics"] if a["topic"]["id"] == "T1")
        self.assertEqual(t1["status"], "analyzed")
        self.assertEqual([m["error"] for m in rec["metrics"] if m.get("error")], ["llm_error"])

    def test_persistent_transport_error_fails_only_that_topic(self):
        # 두 번 다 실패하면 그 주제만 llm_error 로 남고 보완 라운드도 건너뛴다. final 까지 진행, 이전 브리핑 아닌 새 partial
        base, attempts = self.model, []
        def broken(messages, **kwargs):
            req = json.loads(messages[-1]["content"])
            if req["stage"] == "topic" and req["input"]["topic"]["id"] == "T1":
                attempts.append(req)
                raise self.llm.LLMError("Ollama 접속 실패 (http://127.0.0.1:11434): Connection refused")
            return base(messages, **kwargs)
        with mock.patch.object(self.llm, "chat_result", side_effect=broken):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["quality_status"], "partial")
        t1 = next(a for a in rec["topics"] if a["topic"]["id"] == "T1")
        self.assertEqual(t1["error"], "llm_error")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(base.calls[-1]["stage"], "final")

    def test_small_context_fails_without_generation(self):
        with mock.patch.object(self.p.CFG, "num_ctx", 2048):
            rec = self.build()
        self.assertFalse(rec["ok"])
        self.assertFalse(self.model.calls)

    def test_existing_lock_is_not_removed(self):
        path = self.data / "briefings" / "runs" / "demo.lock"
        path.parent.mkdir(parents=True)
        path.write_text("owned by another run")
        rec = self.build()
        self.assertEqual(rec["reason"], "briefing_busy")
        self.assertTrue(path.exists())

    # ── lock 복구 (2026-09-09). pid 판정은 POSIX 전용이라 여기서는 _pid_alive·_boot_id 를 가짜로 둔다 ──
    def _lock(self, **info):
        path = self.p.lock_path("demo")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 4242, "run_id": "old-run", "boot_id": "boot-A", **info}), encoding="utf-8")
        return path

    def test_dead_owner_lock_is_reclaimed_and_status_marked_interrupted(self):
        path = self._lock()
        self.p.atomic_json(self.p.status_path("demo"), {"project_id": "demo", "run_id": "old-run", "state": "running", "stage": "topic"})
        with mock.patch.object(self.p, "_boot_id", return_value="boot-A"), mock.patch.object(self.p, "_pid_alive", return_value=False):
            self.assertFalse(self.p.lock_is_busy("demo"))                 # start_background 가 묻는 길
            st = self.briefing.generation_status("demo")
            self.assertEqual((st["state"], st["stage"], st["run_id"]), ("failed", "interrupted", "old-run"))
            self.assertFalse(path.exists())
            rec = self.build()                                             # 새 run 은 그대로 진행
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(self.briefing.generation_status("demo")["state"], "ready")
        self.assertEqual(list(path.parent.glob("demo.lock*")), [])        # 자기 lock 은 지웠고 stale 사본도 없다

    def test_live_owner_lock_stays_busy(self):
        path = self._lock()
        with mock.patch.object(self.p, "_boot_id", return_value="boot-A"), mock.patch.object(self.p, "_pid_alive", return_value=True):
            rec = self.build()
            self.assertTrue(self.p.lock_is_busy("demo"))
        self.assertEqual(rec["reason"], "briefing_busy")
        self.assertIn("pid 4242", rec["message"])
        self.assertTrue(path.exists())
        self.assertFalse(self.model.calls)

    def test_reboot_makes_lock_stale_even_if_pid_looks_alive(self):
        self._lock(boot_id="boot-A")
        with mock.patch.object(self.p, "_boot_id", return_value="boot-B"), mock.patch.object(self.p, "_pid_alive", return_value=True):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)

    def test_unknown_pid_state_keeps_busy(self):
        # Windows 처럼 판정할 수 없으면(None) 손대지 않는다
        self._lock()
        with mock.patch.object(self.p, "_boot_id", return_value=None), mock.patch.object(self.p, "_pid_alive", return_value=None):
            rec = self.build()
        self.assertEqual(rec["reason"], "briefing_busy")

    def test_release_only_own_lock(self):
        # 생성 도중 다른 쪽이 내 lock 을 stale 로 치우고 새로 잡았다면, 끝날 때 그 lock 을 지우면 안 된다
        original = self.p.Pipeline.final
        def final(p):
            result = original(p)
            self.p.lock_path("demo").write_text(json.dumps({"pid": 1, "run_id": "someone-else", "boot_id": None}), encoding="utf-8")
            return result
        with mock.patch.object(self.p.Pipeline, "final", final):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(self.p._read_lock(self.p.lock_path("demo"))["run_id"], "someone-else")

    def test_interrupt_marks_status_and_frees_lock(self):
        with mock.patch.object(self.p.Pipeline, "execute", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.build()
        st = self.briefing.generation_status("demo")
        self.assertEqual((st["state"], st["stage"]), ("failed", "interrupted"))
        self.assertFalse(self.p.lock_path("demo").exists())

    def test_startup_recovery_handles_locks_and_orphan_running_status(self):
        # 죽은 소유자의 lock → 치움 / 살아 있는 소유자의 lock → 그대로 / lock 없는 running status → interrupted
        dead = self._lock(run_id="dead-run")
        live = self.p.lock_path("other")
        live.write_text(json.dumps({"pid": 7, "run_id": "live-run", "boot_id": "boot-A"}), encoding="utf-8")
        self.p.atomic_json(self.p.status_path("orphan"), {"project_id": "orphan", "run_id": "o1", "state": "running", "stage": "plan"})
        self.p.atomic_json(self.p.status_path("other"), {"project_id": "other", "run_id": "live-run", "state": "running", "stage": "plan"})
        alive = {4242: False, 7: True}
        with mock.patch.object(self.p, "_boot_id", return_value="boot-A"), \
             mock.patch.object(self.p, "_pid_alive", side_effect=lambda pid: alive.get(pid)):
            out = self.p.recover_stale()
        self.assertEqual(out, {"locks_cleared": ["demo"], "status_fixed": ["orphan"]})
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())
        self.assertEqual(self.briefing.generation_status("orphan")["stage"], "interrupted")
        self.assertEqual(self.briefing.generation_status("other")["state"], "running")

    def test_structured_llm_transport_preserves_policy(self):
        # Exercise real new transport, not a fake generation.
        import importlib.util
        source = Path(self.llm.__file__)
        spec = importlib.util.spec_from_file_location("vss._test_llm_result", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        response = io.BytesIO(json.dumps({"message": {"content": "{}"}, "done_reason": "stop",
                                          "prompt_eval_count": 20, "eval_count": 2}).encode())
        with mock.patch.object(module, "_request", return_value=response) as request:
            result = module.chat_result([{"role": "user", "content": "test"}], model="test:latest", response_format="json")
        payload = request.call_args.args[0]
        self.assertEqual(payload["model"], "test:latest")
        self.assertEqual(payload["keep_alive"], -1)
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertEqual(result["stats"]["prompt_eval_count"], 20)


if __name__ == "__main__":
    unittest.main()
