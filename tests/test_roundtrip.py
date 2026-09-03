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
        # 측정 조건은 응답에 전부 남아야 한다 (불변 조건 6) — rrf_k 를 바꿔 가며 재도 어느 설정인지 추적된다
        self.assertEqual(r["search_profile"]["rrf_k"], 60)
        r_k = search_mod.search("결제 payment", "demo", store=self.store, threshold=0.05,
                                search_profile={"use_bm25": True, "rrf_k": 10})
        self.assertEqual(r_k["search_profile"]["rrf_k"], 10)
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
        # prompt_ms 는 프롬프트 조립만, pre_llm_ms 는 요청~LLM직전 누적(embed+search 포함). 둘 다 최종 응답에 온다
        t = payload["metadata"]["timing"]
        self.assertIn("prompt_ms", t)
        self.assertLessEqual(t["prompt_ms"], t["pre_llm_ms"])
        code, payload = chat.collect({"project_id": "demo", "message": "결제 payment process",
                                      "threshold": 0.05})
        t = payload["metadata"]["timing"]
        self.assertLessEqual(t["embed_ms"] + t["search_ms"], t["pre_llm_ms"])   # 누적값이 둘을 포함한다
        self.assertLessEqual(t["prompt_ms"], t["pre_llm_ms"])                   # 조립만 재므로 더 작다
        # 근거가 없으면 프롬프트를 조립하지 않으므로 prompt_ms 자체가 없다 (0 이 아니라 부재)
        _, no_ev = chat.collect({"project_id": "demo", "message": "zzz qqq", "threshold": 0.99})
        self.assertNotIn("prompt_ms", no_ev["metadata"]["timing"])
        self.assertIn("pre_llm_ms", no_ev["metadata"]["timing"])

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
        # 인덱싱 시점에 코퍼스가 미커밋이었는지가 목록 한 줄에 보여야 한다 (git 레포가 아니면 None)
        self.assertIn("dirty", row)
        self.assertIn("dirty", indexer.status("demo-noted", store=self.store)["index"])

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

    def test_12_citation_range_guard(self):
        """범위 밖 인용 번호가 출처를 통째로 지우지 못한다. n 번호는 원래 값 유지 (CHARTER 4)."""
        from vss import prompt
        ctx = [{"path": f"src/f{i}.py", "type": "code", "line_start": i * 10, "line_end": i * 10 + 5,
                "score": 0.7, "text": "x"} for i in range(1, 5)]

        # 모델이 없는 번호를 인용해도 인용 0건과 같게 취급한다 (출처 전멸 금지)
        f = prompt.finalize("설명입니다 [7].", ctx)
        self.assertEqual([r["n"] for r in f["references"]], [1, 2, 3, 4])
        self.assertEqual(f["cited"], [])

        # 진짜 인용 없이 코드 표기(items[0])만 있는 답변도 마찬가지다
        f = prompt.finalize("결과는 items[0] 에 담깁니다.", ctx)
        self.assertEqual(len(f["reference_files"]), 4)
        self.assertEqual(f["cited"], [])

        # 안팎이 섞이면 범위 안의 것만 남기고 번호는 재부여하지 않는다
        f = prompt.finalize("근거는 [2] 와 [9] 입니다.", ctx)
        self.assertEqual([r["n"] for r in f["references"]], [2])
        self.assertEqual(f["cited"], [2])

        # 정상 인용의 기존 동작은 바뀌지 않는다
        f = prompt.finalize("A [1]. B [3].", ctx)
        self.assertEqual([r["n"] for r in f["references"]], [1, 3])
        self.assertEqual(f["cited"], [1, 3])

    def test_13_enclosing_survives_store(self):
        """청커가 만든 enclosing 이 저장을 건너 검색 결과까지 온다 (parent-child 의 재료).

        chunker 는 예전부터 이 값을 만들었지만 저장 계층이 담지 않아 질의 쪽에서 쓸 수 없었다.
        ast-v2 인덱스가 아직 없는 지금 넣어야 재인덱싱이 한 번으로 끝난다.
        """
        chunks = {c["path"]: c for c in self.store.iter_chunks("demo")}
        self.assertIn("src/payment.py", chunks)

        # 모든 hit 에 키가 있고 항상 list 다 (없으면 빈 list — None 이 아니다)
        for c in chunks.values():
            self.assertIsInstance(c["enclosing"], list)

        # 메서드 청크는 감싼 클래스를 알고, 사슬의 마지막은 자기 자신이다
        methods = [c for c in self.store.iter_chunks("demo")
                   if (c.get("symbol") or "").startswith("PaymentService.")]
        self.assertTrue(methods, "PaymentService 메서드 청크가 없다")
        for m in methods:
            self.assertEqual(m["enclosing"][0], "class PaymentService")
            self.assertEqual(m["enclosing"][-1], "def " + m["symbol"].split(".")[-1])

        # 벡터 질의 경로도 같은 값을 싣는다 (iter_chunks 만이 아니라)
        hits = self.store.query("demo", fakes.fake_embed_one("결제 요청 검증"), 5)
        self.assertTrue(all(isinstance(h["enclosing"], list) for h in hits))
        self.assertTrue(any(h["enclosing"] for h in hits), "질의 결과에 enclosing 이 하나도 없다")

    def test_16_kind_survives_store(self):
        """청커가 만든 kind 가 저장을 건너 검색 결과까지 온다 (symbol/metadata filter 의 재료).

        enclosing 과 같은 구멍이었다 — 청커는 만드는데 store 가 안 담아 질의 쪽에서 쓸 수 없었다.
        """
        from vss import indexer
        # ast-v1 은 클래스 선언 청크를 만들지 않으므로 kind 3종을 보려면 ast-v2 인덱스가 필요하다
        indexer.start_index(str(self.repo), "kindcheck--ast-v2", blocking=True, on_done=None,
                            store=self.store,
                            profile={"use_bm25": False, "context_header": False, "chunker": "ast-v2",
                                     "min_chunk_chars": 1})     # 짧은 상수(DEFAULT_RETRY)까지 청크로 남긴다
        chunks = list(self.store.iter_chunks("kindcheck--ast-v2"))
        by_symbol = {c.get("symbol"): c for c in chunks}

        # 클래스와 메서드가 서로 다른 kind 로 구분된다
        self.assertEqual(by_symbol["PaymentService"]["kind"], "class")
        self.assertEqual(by_symbol["PaymentService.process"]["kind"], "method")
        self.assertEqual(by_symbol["DEFAULT_RETRY"]["kind"], "assign")

        # 줄 윈도우·문서 청크는 kind 가 없다 — 키는 늘 있고 값만 None 이다
        for c in chunks:
            self.assertIn("kind", c)
        docs = [c for c in chunks if c["type"] == "doc"]
        self.assertTrue(docs)
        self.assertTrue(all(d["kind"] is None for d in docs))

        # 벡터 질의 경로도 같은 값을 싣는다 (iter_chunks 만이 아니라)
        hits = self.store.query("kindcheck--ast-v2", fakes.fake_embed_one("결제 요청 검증"), 5)
        self.assertTrue(any(h["kind"] for h in hits), "질의 결과에 kind 가 하나도 없다")

    def test_14_symbol_boost_reorders_without_moving_threshold(self):
        """심볼 재정렬은 순서만 바꾼다 — top_score 와 has_evidence 는 그대로다 (CHARTER 5)."""
        from vss import search as search_mod
        q = "PaymentService.process 는 무엇을 하나요?"

        off = search_mod.search(q, "demo", top_k=3, store=self.store,
                                search_profile={"use_symbols": False})
        on = search_mod.search(q, "demo", top_k=3, store=self.store,
                               search_profile={"use_symbols": True})

        # 켠 쪽만 심볼을 뽑고, 그 사실이 run 에 남는다 (불변 조건 6)
        self.assertFalse(off["search_profile"]["use_symbols"])
        self.assertTrue(on["search_profile"]["use_symbols"])
        self.assertIn("PaymentService.process", on["search_profile"]["symbol_tokens"])
        self.assertEqual(off["search_profile"]["symbol_tokens"], [])

        # 판정에 쓰는 두 값은 재정렬과 무관해야 한다
        self.assertEqual(on["top_score"], off["top_score"])
        self.assertEqual(on["has_evidence"], off["has_evidence"])

        # 실제로 그 심볼 청크가 pool 맨 앞으로 온다
        self.assertGreater(on["search_profile"]["symbol_matches"], 0)
        self.assertEqual(on["all_hits"][0]["symbol"], "PaymentService.process")

        # 임계값을 통과하는 질의에서는 그게 곧 contexts[0] = 프롬프트의 [1] 이 된다.
        # (가짜 임베더의 점수가 낮아 기본 임계값으로는 통과하지 못하므로 여기서만 낮춘다)
        lifted = search_mod.search(q, "demo", top_k=3, threshold=0.0, store=self.store,
                                   search_profile={"use_symbols": True})
        self.assertEqual(lifted["contexts"][0]["symbol"], "PaymentService.process")

        # 심볼이 없는 질문은 아무것도 바꾸지 않는다 (pool 도 넓히지 않는다)
        plain = search_mod.search("결제는 어떻게 처리되나요", "demo", top_k=3, store=self.store,
                                  search_profile={"use_symbols": True})
        self.assertEqual(plain["search_profile"]["symbol_tokens"], [])
        self.assertEqual(plain["search_profile"]["symbol_matches"], 0)

    def test_18_index_files_and_unindexed(self):
        """GET /projects 가 낼 재료 — 인덱스에 실제로 들어간 파일과, 아직 인덱싱 안 된 레포."""
        from vss import indexer

        files = indexer.index_files("demo", self.store)
        by_path = {f["path"]: f for f in files}
        self.assertIn("src/payment.py", by_path)
        self.assertIn("docs/conventions.md", by_path)
        self.assertNotIn("data/junk.json", by_path)          # 제외 규칙이 먹은 것이 여기서 보인다
        self.assertEqual(by_path["docs/conventions.md"]["type"], "doc")
        self.assertGreaterEqual(by_path["src/payment.py"]["chunks"], 1)
        self.assertGreater(by_path["src/payment.py"]["line_max"], 0)
        self.assertNotIn("symbols", by_path["src/payment.py"])            # 요청해야만 실린다

        with_syms = {f["path"]: f for f in indexer.index_files("demo", self.store, symbols=True)}
        self.assertTrue(any(s.startswith("PaymentService")
                            for s in with_syms["src/payment.py"]["symbols"]))

        # VSS_REPOS_DIR 이 없으면 키 자체를 안 낸다 (지금 동작과 동일)
        with mock.patch.object(self.config.CFG, "repos_dir", ""):
            self.assertIsNone(indexer.unindexed_repos(self.store))

        # 있으면 인덱스가 없는 디렉터리만 나온다
        (self.tmp / "repos" / "demo").mkdir(parents=True)        # 인덱스가 있는 이름 → 빠짐
        (self.tmp / "repos" / "not-indexed").mkdir()
        (self.tmp / "repos" / ".hidden").mkdir()                 # 숨김 → 빠짐
        with mock.patch.object(self.config.CFG, "repos_dir", str(self.tmp / "repos")):
            names = [r["name"] for r in indexer.unindexed_repos(self.store)]
        self.assertEqual(names, ["not-indexed"])

    def test_19_git_log_survives_korean_messages(self):
        """git 출력은 로케일이 아니라 UTF-8 로 읽는다 — 한글 커밋 메시지에서 죽으면 안 된다."""
        import subprocess
        from vss import indexer

        r = self.tmp / "gitrepo"
        r.mkdir()
        (r / "a.txt").write_text("x", encoding="utf-8")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "commit", "-qm", "첫 커밋: 한글 메시지"]):
            if subprocess.run(cmd, cwd=r, env=env, capture_output=True).returncode != 0:
                self.skipTest("git 을 쓸 수 없는 환경")

        log = indexer.git_log(r, 5)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["message"], "첫 커밋: 한글 메시지")
        self.assertEqual(log[0]["sha"], indexer.git_head(r))
        self.assertTrue(log[0]["short"] and log[0]["date"])
        self.assertIs(indexer.git_dirty(r), False)

        # git 레포가 아니면 조용히 빈 목록 (예외로 터지지 않는다)
        self.assertEqual(indexer.git_log(self.tmp / "nosuch", 5), [])

    def test_17_think_flag_only_ships_when_set(self):
        """VSS_THINK 는 값이 있을 때만 payload 에 실린다 — 이 필드를 모르는 Ollama·모델을 깨뜨리지 않는다."""
        from vss import llm
        from vss.config import CFG

        with mock.patch.object(CFG, "think", ""):
            self.assertIsNone(llm.think_flag())
            self.assertNotIn("think", llm._payload(None, [], stream=False, options={}))
        with mock.patch.object(CFG, "think", "0"):
            self.assertFalse(llm.think_flag())
            self.assertIs(llm._payload(None, [], stream=False, options={})["think"], False)
        with mock.patch.object(CFG, "think", "1"):
            self.assertTrue(llm._payload(None, [], stream=True, options={})["think"])
        # options 가 아니라 payload 최상위여야 한다 (Ollama 계약)
        with mock.patch.object(CFG, "think", "0"):
            p = llm._payload(None, [], stream=False, options={"num_ctx": 8192})
            self.assertNotIn("think", p["options"])

    def test_15_repo_name_resolves_to_newest_index(self):
        """프론트는 레포 이름만 보내고 서버가 가장 새 세대 인덱스를 고른다 (별칭 > 정확 이름 > 자동)."""
        from vss import indexer
        for pid, chunker in (("pick--lines", "line-window-v1"), ("pick--ast", "ast-v1"),
                             ("pick--ast-v2", "ast-v2")):
            indexer.start_index(str(self.repo), pid, blocking=True, on_done=None, store=self.store,
                                profile={"use_bm25": False, "context_header": False, "chunker": chunker})

        # 자동: 청커 세대가 가장 새것 (line-window-v1 < ast-v1 < ast-v2)
        self.assertEqual(indexer.resolve_index("pick", self.store), ("pick--ast-v2", "auto"))
        self.assertEqual(indexer.index_candidates("pick", self.store),
                         ["pick--ast-v2", "pick--ast", "pick--lines"])

        # 정확 이름을 보내면 그대로 — 특정 인덱스를 지목하는 길이 막히면 안 된다
        self.assertEqual(indexer.resolve_index("pick--ast", self.store), ("pick--ast", "exact"))

        # 별칭은 자동을 이긴다 (손으로 고정하고 싶을 때)
        with mock.patch.object(self.config.CFG, "project_aliases", "pick=pick--lines"):
            self.assertEqual(indexer.resolve_index("pick", self.store), ("pick--lines", "alias"))

        # 후보가 없으면 받은 그대로 — 비슷한 이름으로 몰래 바꾸지 않는다
        self.assertEqual(indexer.resolve_index("nosuch", self.store), ("nosuch", "none"))

        # 미완성 빌드는 애초에 후보가 아니다 (저장소가 상태의 정본, 불변 조건 2·3)
        self.assertNotIn("pick", [i.get("target") for i in self.store.incomplete()])
        self.assertEqual(indexer.repo_map(self.store)["pick"]["index_id"], "pick--ast-v2")

        # `--` 없이 한 번만 인덱싱한 레포도 목록에서 빠지면 안 된다 (프론트가 못 고른다)
        indexer.start_index(str(self.repo), "vision", blocking=True, on_done=None, store=self.store,
                            profile={"use_bm25": False, "context_header": False, "chunker": "ast-v2"})
        self.assertEqual(indexer.resolve_index("vision", self.store), ("vision", "exact"))
        self.assertEqual(indexer.index_candidates("vision", self.store), ["vision"])
        self.assertEqual(indexer.repo_map(self.store)["vision"]["index_id"], "vision")

        # 프론트 축약 목록은 선택에 필요한 결과만 노출한다. 내부 선택 정보와 저장 메타는
        # repo_map/list_projects 에 남아 있어 최신 인덱스 결정과 운영 조회에 영향을 주지 않는다.
        compact = next(r for r in indexer.repo_list(self.store) if r["name"] == "pick")
        self.assertEqual(compact["index_id"], "pick--ast-v2")
        for field in ("resolved_by", "candidates", "indexed_at", "dirty"):
            self.assertNotIn(field, compact)

        # current: 그 레포 이름으로 물으면 지금 답하는 인덱스만 True. 옛 세대는 False
        by_id = {p["project_id"]: p for p in indexer.list_projects(self.store)}
        self.assertIn("indexed_at", by_id["pick--ast-v2"])
        self.assertIn("dirty", by_id["pick--ast-v2"])
        self.assertTrue(by_id["pick--ast-v2"]["current"])
        self.assertFalse(by_id["pick--ast"]["current"])
        self.assertFalse(by_id["pick--lines"]["current"])
        only = {p["project_id"] for p in indexer.list_projects(self.store, only_current=True)}
        self.assertIn("pick--ast-v2", only)
        self.assertNotIn("pick--ast", only)

    def test_20_querylog_writes_one_row_and_never_breaks_the_answer(self):
        """질의 로그: DSN 이 비면 아무것도 안 하고, 있으면 한 행, 실패해도 예외를 안 낸다.

        노트북에 PostgreSQL 이 없으므로 연결을 가로채 SQL 을 실측한다 (pgvector 때와 같은 방식).
        """
        from vss import querylog
        from vss.config import CFG

        # 1) DSN 이 비면 no-op — psycopg 를 아예 부르지 않는다
        with mock.patch.object(CFG, "querylog_dsn", ""):
            self.assertFalse(querylog.enabled())
            with mock.patch.object(querylog, "_connect",
                                   side_effect=AssertionError("DSN 이 비었는데 연결했다")):
                self.assertFalse(querylog.write({"request_id": "x"}))

        # 2) 컬럼·자리·값 개수가 어긋날 수 없다 (INSERT 가 COLUMNS 하나에서 만들어진다)
        sql = querylog.insert_sql("rag_test")
        self.assertIn("INSERT INTO rag_test.query_log", sql)
        self.assertEqual(sql.count("%s"), len(querylog.COLUMNS))
        self.assertIn("timing", querylog.JSONB_COLUMNS)
        self.assertIn("%s::jsonb", sql)                       # timing 만 jsonb 로 들어간다

        # 3) DSN 이 있으면 DDL 한 번 + INSERT 한 번, 값은 metadata 그대로
        meta = {"request_id": "req1", "project_id": "api_test", "index_id": "api-test--ast",
                "resolved_by": "auto", "model": "qwen2.5-coder:7b", "has_evidence": True,
                "top_score": 0.71, "threshold": 0.54, "reason": "ok",
                "timing": {"total_ms": 1234.5}}
        rec = querylog.from_metadata(meta, question="결제는 어떻게 처리되나요", outcome="answered")
        self.assertEqual(rec["question"], "결제는 어떻게 처리되나요")
        self.assertEqual(rec["outcome"], "answered")
        self.assertIn(rec["outcome"], querylog.OUTCOMES)
        self.assertIsNone(rec["error_code"])

        executed = []

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                executed.append((sql, params))

        querylog._schema_ready = False
        with mock.patch.object(CFG, "querylog_dsn", "postgresql://x/y"), \
                mock.patch.object(CFG, "pg_schema", "rag_test"), \
                mock.patch.object(querylog, "_connect", return_value=FakeConn()):
            self.assertTrue(querylog.enabled())
            self.assertTrue(querylog.write(rec))
        self.assertEqual(len(executed), 2)                    # DDL 1 + INSERT 1
        self.assertIn("CREATE TABLE IF NOT EXISTS rag_test.query_log", executed[0][0])
        ins_sql, values = executed[1]
        self.assertIn("INSERT INTO rag_test.query_log", ins_sql)
        self.assertEqual(len(values), ins_sql.count("%s"))     # 자리 == 값
        by_col = dict(zip(querylog.COLUMNS, values))
        self.assertEqual(by_col["request_id"], "req1")
        self.assertEqual(by_col["index_id"], "api-test--ast")
        self.assertEqual(by_col["top_score"], 0.71)
        self.assertEqual(json.loads(by_col["timing"])["total_ms"], 1234.5)   # jsonb 는 문자열로 넘긴다

        # 4) 연결이 터져도 답변은 살아야 한다 — 예외가 밖으로 나오지 않는다
        querylog._schema_ready = False
        with mock.patch.object(CFG, "querylog_dsn", "postgresql://x/y"), \
                mock.patch.object(querylog, "_connect", side_effect=RuntimeError("DB down")):
            self.assertFalse(querylog.write(rec))

    def test_21_chat_logs_every_exit_except_rag_false(self):
        """run_chat 의 출구 다섯 개가 각각 한 행을 남긴다. rag:false 만 안 남긴다."""
        from vss import chat, querylog

        rows: list[dict] = []

        def ask(body):
            rows.clear()
            with mock.patch.object(querylog, "write", lambda rec: rows.append(rec) or True):
                return chat.collect(body)

        # 1) 답이 나온 경우 — 응답 metadata 와 DB 행이 같은 값이어야 한다
        code, payload = ask({"project_id": "demo", "message": "결제 payment process 는 어디서?",
                             "threshold": 0.05})
        self.assertEqual(code, 200)
        self.assertEqual(len(rows), 1)
        row, meta = rows[0], payload["metadata"]
        self.assertEqual(set(row), set(querylog.COLUMNS))      # from_metadata 와 COLUMNS 가 어긋나면 여기서 걸린다
        self.assertEqual(row["outcome"], "answered")
        self.assertEqual(row["question"], "결제 payment process 는 어디서?")
        self.assertEqual(row["index_id"], "demo")
        self.assertIsNone(row["error_code"])
        for k in ("request_id", "project_id", "index_id", "resolved_by", "model",
                  "has_evidence", "top_score", "threshold", "reason"):
            self.assertEqual(row[k], meta[k], f"{k} 가 응답과 다르다")
        self.assertIsNotNone(row["timing"]["ttft_ms"])

        # 2) 근거 없음 — LLM 을 안 불렀으므로 model 은 없고 reason 이 왜인지 말한다
        _, payload = ask({"project_id": "demo", "message": "zzz qqq", "threshold": 0.99})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "no_evidence")
        self.assertIs(rows[0]["has_evidence"], False)
        self.assertEqual(rows[0]["reason"], "below_threshold")
        self.assertIsNone(rows[0]["model"])
        self.assertEqual(rows[0], querylog.from_metadata(
            payload["metadata"], question="zzz qqq", outcome="no_evidence"))

        # 3) 없는 인덱스 → error 행. 여기는 metadata 가 만들어지기 전이라 error_code 가 유일한 단서다
        code, _ = ask({"project_id": "nope", "message": "x"})
        self.assertEqual(code, 404)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "error")
        self.assertEqual(rows[0]["error_code"], "project_not_found")
        self.assertEqual(rows[0]["question"], "x")

        # 4) 질문이 비었을 때도 남는다 (프론트 결함이 여기서 보인다)
        code, _ = ask({"project_id": "demo", "message": ""})
        self.assertEqual(code, 400)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error_code"], "bad_request")

        # project_id 를 안 보낸 것도 같은 코드로 남는다
        code, _ = ask({"message": "설명해줘"})
        self.assertEqual(code, 400)
        self.assertEqual(rows[0]["error_code"], "bad_request")

        # 5) rag:false 는 한 행도 안 남긴다 (md 결정 2026-09-02)
        code, payload = ask({"message": "설명해줘", "rag": False})
        self.assertEqual(code, 200)
        self.assertEqual(payload["metadata"]["rag_provider"], "none")
        self.assertEqual(rows, [])

        # rag:false 는 message 가 비어도 안 남긴다 (거르는 자리가 한 곳뿐임을 고정)
        ask({"message": "", "rag": False})
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
