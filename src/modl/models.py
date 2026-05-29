"""Pydantic row models and shared enums for the four ledger tables."""

from enum import StrEnum

from pydantic import BaseModel, Field


class ElementStatus(StrEnum):
    """Lifecycle state of a ledger record."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REMOVED = "REMOVED"


class ElementKind(StrEnum):
    """Structural kind of a model element, stored permanently in concepts.csv.

    The kind is set once when the concept is created and never changes.

    - ``ENTITY``: top-level model container — receives concepts, revisions, variants; **no bindings**.
    - ``PROPERTY``: field of an entity — receives concepts, revisions, variants, and **bindings**
      (one per instance when the parent entity declares instances; one singleton otherwise).
    - ``ENUMERATION_SET`` / ``ENUM_VALUE``: vocabulary elements (enums, units, code lists)
      that receive concept URIs, revisions and variants but **no bindings**.
    """

    ENTITY = "ENTITY"
    PROPERTY = "PROPERTY"
    ENUMERATION_SET = "ENUMERATION_SET"
    ENUM_VALUE = "ENUM_VALUE"


class ConceptRow(BaseModel):
    """One row of concepts.csv — the agreed meaning of a model element."""

    serial: int = Field(ge=0)
    concept_uri: str
    current_label: str
    previous_labels: list[str] = Field(default_factory=list)
    kind: ElementKind
    status: ElementStatus
    parent_uri: str | None = None
    instances: list[str] | None = None


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
