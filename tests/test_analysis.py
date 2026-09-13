"""analysis.py 결정적 추출의 오탐·미탐 회귀 테스트.

2026-08-29 오탐 사냥에서 실행으로 재현된 케이스들을 고정한다 — 라우트 표(routes_of),
포함 라우터(router_prefixes), 진입점(entry_points). 전부 순수 함수라 Ollama·저장소가 필요 없다.
"""

import tempfile
import textwrap
import unittest
from pathlib import Path

from vss.analysis import entry_points, router_prefixes, routes_of, symbols_of
from vss.briefing import Collected, _entry_sections


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def write(self, rel: str, content: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p


class RoutesOf(_Tmp):
    def test_docstring_과_템플릿_문자열_안은_라우트가_아니다(self):
        p = self.write("main.py", '''
            """Adding a route::

                @app.get("/health")
                def health():
                    return {"ok": True}
            """
            from fastapi import FastAPI
            app = FastAPI()

            ROUTE_TEMPLATE = \'\'\'
            @app.get("/{name}")
            async def read_{name}():
                return {{"name": "{name}"}}
            \'\'\'

            @app.get("/")
            def read_root():
                return {"service": "x"}
        ''')
        routes = routes_of(p, self.root)
        self.assertEqual([("GET", "/", "read_root")],
                         [(r["method"], r["path"], r["handler"]) for r in routes])

    def test_라우터_아닌_객체의_데코레이터는_제외된다(self):
        p = self.write("test_api.py", '''
            from unittest import mock
            import pytest
            import pook

            @mock.patch("app.services.fetch_profile")
            def test_read_root(fetch):
                pass

            @pook.get("https://api.example.com/users", reply=200)
            def test_fetch_users():
                pass

            @pook.get("/users", reply=200)
            def test_relative_users():
                pass

            @pytest.mark.get("/integration")
            def test_integration_marker():
                pass

            api = pook.API()

            @api.get("/aliased-users")
            def test_aliased_pook():
                pass

            import pook as router

            @router.get("/import-alias")
            def test_import_alias():
                pass
        ''')
        self.assertEqual([], routes_of(p, self.root))

    def test_다른_함수_안의_mock_대입이_모듈_라우트를_지우지_않는다(self):
        p = self.write("main.py", '''
            from fastapi import FastAPI
            from unittest.mock import MagicMock

            app = FastAPI()

            @app.get("/real")
            def real_handler():
                return {"ok": True}

            def test_read_root():
                app = MagicMock()

                @app.get("/mocked")
                def mocked_handler():
                    pass
        ''')
        routes = routes_of(p, self.root)
        self.assertEqual([("GET", "/real", "real_handler")],
                         [(r["method"], r["path"], r["handler"]) for r in routes])

    def test_여러줄_데코레이터와_긴_kwargs_도_handler_까지_잡힌다(self):
        p = self.write("api.py", '''
            from fastapi import APIRouter, Depends
            router = APIRouter()

            @router.get(
                "/users/{user_id}",
                response_model=dict,
            )
            async def get_user(user_id: int):
                return {}

            @router.get("/items/{item_id}",
                        response_model=dict,
                        responses={404: {"description": "Not found"}},
                        dependencies=[Depends(str)],
                        tags=["items"],
                        summary="Get one item",
                        include_in_schema=True)
            async def read_item(item_id: int):
                return {}
        ''')
        got = routes_of(p, self.root)
        self.assertEqual(2, len(got))
        routes = {r["path"]: r for r in got}
        self.assertEqual("get_user", routes["/users/{user_id}"]["handler"])
        self.assertEqual("read_item", routes["/items/{item_id}"]["handler"])

    def test_methods_인자_줄바꿈과_튜플(self):
        p = self.write("flaskish.py", '''
            @app.route("/upload",
                       methods=["POST"])
            def upload():
                return "ok"

            @api.api_route("/proxy",
                           methods=["POST", "PUT"])
            async def proxy():
                return {}

            @app.route("/tuple", methods=("POST",))
            def tuple_methods():
                return "ok"

            @app.route("/plain")
            def plain():
                return "ok"
        ''')
        methods = {r["path"]: r["method"] for r in routes_of(p, self.root)}
        self.assertEqual({"/upload": "POST", "/proxy": "POST,PUT", "/tuple": "POST", "/plain": "GET"}, methods)

    def test_methods_set과_동적_요소를_부분_손실하지_않는다(self):
        p = self.write("dynamic_methods.py", '''
            METHOD = "PATCH"
            METHODS = ["DELETE"]

            @app.route("/set", methods={"POST", "PUT"})
            def set_methods():
                return "ok"

            @app.route("/mixed", methods=["POST", METHOD])
            def mixed_methods():
                return "ok"

            @api.api_route("/constant", methods=METHODS)
            async def constant_methods():
                return {}

            @api.api_route("/default")
            async def default_methods():
                return {}
        ''')
        methods = {r["path"]: r["method"] for r in routes_of(p, self.root)}
        self.assertEqual({"/set": "POST,PUT", "/mixed": "POST,METHOD",
                          "/constant": "METHODS", "/default": "GET"}, methods)

    def test_object와_decorator_line_반환_계약(self):
        p = self.write("line_contract.py", '''
            from fastapi import APIRouter
            accounts = APIRouter()

            @accounts.get(
                "/accounts",
            )
            async def list_accounts():
                return []
        ''')
        route = routes_of(p, self.root)[0]
        self.assertEqual("accounts", route["object"])
        self.assertEqual(5, route["line"])

    def test_문법_오류는_라우트와_prefix를_반환하지_않는다(self):
        p = self.write("broken.py", '@app.get("/broken")\ndef broken(:\n    pass\n')
        self.assertEqual([], routes_of(p, self.root))
        self.assertEqual([], router_prefixes(p))

    def test_경로_표기_변형(self):
        p = self.write("variants.py", '''
            from fastapi import FastAPI
            app = FastAPI()
            API_V = "v1"

            @app.get(f"/{API_V}/items")
            async def list_items():
                return []

            @app.get(path="/health")
            async def health():
                return {}

            @ app.get("/spaced")
            async def spaced():
                return {}

            @app.trace("/debug")
            async def debug_trace():
                return {}

            @app.websocket_route("/ws-legacy")
            async def ws_legacy(ws):
                pass
        ''')
        got = routes_of(p, self.root)
        self.assertEqual(5, len(got))
        routes = {r["handler"]: r for r in got}
        self.assertEqual("/{API_V}/items", routes["list_items"]["path"])
        self.assertEqual("/health", routes["health"]["path"])
        self.assertEqual("/spaced", routes["spaced"]["path"])
        self.assertEqual("TRACE", routes["debug_trace"]["method"])
        self.assertEqual("WEBSOCKET", routes["ws_legacy"]["method"])

    def test_팩토리_안에_정의된_핸들러도_잡힌다(self):
        p = self.write("factory.py", '''
            from fastapi import FastAPI

            def create_app() -> FastAPI:
                app = FastAPI()

                @app.get("/health")
                def health():
                    return {"ok": True}
                return app
        ''')
        self.assertEqual([("GET", "/health", "health")],
                         [(r["method"], r["path"], r["handler"]) for r in routes_of(p, self.root)])

    def test_빈_경로와_빈_methods_는_라우트로_남는다(self):
        p = self.write("edge.py", '''
            from fastapi import APIRouter
            from flask import Flask
            router = APIRouter()
            app = Flask(__name__)

            @router.get("")
            def prefixed_root():
                pass

            @app.route("/x", methods=[])
            def empty_methods():
                pass
        ''')
        self.assertEqual([("GET", "", "prefixed_root"), ("GET", "/x", "empty_methods")],
                         [(r["method"], r["path"], r["handler"]) for r in routes_of(p, self.root)])

    def test_import_별칭과_클래스_속성_라우터도_잡힌다(self):
        p = self.write("alias.py", '''
            import myapp.api as r
            from fastapi import APIRouter

            class Holder:
                router = APIRouter()

            @r.get("/alias")
            def alias_ep():
                pass

            @Holder.router.get("/cls")
            def cls_ep():
                pass
        ''')
        self.assertEqual([("/alias", "r"), ("/cls", "Holder.router")],
                         [(x["path"], x["object"]) for x in routes_of(p, self.root)])

    def test_fixture_인자_mock_이름은_휴리스틱에서_제외된다(self):
        p = self.write("test_fx.py", '''
            def test_thing(mock_app):
                @mock_app.get("/users")
                def inner():
                    pass
        ''')
        self.assertEqual([], routes_of(p, self.root))


class RouterPrefixes(_Tmp):
    def test_주석과_docstring_속_include_router_는_제외된다(self):
        p = self.write("main.py", '''
            """Routers are mounted like ``app.include_router(admin.router, prefix="/admin")``."""
            from fastapi import FastAPI
            app = FastAPI()
            app.include_router(users.router, prefix="/users")
            # app.include_router(legacy.router, prefix="/v0")
        ''')
        self.assertEqual([{"router": "users.router", "prefix": "/users"}], router_prefixes(p))

    def test_키워드_호출과_비리터럴_prefix(self):
        p = self.write("main.py", '''
            API_PREFIX = "/api/v1/reports"
            app.include_router(router=admin.router, prefix="/admin")
            app.include_router(build_health_router(), prefix="/health")
            app.include_router(reports.router, prefix=API_PREFIX)
        ''')
        got = router_prefixes(p)
        self.assertEqual(3, len(got))
        self.assertEqual({"router": "admin.router", "prefix": "/admin"}, got[0])
        self.assertEqual({"router": "build_health_router()", "prefix": "/health"}, got[1])
        self.assertEqual({"router": "reports.router", "prefix": "API_PREFIX"}, got[2])


class SymbolsOf(_Tmp):
    def test_브리핑_함수_목록도_ast_v2_중첩_symbol을_사용한다(self):
        p = self.write("factory.py", '''
            def create_app():
                def health():
                    return {"ok": True}
                return health

            class Settings:
                class Config:
                    def validate(self, value):
                        return value
        ''')
        items = symbols_of(p, self.root)
        symbols = {item["symbol"] for item in items}
        self.assertIn("create_app.health", symbols)
        self.assertIn("Settings.Config", symbols)
        self.assertIn("Settings.Config.validate", symbols)
        collected = Collected(analysis={"entry_points": [{
            "path": "factory.py", "reason": "fixture", "symbols": items,
            "routes": [], "routers": [],
        }]})
        _entries, functions = _entry_sections(collected)
        self.assertIn("**`create_app.health`** — `def health()`", functions)
        self.assertIn("**`Settings.Config.validate`** — `def validate(self, value)`", functions)
        self.assertNotIn("**`create_app`**", functions)     # 최상위 symbol 에는 qualified prefix 가 붙지 않는다
        self.assertEqual(1, functions.count("`def health()`"))


class EntryPoints(_Tmp):
    def test_문서_파일과_주석_속_마커는_진입점이_아니다(self):
        readme = self.write("README.md", '''
            # Quickstart
            ```python
            from fastapi import FastAPI
            app = FastAPI()
            ```
        ''')
        config = self.write("config.py", '''
            """Settings for the Flask() application (legacy note)."""
            import os
            DEBUG = os.environ.get("DEBUG", "0") == "1"
        ''')
        helper = self.write("app/utils/helpers.py", '''
            # NOTE: the FastAPI() instance lives in app.main — do not create another one here.
            def slugify(s):
                return s
        ''')
        conftest = self.write("conftest.py", '''
            from app.main import create_app_settings
        ''')
        real = self.write("app/main.py", '''
            from fastapi import FastAPI
            app = FastAPI(title="x")
        ''')
        got = entry_points(self.root, [readme, config, helper, conftest, real])
        paths = [e["path"] for e in got]
        self.assertEqual(["app/main.py"], paths)
        self.assertIn("FastAPI", got[0]["reason"])

    def test_테스트_파일의_main_꼬리는_진입점_후보에서_밀린다(self):
        cli = self.write("vss/cli.py", '''
            def main():
                pass

            if __name__ == "__main__":
                main()
        ''')
        in_tests_dir = self.write("tests/test_chunker.py", '''
            import unittest

            if __name__ == "__main__":
                unittest.main()
        ''')
        suffix_named = self.write("bench_test.py", '''
            if __name__ == "__main__":
                print("bench")
        ''')
        got = entry_points(self.root, [cli, in_tests_dir, suffix_named])
        self.assertEqual(["vss/cli.py"], [e["path"] for e in got])

    def test_마커가_6000자_뒤나_파일_끝에_있어도_잡힌다(self):
        pad = "\n".join(f"x{i} = {i}" for i in range(1200))       # 문자열 없는 패딩 > 6000자
        tail = self.write("benchmark.py",
                          pad + '\n\ndef main():\n    pass\n\nif __name__ == "__main__":\n    main()\n')
        got = entry_points(self.root, [tail])
        self.assertEqual(["benchmark.py"], [e["path"] for e in got])


if __name__ == "__main__":
    unittest.main()
