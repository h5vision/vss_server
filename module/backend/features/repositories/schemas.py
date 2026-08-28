"""Admin-facing Repository and Branch binding schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


def validate_branch_ref(value: str) -> str:
    """Validate the safe full-ref subset used by the Admin contract."""

    if not value.startswith("refs/heads/"):
        raise ValueError("branch_ref must start with 'refs/heads/'")
    short_name = value.removeprefix("refs/heads/")
    if not short_name or len(value) > 512:
        raise ValueError("branch_ref must contain a branch name")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("branch_ref must not contain control characters")
    if (
        "\\" in value
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(character in value for character in " ~^:?*[")
        or value.endswith(("/", ".", ".lock"))
    ):
        raise ValueError("branch_ref is not a safe normalized Git branch ref")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in short_name.split("/")):
        raise ValueError("branch_ref contains an invalid path component")
    return value


BranchRef = Annotated[str, AfterValidator(validate_branch_ref)]


class RepositoryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=1, max_length=512)
    display_name: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=64)
    remote_url: HttpUrl
    default_branch_ref: BranchRef
    active: bool = True

    @field_validator("canonical_name", "display_name", "provider")
    @classmethod
    def strip_non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("remote_url")
    @classmethod
    def remote_url_must_not_embed_credentials(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password:
            raise ValueError("remote_url must not embed credentials")
        return value


class RepositoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    remote_url: HttpUrl | None = None
    default_branch_ref: BranchRef | None = None
    active: bool | None = None

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized

    @field_validator("remote_url")
    @classmethod
    def remote_url_must_not_embed_credentials(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (value.username or value.password):
            raise ValueError("remote_url must not embed credentials")
        return value

    @model_validator(mode="after")
    def require_at_least_one_change(self) -> RepositoryUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        return self


class RepositoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID
    canonical_name: str
    display_name: str
    provider: str
    remote_url: HttpUrl
    default_branch_ref: BranchRef
    active: bool
    created_at: datetime
    updated_at: datetime


class RepositoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RepositoryResponse]
    next_cursor: str | None = None


class BranchBindingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frontend_project_id: str = Field(min_length=1)
    frontend_workspace_name: str | None = Field(default=None, min_length=1, max_length=255)
    repository_id: UUID
    branch_ref: BranchRef
    vss_project_id: str = Field(min_length=1)
    active: bool = True

    @field_validator("frontend_project_id", "vss_project_id")
    @classmethod
    def strip_non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("frontend_workspace_name")
    @classmethod
    def strip_workspace_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("frontend_workspace_name must not be blank")
        return normalized


class BranchBindingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_id: UUID | None = None
    frontend_workspace_name: str | None = Field(default=None, min_length=1, max_length=255)
    branch_ref: BranchRef | None = None
    vss_project_id: str | None = Field(default=None, min_length=1)
    active: bool | None = None

    @field_validator("vss_project_id", "frontend_workspace_name")
    @classmethod
    def strip_optional_non_blank_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_at_least_one_change(self) -> BranchBindingUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        return self


class BranchBindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: UUID
    frontend_project_id: str
    frontend_workspace_name: str | None = None
    repository_id: UUID
    branch_ref: BranchRef
    vss_project_id: str
    active: bool
    verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class BranchBindingListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BranchBindingResponse]
    next_cursor: str | None = None
