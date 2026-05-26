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


class BreakingChangeConfig(BaseModel):
    """Root configuration for a modl project, combining namespace settings and per-kind breaking change rules.

    ``entity`` and ``property`` are flat mappings from aspect key name to a boolean:

    - ``true``  — the aspect is **breaking**; a change triggers a new variant.
    - ``false`` — the aspect is **known but non-breaking**; changes are silently accepted.
    - *absent*  — the aspect is **unknown**; changes produce a warning (or error with ``--strict``).

    The reserved key ``name`` governs rename events (``renamed_from`` set on a change event).
    It never appears in ``aspects`` on a diff event — it is checked via *renamed_from* only.

    Example::

        entity:
          instances: true   # breaking
          type: true        # breaking
          name: false       # renames are non-breaking, suppress warnings

        property:
          output_type: true  # breaking
          unit: true         # breaking
          description: false # known, non-breaking — suppress warnings
    """

    model_config = ConfigDict(extra="forbid")

    namespace: NamespaceConfig
    entity: dict[str, bool] = Field(default_factory=dict)
    property: dict[str, bool] = Field(default_factory=dict)

    def is_breaking(self, kind: ElementKind, aspects: dict[str, Any], renamed_from: str | None = None) -> bool:
        """Indicate whether a MODIFIED event constitutes a breaking change for the given element kind.

        Returns ``True`` when any of the following holds:

        - *renamed_from* is set and ``name`` maps to ``true`` in the config for that kind.
        - Any key in *aspects* maps to ``true`` in the config for that kind.
        """
        cfg = self.entity if kind == ElementKind.ENTITY else self.property
        if renamed_from is not None and cfg.get("name") is True:
            return True
        return any(cfg.get(aspect) is True for aspect in aspects)

    @classmethod
    def from_yaml(cls, path: Path) -> BreakingChangeConfig:
        """Load and validate a BreakingChangeConfig from a YAML file."""
        raw = yaml.safe_load(path.read_text())
        return cls.model_validate(raw)
