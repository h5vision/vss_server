from __future__ import annotations

import json
import unittest
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


if __name__ == "__main__":
    unittest.main()
