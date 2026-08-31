"""ast-v2 재귀 Python 청커와 ast-v1 호환성 회귀 테스트."""

import ast
import os
import unittest
from unittest import mock

from vss.chunker import chunk_text, python_nodes
from vss.config import Config, resolve_profile


SOURCE = '''\
if ENABLED:
    def guarded():
        return True

try:
    def optional():
        return "optional"
except ImportError:
    def fallback():
        return "fallback"

def factory():
    @app.get("/health")
    def handler():
        return {"ok": True}
    return handler

@register
class Outer(Base, metaclass=Meta):
    class Config:
        mode = "strict"

        def validate(self, value):
            return value
'''


PROFILE = {
    "chunker": "ast-v2",
    "chunk_size": 1200,
    "chunk_overlap": 0,
    "min_chunk_chars": 1,
    "ast_max_chars": 3500,
    "context_header": True,
}


class PythonNodesV2(unittest.TestCase):
    def test_ast_v1_출력과_누락은_그대로_보존한다(self):
        nodes = python_nodes(SOURCE, "ast-v1")
        symbols = {n["symbol"] for n in nodes}
        self.assertIn("factory", symbols)
        self.assertIn("Outer", symbols)
        self.assertNotIn("guarded", symbols)
        self.assertNotIn("optional", symbols)
        self.assertNotIn("fallback", symbols)
        self.assertNotIn("factory.handler", symbols)
        self.assertNotIn("Outer.Config", symbols)

        outer = next(n for n in nodes if n["symbol"] == "Outer")
        class_line = SOURCE.splitlines().index("class Outer(Base, metaclass=Meta):") + 1
        self.assertEqual(class_line, outer["line_start"])
        self.assertEqual("class Outer", outer["signature"])

        chunks = chunk_text(SOURCE, "sample.py", {**PROFILE, "chunker": "ast-v1"})
        self.assertEqual([{
            "type": "code", "path": "sample.py", "line_start": 12, "line_end": 16,
            "section": None, "enclosing": ["def factory"], "symbol": "factory",
            "text": '# sample.py > def factory\ndef factory():\n    @app.get("/health")\n'
                    '    def handler():\n        return {"ok": True}\n    return handler',
            "chunk_index": 0,
        }], chunks)

    def test_ast_v1_전체_kind_snapshot이_고정된다(self):
        # v1·v2 가 _emit 을 공유하므로 module_doc·const·docstring·method·분할까지 v1 출력 전체를 고정한다
        rich = '''\
"""Ledger module."""

FEE = 3

class Acct:
    """Account model."""

    limit = 10

    def pay(self, amount):
        return amount - FEE

def helper(value):
    return value + FEE
'''
        self.assertEqual([
            {"type": "code", "path": "ledger.py", "line_start": 1, "line_end": 1, "section": None,
             "enclosing": ["docstring (module docstring)"], "symbol": "(module docstring)",
             "text": '# ledger.py > docstring (module docstring)\n"""Ledger module."""', "chunk_index": 0},
            {"type": "code", "path": "ledger.py", "line_start": 3, "line_end": 3, "section": None,
             "enclosing": ["const FEE"], "symbol": "FEE",
             "text": "# ledger.py > const FEE\nFEE = 3", "chunk_index": 1},
            {"type": "code", "path": "ledger.py", "line_start": 6, "line_end": 6, "section": None,
             "enclosing": ["class Acct", "docstring Acct.__doc__"], "symbol": "Acct.__doc__",
             "text": '# ledger.py > class Acct > docstring Acct.__doc__\n"""Account model."""', "chunk_index": 2},
            {"type": "code", "path": "ledger.py", "line_start": 8, "line_end": 8, "section": None,
             "enclosing": ["class Acct", "const limit"], "symbol": "Acct.limit",
             "text": "# ledger.py > class Acct > const limit\nlimit = 10", "chunk_index": 3},
            {"type": "code", "path": "ledger.py", "line_start": 10, "line_end": 11, "section": None,
             "enclosing": ["class Acct", "def pay"], "symbol": "Acct.pay",
             "text": "# ledger.py > class Acct > def pay\ndef pay(self, amount):\n        return amount - FEE",
             "chunk_index": 4},
            {"type": "code", "path": "ledger.py", "line_start": 13, "line_end": 14, "section": None,
             "enclosing": ["def helper"], "symbol": "helper",
             "text": "# ledger.py > def helper\ndef helper(value):\n    return value + FEE", "chunk_index": 5},
        ], chunk_text(rich, "ledger.py", {**PROFILE, "chunker": "ast-v1"}))

    def test_ast_v1_분할_part_라벨_snapshot이_고정된다(self):
        long_src = '''\
def report():
    a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    c = "cccccccccccccccccccccccccccccc"
    return a + b + c
'''
        self.assertEqual([
            {"type": "code", "path": "long.py", "line_start": 1, "line_end": 2, "section": None,
             "enclosing": ["def report [part 1/3]"], "symbol": "report",
             "text": '# long.py > def report [part 1/3]\ndef report():\n    a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
             "chunk_index": 0},
            {"type": "code", "path": "long.py", "line_start": 3, "line_end": 3, "section": None,
             "enclosing": ["def report [part 2/3]"], "symbol": "report",
             "text": '# long.py > def report [part 2/3]\nb = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"', "chunk_index": 1},
            {"type": "code", "path": "long.py", "line_start": 4, "line_end": 5, "section": None,
             "enclosing": ["def report [part 3/3]"], "symbol": "report",
             "text": '# long.py > def report [part 3/3]\nc = "cccccccccccccccccccccccccccccc"\n    return a + b + c',
             "chunk_index": 2},
        ], chunk_text(long_src, "long.py", {**PROFILE, "chunker": "ast-v1", "ast_max_chars": 80}))

    def test_ast_v2가_제어문과_중첩_scope를_수집한다(self):
        nodes = python_nodes(SOURCE, "ast-v2")
        by_symbol = {n["symbol"]: n for n in nodes}
        for symbol in ("guarded", "optional", "fallback", "factory.handler",
                       "Outer.Config", "Outer.Config.mode", "Outer.Config.validate"):
            self.assertIn(symbol, by_symbol)

        handler = by_symbol["factory.handler"]
        self.assertEqual("function", handler["kind"])
        self.assertEqual(["factory"], handler["enclosing"])
        self.assertEqual(["function"], handler["enclosing_kinds"])

        validate = by_symbol["Outer.Config.validate"]
        self.assertEqual("method", validate["kind"])
        self.assertEqual(["Outer", "Config"], validate["enclosing"])
        self.assertEqual(["class", "class"], validate["enclosing_kinds"])

        outer = by_symbol["Outer"]
        decorator_line = SOURCE.splitlines().index("@register") + 1
        self.assertEqual(decorator_line, outer["line_start"])
        self.assertEqual("class Outer(Base, metaclass=Meta)", outer["signature"])

    def test_ast_v2_청크의_symbol_header_index가_결정적이다(self):
        chunks = chunk_text(SOURCE, "sample.py", PROFILE)
        by_symbol = {c["symbol"]: c for c in chunks}
        for symbol in ("guarded", "optional", "fallback", "factory.handler",
                       "Outer", "Outer.Config", "Outer.Config.mode", "Outer.Config.validate"):
            self.assertIn(symbol, by_symbol)
        self.assertEqual(["def factory", "def handler"], by_symbol["factory.handler"]["enclosing"])
        self.assertEqual(["class Outer", "class Config", "def validate"],
                         by_symbol["Outer.Config.validate"]["enclosing"])
        self.assertEqual(["class Outer"], by_symbol["Outer"]["enclosing"])
        self.assertEqual(["class Outer", "class Config"], by_symbol["Outer.Config"]["enclosing"])
        self.assertIn('@register\nclass Outer(Base, metaclass=Meta):', by_symbol["Outer"]["text"])
        self.assertIn("# <nested: factory.handler>", by_symbol["factory"]["text"])
        self.assertNotIn('return {"ok": True}', by_symbol["factory"]["text"])
        self.assertIn('return {"ok": True}', by_symbol["factory.handler"]["text"])
        self.assertIn("return handler", by_symbol["factory"]["text"])
        self.assertEqual(16, by_symbol["factory"]["line_end"])
        self.assertEqual(list(range(len(chunks))), [c["chunk_index"] for c in chunks])

    def test_짧은_중첩_정의도_마스킹_후_본문이_보존된다(self):
        source = '''\
def outer():
    def handler(a):
        return a + 1

    class Cfg:
        mode = "s"
    return handler
'''
        chunks = chunk_text(source, "short.py", {**PROFILE, "min_chunk_chars": 80})
        by_symbol = {c["symbol"]: c for c in chunks}
        self.assertIn("# <nested: outer.handler>", by_symbol["outer"]["text"])
        self.assertIn("return a + 1", by_symbol["outer.handler"]["text"])
        self.assertIn('mode = "s"', by_symbol["outer.Cfg.mode"]["text"])
        joined = "\n".join(c["text"] for c in chunks)
        self.assertIn("return a + 1", joined)

    def test_기본_min_chunk에서도_제어문_아래_정의가_청크가_된다(self):
        source = (
            "if ENABLED:\n"
            "    def guarded_reporter(records):\n"
            '        header = "id,name,total,created_at,updated_at"\n'
            "        rows = [format_row(r) for r in records if r.active]\n"
            '        return "\\n".join([header, *rows])\n'
        )
        chunks = chunk_text(source, "g.py", {**PROFILE, "min_chunk_chars": 80, "context_header": False})
        self.assertEqual(["guarded_reporter"], [c["symbol"] for c in chunks])

    def test_EOF_개행_없는_파일도_부모_line_end가_정확하다(self):
        source = 'def outer():\n    x = 1\n    def inner():\n        return x'
        by_symbol = {c["symbol"]: c for c in chunk_text(source, "eof.py", PROFILE)}
        self.assertEqual((1, 4), (by_symbol["outer"]["line_start"], by_symbol["outer"]["line_end"]))
        self.assertEqual((3, 4), (by_symbol["outer.inner"]["line_start"], by_symbol["outer.inner"]["line_end"]))

    def test_여러줄_header_닫는_줄의_본문은_header_청크에_통째로_남는다(self):
        chunks = chunk_text('class B(\n    Base): val = 1\n', "b.py", {**PROFILE, "context_header": False})
        self.assertEqual(1, len(chunks))
        self.assertEqual("class B(\n    Base): val = 1", chunks[0]["text"])
        self.assertEqual((1, 2), (chunks[0]["line_start"], chunks[0]["line_end"]))

    def test_한_줄_클래스는_한_청크만_나온다(self):
        chunks = chunk_text("class A: x = 1\n", "a.py", {**PROFILE, "context_header": False})
        self.assertEqual(["class A: x = 1"], [c["text"] for c in chunks])

    def test_함수_안_중첩_클래스의_일반_문장은_부모_청크에_남는다(self):
        source = 'def f():\n    class Inner:\n        print(1)\n        y = 2\n    return Inner\n'
        by_symbol = {c["symbol"]: c for c in chunk_text(source, "i.py", {**PROFILE, "context_header": False})}
        self.assertIn("print(1)", by_symbol["f"]["text"])          # 어떤 자식 청크에도 없는 문장
        self.assertNotIn("y = 2", by_symbol["f"]["text"])          # 자식 청크가 담는 줄은 마스킹
        self.assertIn("y = 2", by_symbol["f.Inner.y"]["text"])

    def test_masking은_method_부모와_깊이_2_중첩에도_적용된다(self):
        source = '''\
class Svc:
    def build(self):
        def inner():
            return make()
        return inner

def outer_fn():
    def mid():
        def leaf():
            return 1
        return leaf
    return mid
'''
        by_symbol = {c["symbol"]: c for c in chunk_text(source, "deep.py", {**PROFILE, "context_header": False})}
        self.assertIn("# <nested: Svc.build.inner>", by_symbol["Svc.build"]["text"])
        self.assertIn("return make()", by_symbol["Svc.build.inner"]["text"])
        self.assertIn("# <nested: outer_fn.mid>", by_symbol["outer_fn"]["text"])
        self.assertIn("# <nested: outer_fn.mid.leaf>", by_symbol["outer_fn.mid"]["text"])
        self.assertNotIn("return 1", by_symbol["outer_fn.mid"]["text"])
        self.assertIn("return 1", by_symbol["outer_fn.mid.leaf"]["text"])

    def test_문법_오류는_줄_윈도우로_폴백한다(self):
        chunks = chunk_text("def broken(:\n    pass\n", "broken.py", {**PROFILE, "context_header": False})
        self.assertEqual(1, len(chunks))
        self.assertIn("def broken", chunks[0]["text"])

    def test_type_parameter와_class_base가_signature에_남는다(self):
        source = "def identity[T](value: T) -> T:\n    return value\n\nclass Box[T](Base, metaclass=Meta):\n    pass\n"
        try:
            ast.parse(source)
        except SyntaxError:
            self.skipTest("현재 Python이 PEP 695 type parameter 문법을 지원하지 않음")
        by_symbol = {n["symbol"]: n for n in python_nodes(source, "ast-v2")}
        self.assertEqual("def identity[T](value: T) -> T", by_symbol["identity"]["signature"])
        self.assertEqual("class Box[T](Base, metaclass=Meta)", by_symbol["Box"]["signature"])

    def test_class_header가_첫_member_decorator를_중복하지_않는다(self):
        source = '''\
class Account:
    @property
    def display_name(self):
        return "account"
'''
        chunks = chunk_text(source, "account.py", PROFILE)
        by_symbol = {chunk["symbol"]: chunk for chunk in chunks}
        self.assertNotIn("@property", by_symbol["Account"]["text"])
        self.assertEqual(1, by_symbol["Account.display_name"]["text"].count("@property"))


class ChunkerFingerprint(unittest.TestCase):
    def test_기본값은_ast_v2이고_v1도_별도_fingerprint로_유지된다(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("ast-v2", Config().chunker)
        self.assertEqual("ast-v1", resolve_profile({"chunker": "ast-v1"})["chunker"])
        self.assertEqual("ast-v2", resolve_profile({"chunker": "ast-v2"})["chunker"])


if __name__ == "__main__":
    unittest.main()
