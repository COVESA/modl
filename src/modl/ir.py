"""Intermediate representation (IR) for model diff events consumed by the sync engine."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from modl.models import ElementKind


class ChangeType(StrEnum):
    """Type of change detected on a model element."""

    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"


class EntityChanged(BaseModel):
    """A detected change on an entity (branch/object type)."""

    label: str
    kind: ElementKind = ElementKind.ENTITY
    change_type: ChangeType
    changed_attributes: dict[str, Any] = {}


class PropertyChanged(BaseModel):
    """A detected change on a property (field/attribute) of a parent entity."""

    label: str
    parent_label: str
    kind: ElementKind = ElementKind.PROPERTY
    change_type: ChangeType
    changed_attributes: dict[str, Any] = {}


class DiffReport(BaseModel):
    """Ordered list of all changes detected between two model snapshots."""

    changes: list[EntityChanged | PropertyChanged]

    @classmethod
    def from_json(cls, json_str: str) -> DiffReport:
        """Parse a DiffReport from a JSON string."""
        return cls.model_validate_json(json_str)
