"""Breaking change configuration and namespace settings, loaded from a YAML file."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from modl.models import ElementKind


class NamespaceConfig(BaseModel):
    """Base URI and optional display prefix for a modl project.

    The full URI is always used in stored ledger records. The prefix is a
    display-only alias for use in inspection and query output.

    The namespace must end with '/' or '#' so that URIs are formed by direct
    concatenation: ``namespace + table + '/' + id``.
    """

    model_config = ConfigDict(extra="forbid")

    namespace: str
    prefix: str | None = None

    @field_validator("namespace")
    @classmethod
    def namespace_must_be_valid(cls, v: str) -> str:
        """Reject namespace strings that are not absolute URIs ending with '/' or '#'."""
        if " " in v:
            raise ValueError("namespace must not contain spaces")
        parsed = urlparse(v)
        if not parsed.scheme or not (parsed.netloc or parsed.path):
            raise ValueError("namespace must be an absolute URI (e.g. http://example.org/model/)")
        if not v.endswith(("/", "#")):
            raise ValueError("namespace must end with '/' or '#'")
        return v

    def uri_base(self, table: str) -> str:
        """Return the full base URI for minting identifiers in the given table (e.g. 'http://namespace.example/concepts')."""
        return f"{self.namespace}{table}"


class ElementBreakingConfig(BaseModel):
    """Attributes whose change triggers a new variant (i.e., a breaking change) for an element kind."""

    model_config = ConfigDict(extra="forbid")

    essential_attributes: list[str] = Field(default_factory=list)


class BreakingChangeConfig(BaseModel):
    """Root configuration for a modl project, combining namespace settings and per-kind breaking change rules."""

    model_config = ConfigDict(extra="forbid")

    namespace: NamespaceConfig
    entity: ElementBreakingConfig = Field(default_factory=ElementBreakingConfig)
    property: ElementBreakingConfig = Field(default_factory=ElementBreakingConfig)

    def is_breaking(self, kind: ElementKind, changed_attributes: dict[str, Any]) -> bool:
        """Indicate whether any changed attribute is essential for the given element kind."""
        cfg = self.entity if kind == ElementKind.ENTITY else self.property
        return any(attr in cfg.essential_attributes for attr in changed_attributes)

    @classmethod
    def from_yaml(cls, path: Path) -> BreakingChangeConfig:
        """Load and validate a BreakingChangeConfig from a YAML file."""
        raw = yaml.safe_load(path.read_text())
        return cls.model_validate(raw)
