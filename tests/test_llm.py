from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from vss import llm


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class ModelsTest(unittest.TestCase):
    def test_only_running_completion_models_are_returned(self):
        requested: list[tuple[str, dict | None]] = []

        def urlopen(req, timeout):
            body = json.loads(req.data) if req.data else None
            requested.append((req.full_url, body))
            if req.full_url.endswith("/api/ps"):
                return _Response({"models": [
                    {"name": "qwen2.5-coder:7b"},
                    {"name": "bge-m3:latest"},
                    {"model": "qwen2.5-coder:7b"},  # 같은 모델은 한 번만 검사
                ]})
            capabilities = {
                "qwen2.5-coder:7b": ["completion"],
                "bge-m3:latest": ["embedding"],
            }
            return _Response({"capabilities": capabilities[body["model"]]})

        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(llm.models(), ["qwen2.5-coder:7b"])

        self.assertEqual(requested[0], (f"{llm.CFG.ollama_url.rstrip('/')}/api/ps", None))
        self.assertEqual([body["model"] for url, body in requested[1:]],
                         ["qwen2.5-coder:7b", "bge-m3:latest"])

    def test_missing_capabilities_is_an_error_not_a_silent_fallback(self):
        responses = iter([
            _Response({"models": [{"name": "unknown:latest"}]}),
            _Response({"details": {"family": "unknown"}}),
        ])
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=lambda *a, **k: next(responses)):
            with self.assertRaisesRegex(llm.LLMError, "능력 정보 없음"):
                llm.models()


class _FakeOllama:
    """올라온 모델 상태 + 요청 기록. /api/chat 에 빈 messages 가 오면 그 모델을 올린다 (Ollama 의 로드 동작)."""

    def __init__(self, loaded, installed=None):
        self.loaded = list(loaded)
        self.installed = installed          # None 이면 전부 설치돼 있다고 본다
        self.requests: list[tuple[str, dict | None]] = []

    def urlopen(self, req, timeout=None):
        url = req.full_url
        body = json.loads(req.data) if req.data else None
        self.requests.append((url.rsplit("/", 1)[-1], body))
        if url.endswith("/api/ps"):
            return _Response({"models": [{"name": m} for m in self.loaded]})
        if url.endswith("/api/show"):
            return _Response({"capabilities": ["embedding"] if body["model"].startswith("bge") else ["completion"]})
        if url.endswith("/api/chat"):
            m = body["model"]
            if self.installed is not None and m not in self.installed:
                raise urllib.error.HTTPError(url, 404, "Not Found", None, io.BytesIO(b'{"error":"model not found"}'))
            if m not in self.loaded:
                self.loaded.append(m)
            return _Response({"model": m, "message": {"role": "assistant", "content": ""}, "done": True})
        raise AssertionError(f"예상 밖 요청: {url}")


class PayloadTest(unittest.TestCase):
    """_payload 는 받은 이름을 그대로 쓴다. 다시 해석하면 override=false 에서 .env 모델로 바꿔 보내 로드 요청이 된다 (9/5 재현)."""

    def test_given_name_is_sent_as_is_even_when_override_is_off(self):
        with mock.patch.object(llm.CFG, "allow_model_override", False), \
             mock.patch.object(llm.CFG, "chat_model", "qwen3.8:27b"):
            p = llm._payload("gpt-oss:20b", [], stream=False, options={})
        self.assertEqual(p["model"], "gpt-oss:20b")

    def test_every_request_carries_keep_alive_minus_one(self):
        p = llm._payload("gpt-oss:20b", [], stream=True, options={})
        self.assertEqual(p["keep_alive"], -1)

    def test_none_picks_among_loaded_models_not_env(self):
        with mock.patch.object(llm, "models", lambda: ["gpt-oss:20b"]), \
             mock.patch.object(llm.CFG, "chat_model", "qwen3.8:27b"):
            self.assertEqual(llm._payload(None, [], stream=False, options={})["model"], "gpt-oss:20b")

    def test_resolve_model_is_gone(self):
        # .env 이름을 Ollama 로 보내던 마지막 통로. 남아 있으면 누군가 다시 부른다.
        self.assertFalse(hasattr(llm, "resolve_model"))


class EnsureLoadedTest(unittest.TestCase):
    """기동 전용. 목표 모델이 없으면 올리고, 있으면 아무것도 보내지 않고, 다른 모델은 절대 건드리지 않는다."""

    def setUp(self):
        p = mock.patch.object(llm.CFG, "chat_model", "qwen3.8:27b")
        p.start()
        self.addCleanup(p.stop)

    def _run(self, fake, model=None):
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=fake.urlopen):
            return llm.ensure_loaded(model)

    def test_already_loaded_sends_nothing_but_ps(self):
        fake = _FakeOllama(["bge-m3:latest", "qwen3.8:27b", "gpt-oss:20b"])
        r = self._run(fake)
        self.assertEqual((r["action"], r["ok"], r["model"]), ("none", True, "qwen3.8:27b"))
        self.assertEqual(r["others"], ["bge-m3:latest", "gpt-oss:20b"])
        self.assertEqual([u for u, _ in fake.requests], ["ps"])

    def test_missing_target_is_loaded_with_empty_messages_and_keep_alive(self):
        fake = _FakeOllama(["bge-m3:latest"])
        r = self._run(fake)
        self.assertEqual((r["action"], r["ok"], r["model"]), ("loaded", True, "qwen3.8:27b"))
        self.assertEqual(r["others"], ["bge-m3:latest"])
        chats = [b for u, b in fake.requests if u == "chat"]
        self.assertEqual(chats, [{"model": "qwen3.8:27b", "messages": [], "keep_alive": -1}])
        self.assertEqual([u for u, _ in fake.requests], ["ps", "chat", "ps"])   # 올린 뒤 다시 확인

    def test_never_unloads_or_touches_other_models(self):
        fake = _FakeOllama(["bge-m3:latest", "gpt-oss:20b", "someone-elses:13b"])
        r = self._run(fake)
        self.assertEqual(r["action"], "loaded")
        self.assertEqual(r["others"], ["bge-m3:latest", "gpt-oss:20b", "someone-elses:13b"])
        for u, body in fake.requests:
            if body is not None:
                self.assertNotEqual(body.get("keep_alive"), 0, "내리기 요청은 보내지 않는다")
                self.assertEqual(body["model"], "qwen3.8:27b", "목표 모델 외에는 이름을 보내지 않는다")

    def test_explicit_model_argument_wins_over_env(self):
        fake = _FakeOllama([])
        r = self._run(fake, model="gpt-oss:20b")
        self.assertEqual((r["model"], r["action"]), ("gpt-oss:20b", "loaded"))

    def test_tagless_target_matches_latest(self):
        fake = _FakeOllama(["qwen3.8:latest"])
        r = self._run(fake, model="qwen3.8")
        self.assertEqual((r["action"], r["model"]), ("none", "qwen3.8:latest"))

    def test_not_installed_raises_not_silently_starts(self):
        fake = _FakeOllama(["bge-m3:latest"], installed=["bge-m3:latest"])
        with self.assertRaisesRegex(llm.LLMError, "404"):
            self._run(fake)
        self.assertEqual(fake.loaded, ["bge-m3:latest"])

    def test_ollama_down_raises_llm_error(self):
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=OSError("connection refused")):
            with self.assertRaisesRegex(llm.LLMError, "접속 실패"):
                llm.ensure_loaded()


class PickModelTest(unittest.TestCase):
    """서버는 모델을 올리지 않는다 (md 결정 2026-09-05) — 올라온 모델 중에서만 고르고, 없으면 ModelNotLoaded."""

    def setUp(self):
        self._patches = [
            mock.patch.object(llm.CFG, "chat_model", "qwen2.5-coder:7b"),
            mock.patch.object(llm.CFG, "briefing_model", ""),
            mock.patch.object(llm.CFG, "allow_model_override", True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_requested_model_is_used_only_if_loaded(self):
        self.assertEqual(llm.pick_model("gpt-oss:20b", loaded=["qwen2.5-coder:7b", "gpt-oss:20b"]), "gpt-oss:20b")

    def test_requested_model_not_loaded_raises_instead_of_falling_back(self):
        # 요청은 명시적이다 — 올라온 다른 모델로 조용히 바꾸지 않는다.
        with self.assertRaises(llm.ModelNotLoaded) as cm:
            llm.pick_model("qwen:27b", loaded=["qwen2.5-coder:7b"])
        self.assertEqual(cm.exception.requested, "qwen:27b")
        self.assertEqual(cm.exception.loaded, ["qwen2.5-coder:7b"])
        self.assertEqual(cm.exception.code, "model_not_loaded")
        self.assertIsInstance(cm.exception, llm.LLMError)
        self.assertIn("qwen:27b", str(cm.exception))

    def test_env_model_is_a_preference_not_a_requirement(self):
        self.assertEqual(llm.pick_model(loaded=["gpt-oss:20b", "qwen2.5-coder:7b"]), "qwen2.5-coder:7b")
        # .env 모델이 안 올라와 있으면 올라온 것 중 첫 번째로 — 서버는 그래도 답한다
        self.assertEqual(llm.pick_model(loaded=["gpt-oss:20b", "llama3:8b"]), "gpt-oss:20b")

    def test_nothing_loaded_raises(self):
        with self.assertRaises(llm.ModelNotLoaded) as cm:
            llm.pick_model(loaded=[])
        self.assertIsNone(cm.exception.requested)
        self.assertIn("없음", str(cm.exception))

    def test_briefing_prefers_briefing_model_then_chat_model(self):
        with mock.patch.object(llm.CFG, "briefing_model", "qwen:27b"):
            self.assertEqual(llm.pick_model(purpose="briefing", loaded=["qwen2.5-coder:7b", "qwen:27b"]), "qwen:27b")
            self.assertEqual(llm.pick_model(purpose="briefing", loaded=["qwen2.5-coder:7b"]), "qwen2.5-coder:7b")
            self.assertEqual(llm.pick_model(purpose="briefing", loaded=["gpt-oss:20b"]), "gpt-oss:20b")

    def test_override_disabled_ignores_requested(self):
        with mock.patch.object(llm.CFG, "allow_model_override", False):
            self.assertEqual(llm.pick_model("qwen:27b", loaded=["qwen2.5-coder:7b"]), "qwen2.5-coder:7b")

    def test_tagless_name_means_latest_like_ollama(self):
        self.assertEqual(llm.pick_model("qwen2.5-coder", loaded=["qwen2.5-coder:latest"]), "qwen2.5-coder:latest")
        with self.assertRaises(llm.ModelNotLoaded):
            llm.pick_model("qwen2.5-coder", loaded=["qwen2.5-coder:7b"])   # :7b ≠ :latest — 다른 blob, 로드 요청이 된다
        with mock.patch.object(llm.CFG, "chat_model", "qwen2.5-coder"):
            self.assertEqual(llm.pick_model(loaded=["qwen2.5-coder:latest"]), "qwen2.5-coder:latest")

    def test_consults_api_ps_when_loaded_not_given_and_never_calls_api_chat(self):
        urls: list[str] = []

        def urlopen(req, timeout):
            urls.append(req.full_url)
            if req.full_url.endswith("/api/ps"):
                return _Response({"models": [{"name": "qwen2.5-coder:7b"}]})
            return _Response({"capabilities": ["completion"]})

        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(llm.pick_model(), "qwen2.5-coder:7b")
            with self.assertRaises(llm.ModelNotLoaded):
                llm.pick_model("qwen:27b")
        self.assertTrue(all(u.endswith(("/api/ps", "/api/show")) for u in urls), urls)


if __name__ == "__main__":
    unittest.main()
