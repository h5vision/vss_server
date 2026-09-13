from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_legacy_rag_lab_runtime_boundary_is_removed() -> None:
    assert not list((ROOT / "backend" / "integrations" / "rag_lab").glob("*.py"))
    assert not list((ROOT / "tests" / "fixtures" / "model").glob("*.json"))

    runtime_text = "\n".join(
        path.read_text("utf-8") for path in (ROOT / "backend").rglob("*.py")
    )
    assert "RAG_LAB" not in runtime_text
    assert "integrations.rag_lab" not in runtime_text


def test_backend_uses_vss_http_without_importing_vss_runtime() -> None:
    runtime_text = "\n".join(
        path.read_text("utf-8") for path in (ROOT / "backend").rglob("*.py")
    )
    assert "from vss" not in runtime_text
    assert "import vss" not in runtime_text
    assert "importlib.import_module" not in runtime_text
    assert "vss.indexer.start_index" not in runtime_text


def test_vss_http_client_and_no_direct_adapter_exist() -> None:
    assert (ROOT / "backend" / "integrations" / "vss" / "client.py").is_file()
    assert not (ROOT / "backend" / "integrations" / "vss" / "adapter.py").exists()
