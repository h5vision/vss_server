"""
심볼 인식 검색 — 후보 추출 · 색인 · 재정렬.

가장 중요한 검사는 마지막 둘입니다: 재정렬이 **점수를 건드리지 않고**,
`top_score >= threshold ⟺ has_evidence` (CHARTER 5) 를 깨지 않는다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vss import symbols  # noqa: E402


def _chunk(cid: str, symbol: str | None, score: float = 0.5) -> dict:
    return {"_id": cid, "symbol": symbol, "score": score, "text": "x", "path": "a.py"}


class Candidates(unittest.TestCase):
    def test_dotted_and_parent(self):
        got = symbols.candidates("PaymentService.process 는 뭘 하나요?")
        self.assertIn("PaymentService.process", got)
        self.assertIn("PaymentService", got)          # 부모 이름도 후보가 된다

    def test_backtick_wins_even_when_shape_is_odd(self):
        # 밑줄로 시작해 _SNAKE 로는 안 잡히는 이름도 백틱이면 통째로 들어온다
        self.assertIn("_validate", symbols.candidates("`_validate` 함수 설명해줘"))

    def test_call_and_const(self):
        self.assertIn("process", symbols.candidates("process() 호출 흐름이 궁금합니다"))
        self.assertIn("DEFAULT_RETRY", symbols.candidates("DEFAULT_RETRY 기본값은?"))

    def test_plain_korean_and_stopwords_yield_nothing(self):
        self.assertEqual(symbols.candidates("결제는 어떻게 처리되나요"), [])
        self.assertEqual(symbols.candidates("self 와 None 은 무시한다"), [])

    def test_no_duplicates_and_order_kept(self):
        got = symbols.candidates("`get_user` 와 get_user 는 같은 것")
        self.assertEqual(got.count("get_user"), 1)


class Index(unittest.TestCase):
    def setUp(self):
        self.idx = symbols.SymbolIndex([
            _chunk("c1", "PaymentService"),
            _chunk("c2", "PaymentService.process"),
            _chunk("c3", "DEFAULT_RETRY, MAX_WAIT"),      # 한 청크에 이름 여럿
            _chunk("c4", "(module docstring)"),           # 이름이 아니다
            _chunk("c5", None),                           # 문서·줄 윈도우 청크
        ])

    def test_full_name_beats_tail(self):
        # process 는 c2 의 꼬리(등급 1), PaymentService.process 는 전체 이름(등급 0)
        self.assertEqual(self.idx.lookup(["PaymentService.process"])["c2"], 0)
        self.assertEqual(self.idx.lookup(["process"])["c2"], 1)

    def test_comma_separated_symbols_both_indexed(self):
        self.assertIn("c3", self.idx.lookup(["DEFAULT_RETRY"]))
        self.assertIn("c3", self.idx.lookup(["MAX_WAIT"]))

    def test_placeholder_and_missing_symbols_are_skipped(self):
        self.assertNotIn("c4", self.idx.lookup(["(module docstring)"]))
        self.assertEqual(self.idx.lookup(["없는이름"]), {})

    def test_case_insensitive(self):
        self.assertIn("c1", self.idx.lookup(["paymentservice"]))


class Reorder(unittest.TestCase):
    def test_scores_are_never_touched(self):
        """CHARTER 5 — 재정렬은 순서만 바꾼다. 점수가 바뀌면 임계값 판정이 흔들린다."""
        hits = [_chunk("a", "X", 0.90), _chunk("b", "Y", 0.70), _chunk("c", "Z", 0.60)]
        before = {h["_id"]: h["score"] for h in hits}
        out = symbols.reorder(hits, {"c": 0})
        self.assertEqual([h["_id"] for h in out], ["c", "a", "b"])
        self.assertEqual({h["_id"]: h["score"] for h in out}, before)

    def test_top_score_and_has_evidence_are_order_independent(self):
        hits = [_chunk("a", "X", 0.90), _chunk("b", "Y", 0.30)]
        th = 0.54
        out = symbols.reorder(hits, {"b": 0})       # 낮은 점수를 맨 앞으로 끌어올려도
        self.assertEqual(max(h["score"] for h in out), max(h["score"] for h in hits))
        self.assertEqual(bool([h for h in out if h["score"] >= th]),
                         bool([h for h in hits if h["score"] >= th]))

    def test_stable_within_same_grade(self):
        hits = [_chunk(c, c, 0.5) for c in "abcd"]
        out = symbols.reorder(hits, {"b": 0, "d": 0})
        self.assertEqual([h["_id"] for h in out], ["b", "d", "a", "c"])   # 원래 순서 유지

    def test_no_match_returns_input_unchanged(self):
        hits = [_chunk("a", "X"), _chunk("b", "Y")]
        self.assertIs(symbols.reorder(hits, {}), hits)


if __name__ == "__main__":
    unittest.main()
