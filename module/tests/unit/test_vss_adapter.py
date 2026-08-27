from __future__ import annotations

import sys
from types import ModuleType

import pytest

from backend.core.config import Settings
from backend.integrations.vss.adapter import VssModuleAdapter
from backend.integrations.vss.errors import VssModuleContractMismatch, VssModuleUnavailable
from backend.integrations.vss.schemas import VssIndexCommand, VssIndexState


def command() -> VssIndexCommand:
    return VssIndexCommand(
        project_root="/srv/snapshots/project/revision",
        project_id="project--main",
        expected_revision="2" * 40,
        snapshot_id="snapshot-id",
    )


def fake_module() -> ModuleType:
    module = ModuleType("fake_vss.indexer")

    def start_index(
        project_root,
        project_id,
        *,
        profile=None,
        blocking=False,
        force=False,
        on_done=None,
        extra_meta=None,
        store=None,
    ):
        return {"accepted": True, "project_id": project_id, "state": "running"}

    module.start_index = start_index
    module.status = lambda project_id, store=None: {"project_id": project_id, "state": "none"}
    module.exists = lambda project_id, store=None: {"project_id": project_id, "exists": False}
    module.list_projects = lambda store=None: [
        {
            "project_id": "project--main",
            "state": "done",
            "note": "module contract baseline",
        }
    ]
    return module


def test_adapter_loads_lazily_configures_environment_and_calls_contract(monkeypatch) -> None:
    imported: list[str] = []
    monkeypatch.delenv("VSS_STORE", raising=False)

    def importer(name: str) -> ModuleType:
        imported.append(name)
        assert __import__("os").environ["VSS_STORE"] == "chroma"
        return fake_module()

    adapter = VssModuleAdapter(
        "fake_vss.indexer",
        importer=importer,
        environment={"VSS_STORE": "chroma"},
    )
    assert imported == []

    result = adapter.start_index(command())

    assert imported == ["fake_vss.indexer"]
    assert result.accepted is True
    assert adapter.status("project--main").state is VssIndexState.NONE
    assert adapter.exists("project--main").exists is False
    assert adapter.list_projects()[0].note == "module contract baseline"


def test_adapter_rejects_missing_public_functions() -> None:
    adapter = VssModuleAdapter(importer=lambda _: ModuleType("broken"))

    with pytest.raises(VssModuleContractMismatch):
        adapter.start_index(command())


def test_adapter_factory_uses_backend_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VSS_DATA_DIR", raising=False)
    settings = Settings(
        vss_data_dir=tmp_path / "vss",
        vss_module_name="fake_vss.indexer",
    )

    def importer(_: str) -> ModuleType:
        assert __import__("os").environ["VSS_DATA_DIR"] == str((tmp_path / "vss").resolve())
        return fake_module()

    adapter = VssModuleAdapter.from_settings(settings, importer=importer)

    assert adapter.status("project--main").state is VssIndexState.NONE


def test_adapter_reports_import_failure() -> None:
    def importer(_: str) -> ModuleType:
        raise ModuleNotFoundError("vss")

    adapter = VssModuleAdapter(importer=importer)

    with pytest.raises(VssModuleUnavailable):
        adapter.start_index(command())


def test_adapter_rejects_module_imported_before_environment(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "vss.config", ModuleType("vss.config"))
    adapter = VssModuleAdapter(environment={"VSS_STORE": "chroma"})

    with pytest.raises(VssModuleContractMismatch):
        adapter.start_index(command())
