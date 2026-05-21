from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from modl.models import ElementKind


class ChangeType(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"


class EntityChanged(BaseModel):
    label: str
    kind: ElementKind = ElementKind.ENTITY
    change_type: ChangeType
    changed_attributes: dict[str, Any] = {}


class PropertyChanged(BaseModel):
    label: str
    parent_label: str
    kind: ElementKind = ElementKind.PROPERTY
    change_type: ChangeType
    changed_attributes: dict[str, Any] = {}


class DiffReport(BaseModel):
    changes: list[EntityChanged | PropertyChanged]

    @classmethod
    def from_json(cls, json_str: str) -> DiffReport:
        return cls.model_validate_json(json_str)
