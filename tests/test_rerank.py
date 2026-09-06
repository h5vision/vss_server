"""휴리스틱 재정렬 — 순서만 바꾸고, 켜지는 조건은 인덱스 세대다."""

from __future__ import annotations

import unittest

from vss import rerank
from vss.config import Config

DEMOTE = Config.__dataclass_fields__["demote_globs"].default_factory()


def _h(path: str, i: int) -> dict:
    return {"_id": f"{path}#{i}", "path": path, "score": round(1.0 - i * 0.01, 2)}


def _paths(hits):
    return [h["path"] for h in hits]


class ReorderTest(unittest.TestCase):
    def test_파일당_상한을_넘는_청크는_뒤로_가고_아무것도_버리지_않는다(self):
        hits = [_h("a.py", 0), _h("a.py", 1), _h("a.py", 2), _h("b.py", 3), _h("a.py", 4)]
        out = rerank.reorder(hits, per_file_cap=2, demote_spec="")
        self.assertEqual(["a.py", "a.py", "b.py", "a.py", "a.py"], _paths(out))
        self.assertEqual(sorted(h["_id"] for h in hits), sorted(h["_id"] for h in out))
        self.assertEqual({h["_id"]: h["score"] for h in hits}, {h["_id"]: h["score"] for h in out})   # 점수는 손대지 않는다
        self.assertEqual(_paths(hits), _paths(rerank.reorder(hits, per_file_cap=0, demote_spec="")))  # 0 = 무제한

    def test_테스트_경로는_원본_코드_뒤로_가고_안에서는_원래_순서다(self):
        hits = [_h("tests/test_cli.py", 0), _h("src/cli.py", 1), _h("tests/test_utils.py", 2), _h("src/discover.py", 3)]
        out = rerank.reorder(hits, per_file_cap=0, demote_spec=DEMOTE)
        self.assertEqual(["src/cli.py", "src/discover.py", "tests/test_cli.py", "tests/test_utils.py"], _paths(out))

    def test_fastapi_cli_q040_모양(self):
        # 9/4 run: top-5 = test_cli ×3 · cli.py · test_cli → 정답 cli.py 가 4위였다
        hits = [_h("tests/test_cli.py", 0), _h("tests/test_cli.py", 1), _h("tests/test_cli.py", 2),
                _h("src/fastapi_cli/cli.py", 3), _h("tests/test_cli.py", 4)]
        out = rerank.reorder(hits, per_file_cap=2, demote_spec=DEMOTE)
        self.assertEqual("src/fastapi_cli/cli.py", out[0]["path"])
        self.assertEqual(5, len(out))

    def test_상한을_넘은_원본_코드는_테스트보다_앞이다(self):
        hits = [_h("src/a.py", 0), _h("src/a.py", 1), _h("tests/test_a.py", 2), _h("src/a.py", 3)]
        out = rerank.reorder(hits, per_file_cap=2, demote_spec=DEMOTE)
        self.assertEqual(["src/a.py", "src/a.py", "src/a.py", "tests/test_a.py"], _paths(out))

    def test_기본_감점_glob_이_잡는_것과_안_잡는_것(self):
        demoted = ["tests/test_cli.py", "test/Foo.java", "src/tests/x.py", "backend/test_x.py",
                   "pkg/x_test.go", "web/app.test.ts", "web/app.spec.js"]
        kept = ["src/cli.py", "src/latest_data.py", "backend/contest.py", "README.md", "src/testing_tools.py"]
        for p in demoted:
            self.assertEqual(["k", p], _paths(rerank.reorder([_h(p, 0), _h("k", 1)], per_file_cap=0, demote_spec=DEMOTE)), p)
        for p in kept:
            self.assertEqual([p, "k"], _paths(rerank.reorder([_h(p, 0), _h("k", 1)], per_file_cap=0, demote_spec=DEMOTE)), p)

    def test_빈_감점_spec_은_아무것도_안_바꾼다(self):
        hits = [_h("tests/test_cli.py", 0), _h("src/cli.py", 1)]
        self.assertEqual(_paths(hits), _paths(rerank.reorder(hits, per_file_cap=0, demote_spec="")))


class EnabledTest(unittest.TestCase):
    def test_auto_는_ast_v3_이상에서만_켜진다(self):
        self.assertTrue(rerank.enabled("auto", "ast-v3"))
        self.assertFalse(rerank.enabled("auto", "ast-v2"))
        self.assertFalse(rerank.enabled("auto", "ast-v1"))
        self.assertFalse(rerank.enabled("auto", "line-window-v1"))
        self.assertFalse(rerank.enabled("auto", None))

    def test_on_off_는_세대와_무관하다(self):
        self.assertTrue(rerank.enabled("on", "ast-v1"))
        self.assertFalse(rerank.enabled("off", "ast-v3"))
        self.assertTrue(rerank.enabled("ON", "line-window-v1"))


if __name__ == "__main__":
    unittest.main()
