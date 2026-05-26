"""Intermediate representation (IR) for model diff events consumed by the sync engine.

A diff report describes what changed between two snapshots of a domain model.  It contains
an ordered list of change events — one per modified entity or property.  The sync engine
processes these events to update the ledger (concepts, revisions, variants, bindings).

Terminology
-----------
Entity (a.k.a. container, object type, branch, class, feature of interest)
    A top-level model element that groups properties.  In GraphQL SDL this is a ``type``; in
    vspec it is a ``branch`` node.

Property (a.k.a. field, attribute, signal, characteristic, aspect)
    A named attribute that belongs to exactly one entity.  In GraphQL SDL this is a ``field``
    inside a ``type``; in vspec it is a leaf node (``sensor``, ``actuator``, etc.).

Aspect
    Any named attribute of a property that can change.  The IR recognises three *canonical*
    keys that carry type-system meaning for all modeling languages:

    - ``output_type`` — base type name the property resolves to (e.g. ``"Float"``, ``"Door"``).
    - ``is_list`` — ``True`` when the property resolves to a list of that type.
    - ``is_required`` — ``True`` when the value is guaranteed non-null / mandatory.

    All other keys are *adapter-defined*: the language adapter decides their names (e.g.
    ``"unit"``, ``"min"``, ``"accuracy"``).  The breaking-change config references them by
    their exact key name.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, model_validator

from modl.models import ElementKind

if TYPE_CHECKING:
    from modl.config import BreakingChangeConfig


class ChangeType(StrEnum):
    """Type of change detected on a model element."""

    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"


class ContentItem(BaseModel):
    """Reference to a child property that changed within an entity's content."""

    label: str
    change_type: ChangeType


class DiffReportValidationError(Exception):
    """Raised when a diff report violates structural or configuration constraints."""


class EntityChanged(BaseModel):
    """A detected change on an entity (a.k.a. container, object type, branch, class).

    Payload rules by change_type:

    - ``ADDED``: ``aspects`` holds the full initial-state snapshot of the entity's attributes.
      ``content`` and ``renamed_from`` must be absent.
    - ``MODIFIED``: ``aspects`` holds only the keys that changed (delta); ``renamed_from`` is
      set when the entity was renamed; ``content`` lists the children that changed.
    - ``REMOVED``: ``aspects`` and ``content`` must be empty; ``renamed_from`` must be absent.
    """

    label: str
    kind: ElementKind = ElementKind.ENTITY
    change_type: ChangeType
    renamed_from: str | None = None
    aspects: dict[str, Any] = {}
    content: list[ContentItem] = []

    @model_validator(mode="after")
    def _validate_constraints(self) -> EntityChanged:
        if self.change_type == ChangeType.REMOVED and (self.aspects or self.content):
            raise ValueError("REMOVED events must not carry aspects or content")
        if self.change_type == ChangeType.ADDED and self.content:
            raise ValueError("ADDED events must not carry content")
        if self.renamed_from is not None and self.change_type != ChangeType.MODIFIED:
            raise ValueError("renamed_from is only valid on MODIFIED events")
        return self


class PropertyChanged(BaseModel):
    """A detected change on a property of a parent entity (a.k.a. field, attribute, signal).

    Payload rules by change_type:

    - ``ADDED``: ``aspects`` holds the full initial-state snapshot; ``output_type`` is expected
      among the keys.  ``renamed_from`` must be absent.
    - ``MODIFIED``: ``aspects`` holds only the keys that changed (delta); ``renamed_from`` is
      set when the property was renamed.
    - ``REMOVED``: ``aspects`` must be empty; ``renamed_from`` must be absent.

    Canonical aspect keys: ``output_type``, ``is_list``, ``is_required``.
    All other keys are adapter-defined and configurable in the breaking-change config by their
    exact key name (e.g. ``unit``, ``min``, ``accuracy``).
    """

    label: str
    parent_label: str
    kind: ElementKind = ElementKind.PROPERTY
    change_type: ChangeType
    renamed_from: str | None = None
    aspects: dict[str, Any] = {}

    @model_validator(mode="after")
    def _validate_constraints(self) -> PropertyChanged:
        if self.change_type == ChangeType.REMOVED and self.aspects:
            raise ValueError("REMOVED events must not carry aspects")
        if self.renamed_from is not None and self.change_type != ChangeType.MODIFIED:
            raise ValueError("renamed_from is only valid on MODIFIED events")
        return self


class DiffReport(BaseModel):
    """Ordered list of all changes detected between two model snapshots."""

    changes: list[EntityChanged | PropertyChanged]

    @classmethod
    def from_json(cls, json_str: str) -> DiffReport:
        """Parse a DiffReport from a JSON string."""
        return cls.model_validate_json(json_str)

    def validate_structure(self, *, strict: bool = False) -> list[str]:
        """Check structural constraints across the report; return warning messages.

        Detects duplicate events for the same label within the same kind.
        Raises :exc:`DiffReportValidationError` if *strict* is ``True`` and any warnings arise.
        """
        warnings: list[str] = []
        seen_entities: set[str] = set()
        seen_properties: set[tuple[str, str]] = set()

        for change in self.changes:
            if isinstance(change, EntityChanged):
                if change.label in seen_entities:
                    warnings.append(f"Duplicate entity event for label '{change.label}'")
                seen_entities.add(change.label)
            else:
                key = (change.label, change.parent_label)
                if key in seen_properties:
                    warnings.append(
                        f"Duplicate property event for label '{change.label}' under parent '{change.parent_label}'"
                    )
                seen_properties.add(key)

        if strict and warnings:
            raise DiffReportValidationError("\n".join(warnings))
        return warnings


# ── Config-aware validation ───────────────────────────────────────────────────

#: Aspect keys that are always recognised regardless of user configuration.
CANONICAL_ASPECT_KEYS: frozenset[str] = frozenset({"output_type", "is_list", "is_required"})


def validate_report_aspects(
    report: DiffReport,
    config: BreakingChangeConfig,
    *,
    strict: bool = False,
) -> list[str]:
    """Check that all aspect keys in MODIFIED events are declared in the breaking-change config.

    Unknown keys are non-breaking by default (warn + opt-in).  Set *strict* to ``True`` to
    raise :exc:`DiffReportValidationError` instead of returning warnings.

    Returns a list of warning strings (empty when all keys are known).
    """
    warnings: list[str] = []

    for change in report.changes:
        if change.change_type != ChangeType.MODIFIED or not change.aspects:
            continue
        cfg = config.entity if change.kind == ElementKind.ENTITY else config.property
        configured: set[str] = set(cfg.keys())
        unknown = set(change.aspects.keys()) - configured - CANONICAL_ASPECT_KEYS
        for key in sorted(unknown):
            warnings.append(
                f"[{change.label}] Aspect key '{key}' is not declared in the breaking-change config "
                "— treated as non-breaking by default"
            )

    if strict and warnings:
        raise DiffReportValidationError("\n".join(warnings))
    return warnings
