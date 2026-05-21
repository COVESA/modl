from enum import StrEnum

from pydantic import BaseModel, Field


class ElementStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REMOVED = "REMOVED"


class ElementKind(StrEnum):
    ENTITY = "ENTITY"
    PROPERTY = "PROPERTY"


class ConceptRow(BaseModel):
    id: int = Field(ge=0)
    concept_uri: str
    current_label: str
    previous_labels: list[str] = Field(default_factory=list)
    status: ElementStatus


class RevisionRow(BaseModel):
    id: int = Field(ge=0)
    concept_uri: str
    revision_uri: str
    previous_revision_uri: str | None = None
    status: ElementStatus


class VariantRow(BaseModel):
    id: int = Field(ge=0)
    concept_uri: str
    variant_uri: str
    revision_uri: str
    status: ElementStatus


class BindingRow(BaseModel):
    id: int = Field(ge=0)
    variant_uri: str
    binding_uri: str
    instance_label: str
    status: ElementStatus
