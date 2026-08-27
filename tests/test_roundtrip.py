"""
가짜 임베더·LLM 으로 인덱싱 → 검색 → 채팅 → finalize → 브리핑 → 평가까지 한 바퀴.
저장소는 chroma 를 기본으로 돌리고, VSS_TEST_PG=1 이면 pgvector 도 같은 검사를 통과해야 합니다.

    python -m unittest tests.test_roundtrip -v
    VSS_TEST_PG=1 python -m unittest tests.test_roundtrip -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import fakes  # noqa: E402

CORPUS = ROOT / "tests" / "corpus"


def _make_corpus(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "README.md").write_text("# demo\n\n결제 서비스 데모 프로젝트입니다.\n", encoding="utf-8")
    (root / "src" / "app.py").write_text(
        '"""앱 진입점."""\nfrom fastapi import FastAPI\nfrom src.payment import PaymentService\n\napp = FastAPI()\n\n\n'
        '@app.post("/pay")\ndef pay(req):\n    """결제 요청을 처리한다."""\n    return PaymentService().process(req)\n\n\n'
        'if __name__ == "__main__":\n    print("run")\n', encoding="utf-8")
    (root / "src" / "payment.py").write_text(
        'DEFAULT_RETRY = 3\n\n\nclass PaymentService:\n    """결제 처리 서비스."""\n\n    def process(self, req):\n'
        '        """결제(payment) 요청을 검증하고 게이트웨이에 전송한다."""\n        self._validate(req)\n'
        '        return self._gateway.submit(req, retries=DEFAULT_RETRY)\n\n    def _validate(self, req):\n'
        '        if not req:\n            raise ValueError("empty request")\n', encoding="utf-8")
    (root / "docs" / "conventions.md").write_text(
        "# 컨벤션\n\n## 에러 처리 규칙\n\n모든 예외는 도메인 예외로 감싸서 던진다. 로그에는 request_id 를 남긴다.\n\n"
        "```python\n# 이것은 헤딩이 아니다\nraise DomainError()\n```\n\n## 커밋 규칙\n\n커밋 제목은 50자 이내.\n",
        encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "junk.json").write_text('{"x": 1}', encoding="utf-8")


class RoundTrip(unittest.TestCase):
    store_kind = os.environ.get("VSS_TEST_STORE", "chroma")

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vss-test-"))
        cls.repo = cls.tmp / "repo"
        _make_corpus(cls.repo)
        os.environ["VSS_DATA_DIR"] = str(cls.tmp / "data")
        os.environ["VSS_STORE"] = cls.store_kind
        os.environ["VSS_PG_SCHEMA"] = "rag_test"
        for m in list(sys.modules):
            if m.startswith("vss"):
                del sys.modules[m]
        import vss.config as config  # noqa
        cls.config = config
        import vss.embedder as embedder
        import vss.llm as llm
        cls.fake_llm = fakes.FakeLLM()
        cls.patches = [
            mock.patch.object(embedder, "embed_many", fakes.fake_embed_many),
            mock.patch.object(embedder, "embed_one", fakes.fake_embed_one),
            mock.patch.object(llm, "chat", cls.fake_llm.chat),
            mock.patch.object(llm, "chat_stream", cls.fake_llm.chat_stream),
        ]
        for p in cls.patches:
            p.start()
        # search/indexer 가 embed_one/embed_many 를 from-import 하므로 그쪽도 교체
        import vss.search as search_mod
        import vss.indexer as indexer
        import vss.eval.runner as runner
        search_mod.embed_one = fakes.fake_embed_one
        indexer.embed_many = fakes.fake_embed_many
        runner.embed_one = fakes.fake_embed_one
        from vss.store import get_store
        cls.store = get_store()
        if cls.store_kind == "pgvector":
            for pid in ("demo", "demo-lines"):
                cls.store.drop(pid)

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_index_and_query(self):
        from vss import indexer
        r = indexer.start_index(str(self.repo), "demo", blocking=True,
                                profile={"use_bm25": True, "context_header": True, "chunker": "ast-v1"},
                                on_done=None, store=self.store)
        self.assertTrue(r["accepted"])
        self.assertEqual(r["state"], "done")
        self.assertIn("demo", self.store.projects())
        info = self.store.project_info("demo")
        self.assertGreaterEqual(info["chunks"], 3)
        self.assertTrue(info["fingerprint"]["use_bm25"])
        paths = {h["path"] for h in self.store.iter_chunks("demo")}
        self.assertNotIn("data/junk.json", paths)         # data/ 제외
        self.assertIn("src/payment.py", paths)
        self.assertEqual(self.store.incomplete(), [])

    def test_02_search_and_threshold(self):
        from vss import search as search_mod
        r = search_mod.search("결제 payment process 요청 검증", "demo", store=self.store, threshold=0.05)
        self.assertTrue(r["has_evidence"])
        self.assertEqual(r["contexts"][0]["path"], "src/payment.py")
        self.assertTrue(r["bm25_active"])
        self.assertGreaterEqual(r["top_score"], r["threshold"])
        # top_score 는 pool 최대 벡터 점수: 판정과 항상 같은 방향
        r2 = search_mod.search("zzz qqq 무관한 질문", "demo", store=self.store, threshold=0.99)
        self.assertFalse(r2["has_evidence"])
        self.assertEqual(r2["reason"], "below_threshold")

    def test_03_chat_stream_and_finalize(self):
        from vss import chat
        events = list(chat.run_chat({"project_id": "demo", "message": "결제 payment process 는 어디서?",
                                     "threshold": 0.05, "stream": True}))
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "meta")
        self.assertIn("delta", kinds)
        self.assertEqual(kinds[-1], "done")
        done = events[-1]["data"]
        self.assertFalse(done["no_evidence"])
        self.assertEqual(done["cited"], [1, 2])
        self.assertEqual([r["n"] for r in done["references"]], [1, 2])   # n 재부여 없음
        self.assertEqual(done["metadata"]["rag_provider"], "vss")
        self.assertIsNotNone(done["metadata"]["timing"]["ttft_ms"])
        self.assertTrue(done["source"] and "file" in done["source"][0])   # P 호환 형태
        code, payload = chat.collect({"project_id": "demo", "message": "zzz qqq", "threshold": 0.99})
        self.assertEqual(code, 200)
        self.assertTrue(payload["no_evidence"])
        self.assertEqual(payload["answer"], "NO_EVIDENCE")
        code, payload = chat.collect({"project_id": "nope", "message": "x"})
        self.assertEqual(code, 404)
        code, payload = chat.collect({"message": "설명해줘", "rag": False})
        self.assertEqual(code, 200)
        self.assertEqual(payload["metadata"]["rag_provider"], "none")

    def test_04_prompt_format(self):
        from vss import prompt
        ctx = [{"path": "a.py", "type": "code", "line_start": 1, "line_end": 3, "text": "x"},
               {"path": "d.md", "type": "doc", "section": "규칙", "text": "y"},
               {"path": "z.txt", "type": "doc", "text": "w"}]
        msgs = prompt.render_prompt("왜?", ctx, selected_code="print(1)")
        u = msgs[1]["content"]
        self.assertIn("[1] a.py lines 1-3", u)
        self.assertIn("[2] d.md #규칙", u)
        self.assertIn("[3] z.txt", u)
        self.assertIn("사용자가 선택한 코드", u)
        self.assertTrue(u.endswith("질문:\n왜?"))
        self.assertIn("검색된 프로젝트 문서가 없습니다", prompt.render_prompt("q", [])[1]["content"])
        f = prompt.finalize("답 [2].", ctx)
        self.assertEqual([r["n"] for r in f["references"]], [2])
        self.assertTrue(prompt.is_no_evidence("설명:\nNO_EVIDENCE"))
        self.assertFalse(prompt.is_no_evidence("근거는 [1] 입니다"))

    def test_05_briefing(self):
        from vss import briefing
        rec = briefing.build(str(self.repo), "demo", model="fake", commit="abc")
        self.assertTrue(rec["ok"])
        md = rec["briefing"]
        for h in ("## 이 프로젝트는", "## 문서 요약", "## 진입점", "## 진입점별 함수 목록", "## 기능 목록"):
            self.assertIn(h, md)
        self.assertIn("src/app.py", md)
        self.assertIn("/pay", md)                       # 라우트 표
        self.assertIn("def pay(req)", md)               # 함수 헤더
        self.assertTrue(briefing.md_path("demo").exists())
        self.assertIsNotNone(briefing.load("demo"))

    def test_06_eval_runner(self):
        from vss.eval import runner
        suite = self.tmp / "suite.jsonl"
        suite.write_text("\n".join(json.dumps(q, ensure_ascii=False) for q in [
            {"id": "q1", "question": "결제 payment process 요청 검증", "answerable": True,
             "gold": [{"path": "src/payment.py", "symbol": "process"}], "tags": ["semantic", "korean"]},
            {"id": "q2", "question": "에러 처리 규칙 예외 도메인", "answerable": True,
             "gold": [{"path": "docs/conventions.md"}], "tags": ["semantic", "korean"]},
            {"id": "q3", "question": "zzz qqq 무관", "answerable": False, "gold": [], "tags": ["no_evidence"]},
        ]) + "\n", encoding="utf-8")
        matrix = self.tmp / "m.json"
        matrix.write_text(json.dumps({
            "schema_version": "2.0", "name": "demo-test", "repository": str(self.repo), "suite": "suite.jsonl",
            "search_profiles": [{"name": "vector", "use_bm25": False, "pool": 10, "top_k": 4, "threshold": 0.05},
                                {"name": "hybrid", "use_bm25": True, "pool": 10, "top_k": 4, "threshold": 0.05}],
            "cells": [{"project_id": "demo", "label": "ast", "search_profile": "vector", "modes": ["retrieval", "pipeline"]},
                      {"project_id": "demo", "label": "ast", "search_profile": "hybrid", "modes": ["retrieval", "pipeline"]}],
        }, ensure_ascii=False), encoding="utf-8")
        r = runner.run_matrix(matrix, store=self.store, note="unit")
        self.assertEqual(len(r["cells"]), 2)
        s = r["cells"][0]["modes"]["retrieval"]["summary"]
        self.assertEqual(s["answerable"], 2)
        self.assertGreaterEqual(s["hit@3"], 0.5)
        self.assertTrue(Path(r["report"]).exists())
        self.assertTrue(any(row["cell"] == "ast" for row in runner.list_runs()))

    def test_07_reindex_atomic_and_repair(self):
        from vss import indexer
        before = self.store.count("demo")
        bad = mock.patch.object(indexer, "embed_many", side_effect=RuntimeError("tunnel down"))
        with bad:
            with self.assertRaises(RuntimeError):
                indexer.start_index(str(self.repo), "demo", blocking=True, force=True, store=self.store,
                                    profile={"use_bm25": True})
        self.assertEqual(self.store.count("demo"), before)          # 기존 인덱스 무사
        self.assertTrue(self.store.incomplete())                    # 실패 흔적은 남음
        items = indexer.repair(store=self.store, apply=True)
        self.assertTrue(items)
        self.assertEqual(self.store.incomplete(), [])
        r = indexer.start_index(str(self.repo), "demo-lines", blocking=True, store=self.store,
                                profile={"use_bm25": False, "chunker": "line-window-v1", "context_header": False})
        self.assertEqual(r["state"], "done")
        self.assertFalse(self.store.project_info("demo-lines")["fingerprint"]["use_bm25"])

    def test_08_matrix_repository_expands_home(self):
        """matrix 의 repository 는 `~`·`$VAR` 를 푼다 (계정·머신이 달라도 같은 matrix 가 통해야 한다)."""
        from vss.eval import runner
        from vss.eval.suite import canonical_hash
        home = self.tmp / "home"
        (home / "repos" / "demo").mkdir(parents=True)

        m = {"name": "x", "repository": "$VSS_TEST_HOME/repos/demo"}
        with mock.patch.dict(os.environ, {"VSS_TEST_HOME": str(home)}):
            self.assertEqual(runner.repo_dir(m), home / "repos" / "demo")

        m = {"name": "x", "repository": "~/repos/demo"}
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            self.assertEqual(runner.repo_dir(m), home / "repos" / "demo")
            # 푼 값을 matrix 에 되쓰면 matrix_hash 가 머신마다 달라져 run 비교가 깨진다
            self.assertEqual(m["repository"], "~/repos/demo")
            self.assertEqual(canonical_hash(m), canonical_hash({"name": "x", "repository": "~/repos/demo"}))

        self.assertIsNone(runner.repo_dir({"name": "x", "repository": "~/repos/없는것"}))
        self.assertIsNone(runner.repo_dir({"name": "x"}))

    def test_09_project_alias_query_only(self):
        """프론트는 레포명만 보내고 서버가 인덱스를 고른다. 인덱싱·평가는 별칭을 타지 않는다."""
        from vss import chat
        from vss.config import CFG, alias_map, resolve_project_id

        with mock.patch.object(CFG, "project_aliases", "api_test=demo, rag_lab = demo-lines"):
            self.assertEqual(resolve_project_id("api_test"), "demo")
            self.assertEqual(resolve_project_id("API-TEST"), "demo")     # 하이픈·대소문자 동일시
            self.assertEqual(resolve_project_id("rag_lab"), "demo-lines")
            self.assertEqual(resolve_project_id("demo"), "demo")         # 별칭에 없으면 그대로
            self.assertIsNone(resolve_project_id(None))
            self.assertEqual(alias_map(), {"api-test": "demo", "rag-lab": "demo-lines"})

            body = {"project_id": "api_test", "message": "결제 처리 함수", "stream": True}
            events = {e["event"]: e["data"] for e in chat.run_chat(body)}
            self.assertNotIn("error", events)
            # 받은 이름은 그대로 돌려주고, 실제로 검색한 인덱스는 index_id 로 알린다
            self.assertEqual(events["meta"]["project_id"], "api_test")
            self.assertEqual(events["meta"]["index_id"], "demo")
            self.assertEqual(events["done"]["metadata"]["index_id"], "demo")

        # 별칭이 없으면 그 이름의 인덱스를 찾다가 기존 예외 — 조용한 폴백 없음
        events = {e["event"]: e["data"] for e in chat.run_chat({"project_id": "api_test", "message": "x"})}
        self.assertEqual(events["error"]["code"], "project_not_found")

    def test_10_index_note_rides_in_store_meta(self):
        """--note 는 별도 파일이 아니라 인덱스 자신의 meta 에 저장되고 승격까지 살아남는다."""
        from vss import indexer
        r = indexer.start_index(str(self.repo), "demo-noted", blocking=True, store=self.store,
                                extra_meta={"note": "8/27 기준선 · ast+header"})
        self.assertEqual(r["state"], "done")
        self.assertEqual(self.store.project_info("demo-noted")["note"], "8/27 기준선 · ast+header")
        row = next(x for x in indexer.list_projects(self.store) if x["project_id"] == "demo-noted")
        self.assertEqual(row["note"], "8/27 기준선 · ast+header")

    def test_11_threshold_sweep_counts(self):
        """sweep 은 저장된 top_score 를 다시 셀 뿐이다 — 검색도 임베딩도 하지 않는다."""
        from vss.eval import sweep
        rows = [                                     # 답있음 3 · 답없음 2
            {"answerable": True,  "top_score": 0.70, "rank": 1},
            {"answerable": True,  "top_score": 0.55, "rank": 5},     # 통과하지만 top-3 밖
            {"answerable": True,  "top_score": 0.45, "rank": 2},     # 0.54 에서 막힌다
            {"answerable": False, "top_score": 0.60, "rank": None},  # 0.54 를 통과하는 hard negative
            {"answerable": False, "top_score": 0.30, "rank": None},
        ]
        c = sweep.confusion(rows, 0.54)
        self.assertEqual((c["tp"], c["fn"], c["fp"], c["tn"]), (2, 1, 1, 1))
        self.assertAlmostEqual(c["gate_recall"], 2 / 3)
        self.assertAlmostEqual(c["no_ev_recall"], 1 / 2)
        self.assertAlmostEqual(c["hit@3"], 1 / 3)                    # 통과 + rank<=3 은 첫 문항뿐
        self.assertAlmostEqual(c["bal_acc"], (2 / 3 + 1 / 2) / 2)

        low = sweep.confusion(rows, 0.40)                            # 낮추면 다 통과 → 근거 없음을 못 막는다
        self.assertEqual((low["tp"], low["fn"]), (3, 0))
        self.assertAlmostEqual(low["no_ev_recall"], 1 / 2)

        table = sweep.sweep_cell(rows, sweep.grid(0.40, 0.70, 0.05))
        b = sweep.best(table)
        self.assertGreaterEqual(b["bal_acc"], max(r["bal_acc"] for r in table))
        # 답없음 문항이 없으면 bal_acc 를 계산할 수 없다 (조용히 0 으로 만들지 않는다)
        only_pos = sweep.confusion([r for r in rows if r["answerable"]], 0.54)
        self.assertIsNone(only_pos["no_ev_recall"])
        self.assertIsNone(only_pos["bal_acc"])


if __name__ == "__main__":
    unittest.main()
