"""Pydantic row models and shared enums for the four ledger tables."""

from enum import StrEnum

from pydantic import BaseModel, Field


class ElementStatus(StrEnum):
    """Lifecycle state of a ledger record."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REMOVED = "REMOVED"


class ElementKind(StrEnum):
    """Distinguishes top-level objects (ENTITY) from their fields (PROPERTY)."""

    ENTITY = "ENTITY"
    PROPERTY = "PROPERTY"


class ConceptRow(BaseModel):
    """One row of concepts.csv — the agreed meaning of a model element."""

    serial: int = Field(ge=0)
    concept_uri: str
    current_label: str
    previous_labels: list[str] = Field(default_factory=list)
    status: ElementStatus


class RevisionRow(BaseModel):
    """One row of revisions.csv — assigned to every detected change regardless of whether it is breaking."""

    serial: int = Field(ge=0)
    concept_uri: str
    revision_uri: str
    previous_revision_uri: str | None = None
    status: ElementStatus


class VariantRow(BaseModel):
    """One row of variants.csv — a snapshot of the essential metadata that constitutes a data contract."""

    serial: int = Field(ge=0)
    concept_uri: str
    variant_uri: str
    revision_uri: str
    status: ElementStatus


class BindingRow(BaseModel):
    """One row of bindings.csv — maps a property variant to a concrete runtime path via an instance label."""

    serial: int = Field(ge=0)
    variant_uri: str
    binding_uri: str
    instance_label: str | None = None
    status: ElementStatus
