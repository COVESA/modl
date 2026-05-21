from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from modl.models import ElementKind

_TABLE_SUFFIXES: dict[str, str] = {
    "concepts": "-c",
    "revisions": "-r",
    "variants": "-v",
    "bindings": "-b",
}


class NamespaceConfig(BaseModel):
    namespace: str
    prefix: str | None = None

    def uri_base(self, table: str) -> str:
        """Return the URI base for a given table name (e.g. 'concepts' → 'mp-c')."""
        suffix = _TABLE_SUFFIXES[table]
        root = self.prefix if self.prefix is not None else self.namespace
        return f"{root}{suffix}"


class ElementBreakingConfig(BaseModel):
    essential_attributes: list[str] = Field(default_factory=list)


class BreakingChangeConfig(BaseModel):
    namespace: NamespaceConfig
    entity: ElementBreakingConfig = Field(default_factory=ElementBreakingConfig)
    property: ElementBreakingConfig = Field(default_factory=ElementBreakingConfig)

    def is_breaking(self, kind: ElementKind, changed_attributes: dict[str, Any]) -> bool:
        """Indicate whether any changed attribute is essential for the given element kind."""
        cfg = self.entity if kind == ElementKind.ENTITY else self.property
        return any(attr in cfg.essential_attributes for attr in changed_attributes)

    @classmethod
    def from_yaml(cls, path: Path) -> BreakingChangeConfig:
        raw = yaml.safe_load(path.read_text())
        return cls.model_validate(raw)
