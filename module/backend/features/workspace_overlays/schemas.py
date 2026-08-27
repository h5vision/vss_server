"""Exact request schema sent by the current VS Code Frontend."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, field_validator, model_validator

from backend.features.workspace_overlays.validation import (
    validate_git_revision,
    validate_posix_relative_path,
)

GitRevision = Annotated[str, AfterValidator(validate_git_revision)]
PosixRelativePath = Annotated[str, AfterValidator(validate_posix_relative_path)]


class WorkspaceOverlayFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["added", "modified"]
    path: PosixRelativePath
    content: str
    encoding: Literal["utf-8"]


class WorkspaceOverlayRename(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_path: PosixRelativePath
    new_path: PosixRelativePath

    @model_validator(mode="after")
    def paths_must_differ(self) -> Self:
        if self.old_path == self.new_path:
            raise ValueError("old_path and new_path must differ")
        return self


class WorkspaceOverlayRequest(BaseModel):
    """Do not add fields that the current Frontend does not send."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    base_revision: GitRevision
    target_revision: GitRevision
    files: list[WorkspaceOverlayFile]
    deleted_paths: list[PosixRelativePath]
    renames: list[WorkspaceOverlayRename]

    @field_validator("project_id")
    @classmethod
    def project_id_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("project_id must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_cross_field_path_rules(self) -> Self:
        file_paths = [item.path for item in self.files]
        deleted_paths = self.deleted_paths
        rename_old_paths = [item.old_path for item in self.renames]
        rename_new_paths = [item.new_path for item in self.renames]

        if len(file_paths) != len(set(file_paths)):
            raise ValueError("files contains duplicate paths")
        if len(deleted_paths) != len(set(deleted_paths)):
            raise ValueError("deleted_paths contains duplicate paths")
        if len(rename_old_paths) != len(set(rename_old_paths)):
            raise ValueError("renames contains duplicate old_path values")
        if len(rename_new_paths) != len(set(rename_new_paths)):
            raise ValueError("renames contains duplicate new_path values")

        file_path_set = set(file_paths)
        deleted_path_set = set(deleted_paths)
        conflict = file_path_set & deleted_path_set
        if conflict:
            raise ValueError(f"paths cannot be both changed and deleted: {sorted(conflict)}")

        missing_content = set(rename_new_paths) - file_path_set
        if missing_content:
            raise ValueError(
                f"rename destinations require final content in files: {sorted(missing_content)}"
            )
        return self
