"""Intermediate representation (IR) for model diff events consumed by the sync engine.

A diff report describes what changed between two snapshots of a domain model.  It contains
an ordered list of change events — one per modified entity, property, enumeration set, or
enum value.  The sync engine processes these events to update the ledger tables.

Terminology
-----------
Entity (a.k.a. container, object type, branch, class, feature of interest)
    A top-level model element that groups properties.  In GraphQL SDL this is a ``type``; in
    vspec it is a ``branch`` node.

Property (a.k.a. field, attribute, signal, characteristic, aspect)
    A named attribute that belongs to exactly one entity.  In GraphQL SDL this is a ``field``
    inside a ``type``; in vspec it is a leaf node (``sensor``, ``actuator``, etc.).

EnumerationSet
    A vocabulary entity — an enum type, unit group, or code list.  Use ``kind: ENUMERATION_SET``
    on :class:`EntityChanged` events.  Receives concept URIs, revisions, and contracts, but no
    bindings.

EnumValue
    A child of an EnumerationSet — an individual enum member or unit entry.  Use
    ``kind: ENUM_VALUE`` on :class:`PropertyChanged` events.  Receives concept URIs, revisions,
    and contracts, but no bindings.

Aspect
    Any named attribute of a model element that can change.  Every change is reported in the
    ``aspects`` dict.  The engine uses this dict together with the breaking-change config to
    decide whether a new data contract is warranted.

    One canonical aspect key exists for entity events:

    - ``instances`` — full list of instance labels on **ADDED** events.
      On **MODIFIED** events use the directional split:
      ``instances_added`` and ``instances_removed`` (lists of labels that appeared or disappeared).

    For property events no canonical keys are prescribed — ``output_type``, ``is_list``, and
    ``is_required`` are widely used conventions for typed modeling languages but are treated
    as adapter-defined keys and must be declared in the breaking-change config if relevant.

    All other keys are *adapter-defined*: the language adapter decides their names (e.g.
    ``"unit"``, ``"symbol"``, ``"description"``).  The breaking-change config references them
    by their exact key name (dotted paths such as ``"arg.unit.type"`` are valid flat strings).

Operation annotation (MODIFIED events only)
--------------------------------------------
By default, a key present in a MODIFIED event's ``aspects`` dict is treated as having the
operation ``"modified"`` (value changed).  Adapters that can determine the exact operation
may wrap the value to be more specific::

    "aspects": {
        "unit": {"_op": "added", "_value": "mph"},       # key appeared for the first time
        "accuracy": {"_op": "removed"},                  # key was dropped
        "description": {"_op": "modified", "_value": "new text"}  # value changed
    }

Plain values (not wrapped) remain valid and default to op ``"modified"``.  This is an
opt-in extension — adapters that cannot distinguish "appeared" from "changed" emit plain
values and the engine evaluates them against the ``modified`` rule only.

Reserved aspect keys
--------------------
The key ``"name"`` is **forbidden** in the ``aspects`` dict of any event.  Renames are
signalled exclusively via the ``renamed_from`` field.  The engine maps ``renamed_from``
internally to the config key ``name.modified``.

The keys ``"instances_added"`` and ``"instances_removed"`` are reserved for MODIFIED entity
events to carry the directional instance-list diff (see above).  The plain ``"instances"``
key is reserved for ADDED entity events only.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, model_validator

from modl.models import ElementKind

if TYPE_CHECKING:
    from modl.config import BreakingChangeConfig

# Reserved aspect keys that may not be used in aspects dicts freely
_RESERVED_ASPECT_KEYS: frozenset[str] = frozenset({"name"})

# Keys reserved on MODIFIED entity events for directional instance diff
_INSTANCE_DELTA_KEYS: frozenset[str] = frozenset({"instances_added", "instances_removed"})

# Key used in ADDED entity events for the full instance snapshot
_INSTANCES_SNAPSHOT_KEY = "instances"


class ChangeType(StrEnum):
    """Type of change detected on a model element."""

    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"


class ContentItem(BaseModel):
    """Reference to a child element that changed within a container's content list."""

    label: str
    change_type: ChangeType


class DiffReportValidationError(Exception):
    """Raised when a diff report violates structural or configuration constraints."""


def extract_op(value: Any) -> tuple[str, Any]:
    """Return ``(op, actual_value)`` from an aspect value.

    Plain values (not operation-annotated) return ``("modified", value)``.
    Annotated values — a ``dict`` with an ``"_op"`` key — return ``(op, _value)``.
    ``"_value"`` may be absent for ``"removed"`` operations.
    """
    if isinstance(value, dict) and "_op" in value:
        op: str = value["_op"]
        actual = value.get("_value")
        return op, actual
    return "modified", value


def extract_aspect_ops(aspects: dict[str, Any]) -> dict[str, str]:
    """Return a mapping of aspect key → operation derived from an aspects dict.

    Each value is passed through :func:`extract_op`; the resulting op string is collected.
    """
    return {key: extract_op(val)[0] for key, val in aspects.items()}


class EntityChanged(BaseModel):
    """A detected change on an entity or enumeration set.

    Set ``kind`` to ``ENUMERATION_SET`` for vocabulary container elements (enum types,
    unit groups, code lists).  The ledger records concept URIs, revisions, and contracts
    for both kinds, but suppresses binding minting for ``ENUMERATION_SET``.

    Payload rules by ``change_type``:

    - ``ADDED``: ``aspects`` holds the full initial-state snapshot.  Use ``instances`` to
      carry the list of instance labels if applicable.  ``content`` and ``renamed_from``
      must be absent.
    - ``MODIFIED``: ``aspects`` carries only the keys that changed (delta).  Use
      ``instances_added`` and ``instances_removed`` (not ``instances``) to report instance
      list changes.  ``renamed_from`` is set when the element was renamed.  ``content``
      lists the children that changed.
    - ``REMOVED``: ``aspects`` and ``content`` must be empty; ``renamed_from`` must be absent.

    The key ``"name"`` is forbidden in ``aspects`` — use ``renamed_from`` for renames.
    """

    label: str
    kind: ElementKind = ElementKind.ENTITY
    change_type: ChangeType
    renamed_from: str | None = None
    aspects: dict[str, Any] = {}
    content: list[ContentItem] = []

    @model_validator(mode="after")
    def _validate_constraints(self) -> EntityChanged:
        if self.kind not in {ElementKind.ENTITY, ElementKind.ENUMERATION_SET}:
            raise ValueError(f"EntityChanged.kind must be ENTITY or ENUMERATION_SET, got {self.kind!r}")
        if self.change_type == ChangeType.REMOVED and (self.aspects or self.content):
            raise ValueError("REMOVED events must not carry aspects or content")
        if self.change_type == ChangeType.ADDED and self.content:
            raise ValueError("ADDED events must not carry content")
        if self.renamed_from is not None and self.change_type != ChangeType.MODIFIED:
            raise ValueError("renamed_from is only valid on MODIFIED events")
        if "name" in self.aspects:
            raise ValueError("The key 'name' is forbidden in aspects. Signal renames via the 'renamed_from' field.")
        if self.change_type == ChangeType.MODIFIED and _INSTANCES_SNAPSHOT_KEY in self.aspects:
            raise ValueError(
                "The key 'instances' is not valid on MODIFIED entity events. "
                "Use 'instances_added' and 'instances_removed' to report directional instance changes."
            )
        return self


class PropertyChanged(BaseModel):
    """A detected change on a property or enum value.

    Set ``kind`` to ``ENUM_VALUE`` for vocabulary leaf elements (enum members, unit entries).
    The ledger records concept URIs, revisions, and contracts for both kinds, but suppresses
    binding minting for ``ENUM_VALUE``.

    Payload rules by ``change_type``:

    - ``ADDED``: ``aspects`` holds the full initial-state snapshot.  ``renamed_from`` must
      be absent.
    - ``MODIFIED``: ``aspects`` carries only the keys that changed (delta).  ``renamed_from``
      is set when the element was renamed.
    - ``REMOVED``: ``aspects`` must be empty; ``renamed_from`` must be absent.

    The key ``"name"`` is forbidden in ``aspects`` — use ``renamed_from`` for renames.
    """

    label: str
    parent_label: str
    kind: ElementKind = ElementKind.PROPERTY
    change_type: ChangeType
    renamed_from: str | None = None
    aspects: dict[str, Any] = {}

    @model_validator(mode="after")
    def _validate_constraints(self) -> PropertyChanged:
        if self.kind not in {ElementKind.PROPERTY, ElementKind.ENUM_VALUE}:
            raise ValueError(f"PropertyChanged.kind must be PROPERTY or ENUM_VALUE, got {self.kind!r}")
        if self.change_type == ChangeType.REMOVED and self.aspects:
            raise ValueError("REMOVED events must not carry aspects")
        if self.renamed_from is not None and self.change_type != ChangeType.MODIFIED:
            raise ValueError("renamed_from is only valid on MODIFIED events")
        if "name" in self.aspects:
            raise ValueError("The key 'name' is forbidden in aspects. Signal renames via the 'renamed_from' field.")
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


def _aspect_ops_for_event(change: EntityChanged | PropertyChanged) -> dict[str, str]:
    """Build an aspect_ops dict for a MODIFIED event, including structural synthetic keys.

    For entity events, ``instances_added`` and ``instances_removed`` in the aspects dict
    are mapped to the structural keys ``instances.added`` / ``instances.removed`` and their
    ops are set to ``"added"`` / ``"removed"`` respectively.

    For all events, the plain aspects are passed through :func:`extract_aspect_ops`.
    """
    raw_ops = extract_aspect_ops(change.aspects)
    result: dict[str, str] = {}

    if isinstance(change, EntityChanged):
        # Map directional instance keys to their structural equivalents
        if "instances_added" in raw_ops:
            result["instances"] = "added"
        if "instances_removed" in raw_ops:
            result["instances"] = "removed"
        # Keep other non-instance keys
        for key, op in raw_ops.items():
            if key not in _INSTANCE_DELTA_KEYS:
                result[key] = op
    else:
        result = raw_ops

    return result


def validate_report_aspects(
    report: DiffReport,
    config: BreakingChangeConfig,
    *,
    strict: bool = False,
) -> list[str]:
    """Check that all aspect keys in MODIFIED events are declared in the breaking-change config.

    Two categories of warnings are produced:

    1. **Section absent** — the config has no rules at all for a kind that appears in the
       report.  One warning is emitted per kind, not per event.
    2. **Key absent** — the config section exists but a specific key is not declared.  One
       warning is emitted per undeclared key per event.

    Unknown keys are treated as non-breaking.  Set *strict* to ``True`` to raise
    :exc:`DiffReportValidationError` instead of returning warnings.

    Returns a list of warning strings (empty when all keys are known).
    """
    warnings: list[str] = []
    warned_empty_sections: set[ElementKind] = set()

    for change in report.changes:
        if change.change_type != ChangeType.MODIFIED:
            continue

        kind = change.kind
        section = config._section(kind)  # noqa: SLF001

        # Section-level warning: no rules configured at all for this kind
        if not section:
            if kind not in warned_empty_sections:
                warned_empty_sections.add(kind)
                warnings.append(
                    f"No breaking-aspect rules configured for '{kind.value.lower()}'. "
                    "All changes will be treated as non-breaking."
                )
            continue  # skip per-key warnings when the whole section is absent

        # Per-key warnings: key not declared and not structural
        aspect_ops = _aspect_ops_for_event(change)
        unknown = config.unknown_keys(kind, aspect_ops, renamed_from=change.renamed_from)
        for qualified_key in unknown:
            warnings.append(
                f"[{change.label}] Aspect '{qualified_key}' is not declared in the "
                f"breaking-change config for '{kind.value.lower()}' — treated as non-breaking"
            )

    if strict and warnings:
        raise DiffReportValidationError("\n".join(warnings))
    return warnings
