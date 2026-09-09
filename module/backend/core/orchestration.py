"""Snapshot과 VSS 사이의 인덱싱 시작 소유권."""

from __future__ import annotations

from typing import Literal

IndexOrchestrationMode = Literal["module_push", "vss_pull"]

MODULE_PUSH: IndexOrchestrationMode = "module_push"
VSS_PULL: IndexOrchestrationMode = "vss_pull"
