from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


class FakeBriefingModel:
    def __init__(self):
        self.calls = []
        self.fail_final = False
        self.wide = False          # True 면 주제 분석마다 긴 조건 10개 → final 입력이 예산을 넘는다

    def __call__(self, messages, **kwargs):
        req = json.loads(messages[-1]["content"])
        self.calls.append(req)
        stage, body = req["stage"], req["input"]
        ids = body.get("evidence_ids") or [e["id"] for e in body.get("evidence", [])]
        if stage == "plan":
            data = {"topics": [{"title": "주문 처리", "questions": ["주문을 어떻게 저장하는가?"],
                                 "paths": ["app.py"], "queries": ["create_order"], "representative": True}]}
        elif stage == "final":
            if self.fail_final:
                return {"content": "{bad", "done_reason": "length", "stats": {"eval_count": 2500}}
            c = {"text": "분석 결과를 종합한 주문 서버입니다.", "evidence_ids": ids[:1]}
            data = {k: [c] for k in ("overview", "features", "flow", "reading")}
            data["unknowns"] = []
        else:
            c = {"text": "주문을 저장합니다.", "evidence_ids": ids[:1]}
            conditions = [{"text": "요청 검증 후에만 저장합니다.", "evidence_ids": ids[:1]}]
            if self.wide and stage == "topic":
                conditions = [{"text": f"조건 {i} " + "가" * 100, "evidence_ids": ids[:1]} for i in range(10)]
            data = {"claims": [c], "conditions": conditions,
                    "flow": [c], "reading": [c], "compact": [c], "unknowns": [], "followup_queries": []}
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
        for file in cache.glob("*.json"):
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

    def test_source_mutation_prevents_publication(self):
        original = self.p.Pipeline.final
        def final(p):
            result = original(p)
            (self.root / "storage.py").write_text("# changed\n", encoding="utf-8")
            return result
        with mock.patch.object(self.p.Pipeline, "final", final):
            rec = self.build()
        self.assertEqual(rec["reason"], "source_changed")
        self.assertIsNone(self.briefing.load("demo"))

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
        with mock.patch.object(self.llm, "chat_result", side_effect=flaky):
            rec = self.build()
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["quality_status"], "partial")
        t1 = next(a for a in rec["topics"] if a["topic"]["id"] == "T1")
        self.assertEqual(t1["error"], "llm_timeout")
        # 재시도도, 보완 라운드도 없이 한 번만 기다린다
        self.assertEqual(sum(1 for c in base.calls if c["stage"] == "topic" and c["input"]["topic"]["id"] == "T1"), 1)
        self.assertEqual(base.calls[-1]["stage"], "final")
        self.assertTrue(all(m["timeout_s"] == self.p.CFG.chat_timeout * 4 for m in rec["metrics"] if "timeout_s" in m))

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
