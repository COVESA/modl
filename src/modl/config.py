"""Breaking change configuration and model metadata, loaded from YAML files."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from modl.models import ElementKind


class ModelMetadata(BaseModel):
    """Identity metadata for a data model project, following the s2dm metadata.yaml convention.

    ``id`` is the namespace URI used to mint ledger record identifiers. It must be an
    absolute URI ending with ``/`` or ``#`` so that URIs are formed by direct concatenation:
    ``id + table + '/' + serial``.

    ``name`` is the human-readable model name. It is accepted for s2dm compatibility but not
    used internally by modl.

    ``preferred_prefix`` is an optional display alias used by inspection and query output.
    It is never stored in ledger records.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    id: str
    preferred_prefix: str | None = None

    @field_validator("id")
    @classmethod
    def id_must_be_valid_namespace(cls, v: str) -> str:
        """Reject id values that are not absolute URIs ending with '/' or '#'."""
        if " " in v:
            raise ValueError("id must not contain spaces")
        parsed = urlparse(v)
        if not parsed.scheme or not (parsed.netloc or parsed.path):
            raise ValueError("id must be an absolute URI (e.g. http://example.org/model/)")
        if not v.endswith(("/", "#")):
            raise ValueError("id must end with '/' or '#'")
        return v

    def uri_base(self, table: str) -> str:
        """Return the full base URI for minting identifiers in the given table (e.g. 'http://namespace.example/concepts')."""
        return f"{self.id}{table}"

    @classmethod
    def from_yaml(cls, path: Path) -> ModelMetadata:
        """Load and validate a ModelMetadata from a YAML file."""
        raw = yaml.safe_load(path.read_text())
        return cls.model_validate(raw)


class BreakingChangeConfig(BaseModel):
    """Per-kind breaking change rules for a modl project.

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
