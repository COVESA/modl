"""Pydantic row models and shared enums for the five ledger tables."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ElementStatus(StrEnum):
    """Lifecycle state of a ledger record."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REMOVED = "REMOVED"


class ElementKind(StrEnum):
    """Structural kind of a model element, stored permanently in concepts.csv.

    The kind is set once when the concept is created and never changes.

    - ``ENTITY``: top-level model container — receives concepts, revisions, contracts; **no bindings**.
    - ``PROPERTY``: field of an entity — receives concepts, revisions, contracts, and **bindings**
      (one per instance when the parent entity declares instances; one singleton otherwise).
    - ``ENUMERATION_SET`` / ``ENUM_VALUE``: vocabulary elements (enums, units, code lists)
      that receive concept URIs, revisions and contracts but **no bindings**.

    ``current_label`` uniqueness (see :class:`ConceptRow`) is scoped by kind into two
    independent namespaces, mirroring GraphQL SDL: ``ENTITY`` + ``ENUMERATION_SET`` labels are
    globally unique against each other (like GraphQL ``type``/``enum`` names), while
    ``PROPERTY`` + ``ENUM_VALUE`` labels are unique only among siblings sharing the same
    ``parent_uri`` (like GraphQL fields and enum values, scoped to their enclosing type). The two
    namespaces are never compared against each other.
    """

    ENTITY = "ENTITY"
    PROPERTY = "PROPERTY"
    ENUMERATION_SET = "ENUMERATION_SET"
    ENUM_VALUE = "ENUM_VALUE"


class ConceptRow(BaseModel):
    """One row of concepts.csv — the agreed meaning of a model element.

    ``current_label`` uniqueness is scoped by ``kind`` — see :class:`ElementKind` for the two
    namespaces (container vs. member) and how they relate to ``parent_uri``.
    """

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
    revision_uri: str
    concept_uri: str
    previous_revision_uri: str | None = None
    status: ElementStatus


class ContractRow(BaseModel):
    """One row of contracts.csv — a versioned data contract for a concept.

    Each contract captures a distinct variant of the concept's essential metadata — the attributes
    that matter to downstream consumers (e.g. output type, unit, instance list). A new contract is
    minted whenever any of those essential attributes changes according to the breaking change config.
    Non-breaking changes leave the active contract untouched, keeping all downstream binding URIs stable.
    """

    serial: int = Field(ge=0)
    contract_uri: str
    concept_uri: str
    revision_uri: str
    status: ElementStatus


class BindingRow(BaseModel):
    """One row of bindings.csv — maps a property contract to a concrete runtime path via an instance label."""

    serial: int = Field(ge=0)
    binding_uri: str
    contract_uri: str
    instance_label: str | None = None
    status: ElementStatus


class RevisionAspectRow(BaseModel):
    """One row of revision_aspects.csv — the old/new value of one aspect changed by a revision.

    Identity is the composite key ``(revision_uri, aspect_key)`` — no row has its own serial or
    URI, since nothing else in the ledger references an individual aspect change by identity.
    ``operation`` is ``"added"``, ``"modified"``, or ``"removed"``: ``"modified"`` requires both
    ``previous_value`` and ``newer_value`` to be non-null; ``"added"`` forbids ``previous_value``;
    ``"removed"`` forbids ``newer_value``.
    """

    revision_uri: str
    aspect_key: str
    operation: str
    previous_value: Any | None = None
    newer_value: Any | None = None

    @model_validator(mode="after")
    def _validate_operation_nullability(self) -> "RevisionAspectRow":
        if self.operation not in {"added", "modified", "removed"}:
            raise ValueError(f"operation must be 'added', 'modified', or 'removed', got {self.operation!r}")
        if self.operation == "modified" and (self.previous_value is None or self.newer_value is None):
            raise ValueError("operation='modified' requires both previous_value and newer_value to be non-null")
        if self.operation == "added" and self.previous_value is not None:
            raise ValueError("operation='added' requires previous_value to be null")
        if self.operation == "removed" and self.newer_value is not None:
            raise ValueError("operation='removed' requires newer_value to be null")
        return self
