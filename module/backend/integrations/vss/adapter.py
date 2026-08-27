"""Lazy, injectable adapter around the pinned ``vss.indexer`` public functions."""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from backend.integrations.vss.errors import (
    VssModuleCallFailed,
    VssModuleContractMismatch,
    VssModuleUnavailable,
)
from backend.integrations.vss.schemas import (
    VssExistsResult,
    VssIndexCommand,
    VssIndexStatus,
    VssProject,
    VssStartIndexResult,
)

if TYPE_CHECKING:
    from backend.core.config import Settings

_REQUIRED_START_INDEX_PARAMETERS = {
    "project_root",
    "project_id",
    "profile",
    "blocking",
    "force",
    "on_done",
    "extra_meta",
    "store",
}


class VssModuleAdapter:
    """Load VSS only on first use so VSS_* environment is settled beforehand."""

    def __init__(
        self,
        module_name: str = "vss.indexer",
        *,
        importer: Callable[[str], ModuleType] = importlib.import_module,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._module_name = module_name
        self._importer = importer
        self._environment = dict(environment or {})
        self._module: ModuleType | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        importer: Callable[[str], ModuleType] = importlib.import_module,
    ) -> VssModuleAdapter:
        return cls(
            settings.vss_module_name,
            importer=importer,
            environment=settings.vss_environment(),
        )

    def _load(self) -> ModuleType:
        if self._module is not None:
            return self._module
        root_package = self._module_name.split(".", maxsplit=1)[0]
        config_module = f"{root_package}.config"
        if self._environment and (
            self._module_name in sys.modules or config_module in sys.modules
        ):
            raise VssModuleContractMismatch(
                "VSS was imported before the configured VSS_* environment was applied."
            )
        for key, value in self._environment.items():
            os.environ[key] = value
        try:
            module = self._importer(self._module_name)
        except (ImportError, ModuleNotFoundError) as exc:
            raise VssModuleUnavailable(
                f"Cannot import configured VSS module {self._module_name!r}."
            ) from exc
        self._validate_contract(module)
        self._module = module
        return module

    @staticmethod
    def _validate_contract(module: ModuleType) -> None:
        for name in ("start_index", "status", "exists", "list_projects"):
            if not callable(getattr(module, name, None)):
                raise VssModuleContractMismatch(f"VSS module is missing callable {name}().")
        parameters = set(inspect.signature(module.start_index).parameters)
        missing = _REQUIRED_START_INDEX_PARAMETERS - parameters
        if missing:
            names = ", ".join(sorted(missing))
            raise VssModuleContractMismatch(f"VSS start_index() is missing parameters: {names}.")

    def start_index(
        self,
        command: VssIndexCommand,
        *,
        on_done: Callable[..., Any] | None = None,
        store: Any = None,
    ) -> VssStartIndexResult:
        module = self._load()
        kwargs = command.start_index_kwargs()
        kwargs["on_done"] = on_done
        kwargs["store"] = store
        try:
            result = module.start_index(**kwargs)
            return VssStartIndexResult.model_validate(result)
        except ValidationError as exc:
            raise VssModuleContractMismatch(
                "VSS start_index() returned an invalid result."
            ) from exc
        except VssModuleContractMismatch:
            raise
        except Exception as exc:
            raise VssModuleCallFailed("VSS start_index() raised an exception.") from exc

    def status(self, project_id: str, *, store: Any = None) -> VssIndexStatus:
        module = self._load()
        try:
            return VssIndexStatus.model_validate(module.status(project_id, store=store))
        except ValidationError as exc:
            raise VssModuleContractMismatch("VSS status() returned an invalid result.") from exc
        except VssModuleContractMismatch:
            raise
        except Exception as exc:
            raise VssModuleCallFailed("VSS status() raised an exception.") from exc

    def list_projects(self, *, store: Any = None) -> list[VssProject]:
        module = self._load()
        try:
            return [VssProject.model_validate(item) for item in module.list_projects(store=store)]
        except ValidationError as exc:
            raise VssModuleContractMismatch("VSS list_projects() returned invalid data.") from exc
        except VssModuleContractMismatch:
            raise
        except Exception as exc:
            raise VssModuleCallFailed("VSS list_projects() raised an exception.") from exc

    def exists(self, project_id: str, *, store: Any = None) -> VssExistsResult:
        module = self._load()
        try:
            return VssExistsResult.model_validate(module.exists(project_id, store=store))
        except ValidationError as exc:
            raise VssModuleContractMismatch("VSS exists() returned invalid data.") from exc
        except VssModuleContractMismatch:
            raise
        except Exception as exc:
            raise VssModuleCallFailed("VSS exists() raised an exception.") from exc
