"""Breaking change configuration and model metadata, loaded from YAML files."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from modl.models import ElementKind

# ── Structural keys ────────────────────────────────────────────────────────────
# These keys are derived by the engine from event fields (not from the aspects dict).
# The engine always recognises them — they never produce an "unknown key" warning even
# if absent from the config — but their breaking classification must still be declared
# explicitly by the user.
#
# Valid operations per key:
#   name.modified          — rename (renamed_from set on the event)
#   properties.added/removed — child property added/removed under an ENTITY
#   values.added/removed   — ENUM_VALUE added/removed under an ENUMERATION_SET
#   instances.added/removed — instance label added/removed on an ENTITY

# Directional instance keys emitted by _aspect_ops_for_event → canonical config key ("instances")
_INSTANCE_KEY_TRANSLATIONS: dict[str, tuple[str, str]] = {
    "instances_added": ("instances", "added"),
    "instances_removed": ("instances", "removed"),
}

_STRUCTURAL_KEYS: dict[ElementKind, frozenset[str]] = {
    ElementKind.ENTITY: frozenset(
        {
            "name.modified",
            "properties.added",
            "properties.removed",
            "instances.added",
            "instances.removed",
            # Directional forms emitted by _aspect_ops_for_event (recognised so they never warn)
            "instances_added",
            "instances_removed",
        }
    ),
    ElementKind.ENUMERATION_SET: frozenset(
        {
            "name.modified",
            "values.added",
            "values.removed",
        }
    ),
    ElementKind.PROPERTY: frozenset({"name.modified"}),
    ElementKind.ENUM_VALUE: frozenset({"name.modified"}),
}

# Valid op suffixes when a key uses the dotted form (e.g. ``unit.added``)
_VALID_OPS: frozenset[str] = frozenset({"added", "removed", "modified"})


def structural_keys(kind: ElementKind) -> frozenset[str]:
    """Return the set of structural config keys for the given element kind."""
    return _STRUCTURAL_KEYS.get(kind, frozenset())


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


def _validate_section(section: dict[str, bool], section_name: str) -> None:
    """Raise ValueError for any invalid key in a breaking-change config section.

    Rules enforced:
    - A key ending in ``.added`` or ``.removed`` is forbidden when the base key is ``name``
      (those operations are structurally impossible — an element always has exactly one label).
    - When a key contains a dot, the suffix after the last dot must be one of
      ``added``, ``removed``, or ``modified``.  Any other suffix (e.g. ``unit.replaced``) is
      rejected with a descriptive error.
    - The plain key ``name`` (without an op suffix) is forbidden; use ``name.modified`` instead.
    """
    for key in section:
        parts = key.rsplit(".", 1)
        if len(parts) == 2:
            base, op = parts
            if op not in _VALID_OPS:
                raise ValueError(
                    f"[{section_name}] Invalid op suffix '{op}' in key '{key}'. "
                    f"Expected one of: {', '.join(sorted(_VALID_OPS))}"
                )
            if base == "name" and op in {"added", "removed"}:
                raise ValueError(
                    f"[{section_name}] Key '{key}' is invalid. "
                    "'name.added' and 'name.removed' are not meaningful — an element always "
                    "has exactly one label. Use 'name.modified' to configure rename behaviour."
                )
        else:
            if key == "name":
                raise ValueError(
                    f"[{section_name}] Plain key 'name' is not allowed. "
                    "Use 'name.modified' to configure rename behaviour."
                )


class BreakingChangeConfig(BaseModel):
    """Per-kind breaking change rules for a modl project.

    Each of the four sections (``entity``, ``property``, ``enumeration_set``, ``enum_value``)
    is a flat mapping from a key to a boolean:

    - ``true``  — the change is **breaking**; triggers a new contract.
    - ``false`` — the change is **known but non-breaking**; silently accepted.
    - *absent*  — the change is **unknown**; produces a warning (error with ``--strict``).

    Keys use a flat dotted form to express per-operation classification::

        unit: true               # shorthand — any op (added/removed/modified) is breaking
        unit.added: true         # only gaining a unit for the first time is breaking
        unit.modified: true      # changing the unit value is breaking
        unit.removed: false      # dropping a unit annotation is non-breaking

    A plain value is shorthand for all three operations having the same classification.
    The granular dotted form takes precedence over the shorthand when both are present.

    **Structural keys** — derived by the engine from event fields rather than from the
    ``aspects`` dict.  They are always recognised (never produce an unknown-key warning)
    but their breaking classification must still be declared:

    +---------------------------+----------------------------------------------+
    | Key                       | Meaning                                      |
    +===========================+==============================================+
    | ``name.modified``         | Rename (``renamed_from`` set on the event)   |
    +---------------------------+----------------------------------------------+
    | ``properties.added``      | Child property added under an ENTITY         |
    +---------------------------+----------------------------------------------+
    | ``properties.removed``    | Child property removed from an ENTITY        |
    +---------------------------+----------------------------------------------+
    | ``values.added``          | ENUM_VALUE added to an ENUMERATION_SET       |
    +---------------------------+----------------------------------------------+
    | ``values.removed``        | ENUM_VALUE removed from an ENUMERATION_SET   |
    +---------------------------+----------------------------------------------+
    | ``instances.added``       | Instance label added to an ENTITY            |
    +---------------------------+----------------------------------------------+
    | ``instances.removed``     | Instance label removed from an ENTITY        |
    +---------------------------+----------------------------------------------+

    ``name.added`` and ``name.removed`` are forbidden — those operations do not exist at
    the aspect level.  The plain key ``name`` is also forbidden; use ``name.modified``.

    Example::

        entity:
          name.modified: false      # renames are non-breaking
          properties.added: false   # adding a property is non-breaking
          properties.removed: true  # removing a property is breaking
          instances.added: false    # adding an instance is non-breaking
          instances.removed: true   # removing an instance is breaking
          type: true                # any change to 'type' aspect is breaking

        property:
          name.modified: false
          output_type: true
          unit.added: true
          unit.modified: true
          unit.removed: false       # dropping a unit annotation is non-breaking
          description: false

        enumeration_set:
          name.modified: false
          values.added: false
          values.removed: true

        enum_value:
          name.modified: true       # renaming a value is breaking (consumers match on string)
          symbol: true
    """

    model_config = ConfigDict(extra="forbid")

    entity: dict[str, bool] = Field(default_factory=dict)
    property: dict[str, bool] = Field(default_factory=dict)
    enumeration_set: dict[str, bool] = Field(default_factory=dict)
    enum_value: dict[str, bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_all_sections(self) -> BreakingChangeConfig:
        _validate_section(self.entity, "entity")
        _validate_section(self.property, "property")
        _validate_section(self.enumeration_set, "enumeration_set")
        _validate_section(self.enum_value, "enum_value")
        return self

    def _section(self, kind: ElementKind) -> dict[str, bool]:
        """Return the config section for the given element kind."""
        return {
            ElementKind.ENTITY: self.entity,
            ElementKind.ENUMERATION_SET: self.enumeration_set,
            ElementKind.PROPERTY: self.property,
            ElementKind.ENUM_VALUE: self.enum_value,
        }[kind]

    def _lookup(self, cfg: dict[str, bool], base_key: str, op: str) -> bool | None:
        """Look up the classification for a (key, op) pair.

        Precedence: ``key.op`` (granular) > ``key`` (shorthand) > absent (``None``).
        """
        granular = cfg.get(f"{base_key}.{op}")
        if granular is not None:
            return granular
        return cfg.get(base_key)

    def is_breaking(self, kind: ElementKind, aspect_ops: dict[str, str], renamed_from: str | None = None) -> bool:
        """Indicate whether a MODIFIED event constitutes a breaking change.

        *aspect_ops* maps each changed aspect key to the operation that occurred
        (``"added"``, ``"removed"``, or ``"modified"``).  Structural keys such as
        ``"properties"``, ``"instances"``, and ``"values"`` should be included with their
        directional op when applicable.  The directional instance keys ``"instances_added"``
        and ``"instances_removed"`` (emitted by the engine) are translated to the canonical
        ``instances`` config key automatically.

        Returns ``True`` when any declared breaking rule fires.  Unknown keys (absent from
        config and not structural) are treated as non-breaking.
        """
        cfg = self._section(kind)

        # Rename maps to name.modified
        if renamed_from is not None and self._lookup(cfg, "name", "modified") is True:
            return True

        for base_key, op in aspect_ops.items():
            canonical_key, canonical_op = _INSTANCE_KEY_TRANSLATIONS.get(base_key, (base_key, op))
            if self._lookup(cfg, canonical_key, canonical_op) is True:
                return True
        return False

    def unknown_keys(self, kind: ElementKind, aspect_ops: dict[str, str], renamed_from: str | None = None) -> list[str]:
        """Return aspect keys that are undeclared in the config and not structural.

        These are the keys that trigger warnings (or errors with ``--strict``).
        Each returned string is the bare key name (without op suffix).
        """
        cfg = self._section(kind)
        structural = structural_keys(kind)
        unknown: list[str] = []

        if renamed_from is not None:
            qualified = "name.modified"
            if qualified not in structural and self._lookup(cfg, "name", "modified") is None:
                unknown.append("name.modified")

        for base_key, op in aspect_ops.items():
            qualified = f"{base_key}.{op}"
            if qualified in structural or f"{base_key}" in structural:
                continue
            if self._lookup(cfg, base_key, op) is None:
                unknown.append(qualified)

        return sorted(set(unknown))

    @classmethod
    def from_yaml(cls, path: Path) -> BreakingChangeConfig:
        """Load and validate a BreakingChangeConfig from a YAML file."""
        raw = yaml.safe_load(path.read_text())
        if raw is None:
            raw = {}
        return cls.model_validate(raw)


# ── Adaptation config ─────────────────────────────────────────────────────────


class StepKind(StrEnum):
    """Valid adaptation kind identifiers for adaptation plan steps.

    Each value names a transformation *class* (Level 2 — adaptation).
    Execution parameters (Level 3 — recipe) are declared separately in
    the ``recipe`` block of each :class:`AdaptationStep`.

    ``rename`` is auto-emitted for renamed fields and never needs to be
    declared in the adaptation config.  ``round`` is the only kind the engine
    inspects for lossiness classification.
    """

    RENAME = "rename"
    SCALE = "scale"
    ROUND = "round"
    CAST = "cast"
    LOOKUP = "lookup"
    DEFAULT = "default"
    NEST = "nest"
    EXTRACT = "extract"
    MAP = "map"


# Kinds that require recipe parameters to produce an executable plan.
# Steps of these kinds with no recipe → recipe_status: incomplete.
_RECIPE_REQUIRED_KINDS: frozenset[StepKind] = frozenset(
    {
        StepKind.SCALE,
        StepKind.ROUND,
        StepKind.LOOKUP,
        StepKind.DEFAULT,
    }
)


class AdaptationBlock(BaseModel):
    """Adaptation kind declaration for a single step (Level 2).

    Only ``kind`` is stored here — execution parameters belong in the sibling
    ``recipe`` block of :class:`AdaptationStep`.
    """

    model_config = ConfigDict(extra="forbid")
    kind: StepKind


class AdaptationStep(BaseModel):
    """One step in an adaptation pipeline (Level 2 + Level 3).

    ``adaptation`` declares which transformation class to apply.
    ``recipe`` carries the execution parameters (optional).  Its structure
    depends on the step kind:

    - ``scale`` / ``lookup``: a **list** of ``{source, target, ...}`` rows,
      matched at runtime against the actual aspect values from the diff.
    - ``round``: a **dict** with a ``policy`` key (``floor`` / ``ceil`` /
      ``round`` / ``trunc``).
    - ``default``: a **dict** with a ``default_value`` key.
    - All other kinds: recipe is optional; any dict provided is passed through.

    Recipe completeness is validated at config load time (structure / type only).
    Whether a matching row exists for a given ``{source, target}`` pair is
    determined at adapt time.
    """

    model_config = ConfigDict(extra="forbid")
    adaptation: AdaptationBlock
    recipe: dict[str, Any] | list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _validate_recipe_structure(self) -> AdaptationStep:
        """Enforce per-kind recipe structural constraints."""
        kind = self.adaptation.kind
        recipe = self.recipe
        if recipe is None:
            return self
        if kind == StepKind.ROUND:
            if not isinstance(recipe, dict):
                raise ValueError("'round' recipe must be a dict (e.g. {policy: floor})")
            policy = recipe.get("policy")
            if policy is not None and policy not in {"floor", "ceil", "round", "trunc"}:
                raise ValueError(f"'round' recipe policy must be floor / ceil / round / trunc, got '{policy}'")
        elif kind in (StepKind.SCALE, StepKind.LOOKUP):
            if not isinstance(recipe, list):
                raise ValueError(f"'{kind}' recipe must be a list of source/target rows")
        elif kind == StepKind.DEFAULT:
            if not isinstance(recipe, dict):
                raise ValueError("'default' recipe must be a dict with a 'default_value' key")
        return self


class AspectRule(BaseModel):
    """Step pipeline declared for a single aspect key in the adaptation config."""

    model_config = ConfigDict(extra="forbid")
    steps: list[AdaptationStep]


class AdaptationConfig(BaseModel):
    """Per-kind adaptation rules for the ``modl adapt`` command.

    The adaptation config encodes **Level 2** (adaptation kind) and **Level 3** (recipe
    parameters) of the three-level separation used by ``modl adapt``:

    - **Level 1 — change**: observed facts from the diff (automatic, no config needed).
    - **Level 2 — adaptation**: which transformation class addresses this change
      (``kind`` declared in the ``adaptation`` block of each step).
    - **Level 3 — recipe**: execution parameters for the transformation (declared in
      the ``recipe`` block of each step; may be omitted initially and added later).

    Each element-kind section maps aspect keys to an ordered list of
    :class:`AdaptationStep` objects.  Keys use the same dotted form as the
    breaking-change config (``unit.modified``, ``datatype.modified``, or a plain
    ``unit`` as shorthand for all ops).  Every key declared here **must** resolve
    to a breaking aspect in the corresponding ``breaking-aspects.yaml`` — validated
    by :meth:`validate_against_breaking_config` at run time.

    **No built-in defaults** are applied.  Every aspect key that requires adaptation
    must be declared explicitly here.

    Example ``adaptation.yaml``::

        property:
          unit.modified:
            steps:
              - adaptation:
                  kind: scale
                recipe:
                  - source: km/h
                    target: mph
                    factor: 1.60934

          datatype.modified:
            steps:
              - adaptation:
                  kind: cast
              - adaptation:
                  kind: round
                recipe:
                  policy: floor

        enum_value:
          symbol.modified:
            steps:
              - adaptation:
                  kind: lookup
                recipe:
                  - source: KMH
                    target: KILOMETRES_PER_HOUR
    """

    model_config = ConfigDict(extra="forbid")

    entity: dict[str, AspectRule] = Field(default_factory=dict)
    property: dict[str, AspectRule] = Field(default_factory=dict)
    enumeration_set: dict[str, AspectRule] = Field(default_factory=dict)
    enum_value: dict[str, AspectRule] = Field(default_factory=dict)

    def _section(self, kind: ElementKind) -> dict[str, AspectRule]:
        """Return the adaptation rule section for the given element kind."""
        return {
            ElementKind.ENTITY: self.entity,
            ElementKind.ENUMERATION_SET: self.enumeration_set,
            ElementKind.PROPERTY: self.property,
            ElementKind.ENUM_VALUE: self.enum_value,
        }[kind]

    def steps_for(self, kind: ElementKind, key: str) -> list[AdaptationStep]:
        """Return the step pipeline for a (element kind, aspect key) pair.

        Resolution order:
        1. Exact dotted key in user config (e.g. ``unit.modified``).
        2. Plain base key in user config (e.g. ``unit``).
        3. Empty list — no rule declared.

        No built-in defaults are applied.  All adaptation rules must be declared
        explicitly in the config file.
        """
        section = self._section(kind)
        base = key.rsplit(".", 1)[0] if "." in key else key
        rule = section.get(key) or section.get(base)
        return rule.steps if rule else []

    def validate_against_breaking_config(self, breaking_cfg: BreakingChangeConfig) -> list[str]:
        """Return error strings for adaptation keys that don't correspond to breaking aspects.

        Every key in the adaptation config must resolve to at least one op that is
        declared breaking (``true``) in the breaking-change config.  Keys covering ops
        that are non-breaking or undeclared are rejected (exact op-form match required).

        A dotted key (e.g. ``unit.modified``) covers exactly one op.
        A plain key (e.g. ``unit``) covers all three ops (added / modified / removed).
        """
        errors: list[str] = []
        sections: list[tuple[str, ElementKind]] = [
            ("entity", ElementKind.ENTITY),
            ("property", ElementKind.PROPERTY),
            ("enumeration_set", ElementKind.ENUMERATION_SET),
            ("enum_value", ElementKind.ENUM_VALUE),
        ]
        for section_name, element_kind in sections:
            adapt_section = self._section(element_kind)
            for config_key in adapt_section:
                parts = config_key.rsplit(".", 1)
                if len(parts) == 2 and parts[1] in _VALID_OPS:
                    base_key, op = parts
                    ops_to_check: list[tuple[str, str]] = [(base_key, op)]
                else:
                    base_key = config_key
                    ops_to_check = [(base_key, "added"), (base_key, "modified"), (base_key, "removed")]

                for base, op in ops_to_check:
                    val = breaking_cfg._lookup(breaking_cfg._section(element_kind), base, op)
                    if val is not True:
                        state = "non-breaking" if val is False else "undeclared"
                        errors.append(
                            f"[{section_name}] '{config_key}' covers op '{base}.{op}' which is "
                            f"{state} in the breaking-aspects config — only breaking aspects can have adaptation rules"
                        )
        return errors

    @classmethod
    def from_yaml(cls, path: Path) -> AdaptationConfig:
        """Load and validate an AdaptationConfig from a YAML file."""
        raw = yaml.safe_load(path.read_text())
        if raw is None:
            raw = {}
        return cls.model_validate(raw)
