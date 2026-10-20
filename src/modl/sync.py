"""Sync engine — processes a DiffReport and updates the six ledger tables.

The engine iterates the ordered list of change events from a :class:`~modl.ir.DiffReport`,
determines whether each change is breaking according to the :class:`~modl.config.BreakingChangeConfig`,
and writes the appropriate rows to the concepts, revisions, contracts, bindings,
revision_aspects, and contract_references tables.

After the events are applied, a reference pass records the contract-to-contract dependency edges
declared by ``PropertyChanged.references`` and cascades a new contract of a referenced
entity/enumeration set onto the properties that reference it, according to the property config.

Usage::

    updated_tables = sync(tables, report, cfg)
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

import pandas as pd

from modl.config import BreakingChangeConfig, ModelMetadata
from modl.ir import ChangeType, DiffReport, EntityChanged, PropertyChanged, _aspect_ops_for_event, extract_op_full
from modl.ledger import b36encode, next_serial
from modl.models import ElementKind, ElementStatus

log = logging.getLogger(__name__)

# Aspect keys for directional instance changes on MODIFIED entity events
_INSTANCES_ADDED_KEY = "instances_added"
_INSTANCES_REMOVED_KEY = "instances_removed"
# Aspect key for full instance list on ADDED entity events
_INSTANCES_SNAPSHOT_KEY = "instances"
# Structural instance keys carry list payloads, not single old/new scalar values — they are
# tracked via the concepts.instances column instead of revision_aspects.
_INSTANCE_STRUCTURAL_KEYS = frozenset({_INSTANCES_SNAPSHOT_KEY, _INSTANCES_ADDED_KEY, _INSTANCES_REMOVED_KEY})

# Kinds a property reference may target (the container namespace)
_CONTAINER_KINDS = frozenset({ElementKind.ENTITY.value, ElementKind.ENUMERATION_SET.value})
# revision_aspects.reason values for changes the engine derives from references
_REASON_REFERENCED_CONTRACT_CHANGED = "referenced-contract-changed"
_REASON_REFERENCE_RETARGETED = "reference-retargeted"


class SyncError(Exception):
    """Raised when the diff report violates a consistency constraint the engine cannot resolve."""


# ── Public API ────────────────────────────────────────────────────────────────


def sync(
    tables: dict[str, pd.DataFrame],
    report: DiffReport,
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
    *,
    strict: bool = False,
) -> dict[str, pd.DataFrame]:
    """Apply a diff report to the ledger tables and return the updated tables.

    The input tables are not modified; the function returns deep copies with all
    changes applied.  The caller is responsible for writing the result to disk.

    Raises :exc:`SyncError` on structural consistency violations (e.g. entity removed
    without corresponding child property REMOVED events).  With *strict*, a property aspect
    whose value names an entity/enumeration set without a matching ``references`` entry also
    raises; otherwise it is logged as a warning.
    """
    # Work on copies so the caller's state is unaffected on error
    tables = {name: df.copy() for name, df in tables.items()}
    contracts_before = set(tables["contracts"]["contract_uri"])
    active_before = _active_contracts_by_concept(tables)

    # Build a set of all labels that appear as REMOVED events for fast consistency checks
    removed_labels: set[str] = {ev.label for ev in report.changes if ev.change_type == ChangeType.REMOVED}

    # Guarantee parent-before-child ordering for ADDED events: entity/enumeration-set
    # concepts must exist before their child property/enum-value concepts are processed,
    # regardless of the order the adapter emitted them.
    # MODIFIED and REMOVED events are kept in their original interleaved order because
    # entity modifications may cascade to child properties that were added earlier in the
    # same run.
    added_entities = [
        ev for ev in report.changes if isinstance(ev, EntityChanged) and ev.change_type == ChangeType.ADDED
    ]
    added_properties = [
        ev for ev in report.changes if isinstance(ev, PropertyChanged) and ev.change_type == ChangeType.ADDED
    ]
    rest = [ev for ev in report.changes if ev.change_type != ChangeType.ADDED]

    for event in added_entities:
        _process_entity(tables, event, metadata, cfg, removed_labels)
    for event in added_properties:
        _process_property(tables, event, metadata, cfg)
    for event in rest:
        if isinstance(event, EntityChanged):
            _process_entity(tables, event, metadata, cfg, removed_labels)
        else:
            _process_property(tables, event, metadata, cfg)

    _apply_references(tables, report, metadata, contracts_before, active_before, strict=strict)
    _cascade_references(tables, metadata, cfg, contracts_before)

    return tables


# ── Event processors ──────────────────────────────────────────────────────────


def _process_entity(
    tables: dict[str, pd.DataFrame],
    event: EntityChanged,
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
    removed_labels: set[str],
) -> None:
    ct = event.change_type

    if ct == ChangeType.ADDED:
        _entity_added(tables, event, metadata, cfg)
    elif ct == ChangeType.MODIFIED:
        _entity_modified(tables, event, metadata, cfg)
    else:
        _entity_removed(tables, event, metadata, cfg, removed_labels)


def _process_property(
    tables: dict[str, pd.DataFrame],
    event: PropertyChanged,
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
) -> None:
    ct = event.change_type

    if ct == ChangeType.ADDED:
        _property_added(tables, event, metadata, cfg)
    elif ct == ChangeType.MODIFIED:
        _property_modified(tables, event, metadata, cfg)
    else:
        _property_removed(tables, event, metadata, cfg)


# ── Entity handlers ───────────────────────────────────────────────────────────


def _entity_added(
    tables: dict[str, pd.DataFrame],
    event: EntityChanged,
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
) -> None:
    instances = event.aspects.get(_INSTANCES_SNAPSHOT_KEY)
    if instances is not None:
        _validate_instances(instances, event.label)
    instances_json = _serialize_instances(instances)

    concept_uri = _mint_concept(
        tables,
        metadata,
        label=event.label,
        kind=event.kind,
        parent_uri=None,
        instances_json=instances_json,
    )
    revision_uri = _mint_revision(
        tables, metadata, concept_uri=concept_uri, prev_revision_uri=None, status=ElementStatus.ACTIVE
    )
    _record_added_aspects(tables, revision_uri, event.aspects)
    _mint_contract(tables, metadata, concept_uri=concept_uri, revision_uri=revision_uri)

    log.info(
        "Entity ADDED:\n  label=%s\n  concept_URI=%s\n  revision_URI=%s",
        event.label,
        concept_uri,
        revision_uri,
    )


def _entity_modified(
    tables: dict[str, pd.DataFrame],
    event: EntityChanged,
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
) -> None:
    lookup_label = event.renamed_from if event.renamed_from is not None else event.label
    concept_row_idx, concept_uri = _require_concept(tables, lookup_label)

    # Build aspect_ops from the event (maps structural+aspect keys to their op strings)
    aspect_ops = _aspect_ops_for_event(event)
    breaking = cfg.is_breaking(event.kind, aspect_ops, renamed_from=event.renamed_from)

    # Evaluate content-derived structural keys (properties.added/removed for ENTITY,
    # values.added/removed for ENUMERATION_SET).  Each content item is evaluated
    # independently so that both "added" and "removed" children in the same event are
    # checked against the config without one overwriting the other.
    if event.content and not breaking:
        content_key = "properties" if event.kind == ElementKind.ENTITY else "values"
        for item in event.content:
            if item.change_type == ChangeType.ADDED and cfg.is_breaking(event.kind, {content_key: "added"}):
                breaking = True
                break
            if item.change_type == ChangeType.REMOVED and cfg.is_breaking(event.kind, {content_key: "removed"}):
                breaking = True
                break

    # Rename
    if event.renamed_from is not None:
        _apply_rename(tables, concept_row_idx, event.label, event.renamed_from)

    # Read directional instance deltas from the event
    instances_added: list[str] = event.aspects.get(_INSTANCES_ADDED_KEY) or []
    instances_removed: list[str] = event.aspects.get(_INSTANCES_REMOVED_KEY) or []
    if instances_added:
        _validate_instances(instances_added, event.label)
    if instances_removed:
        _validate_instances(instances_removed, event.label)
    instances_changed = bool(instances_added or instances_removed)

    # Derive the new complete instance list from the stored one + delta
    old_instances_json: str | None = tables["concepts"].at[concept_row_idx, "instances"]
    old_instances: list[str] = _parse_instances(old_instances_json) or []

    # Reject instances_added values that overlap with the existing instance list
    if instances_added and old_instances:
        overlap = set(instances_added) & set(old_instances)
        if overlap:
            raise SyncError(
                f"[{event.label}] 'instances_added' contains values already present "
                f"in the stored instance list: {sorted(overlap)}"
            )
    new_instances: list[str] = [i for i in old_instances if i not in instances_removed] + instances_added

    # --- Entity revision ---
    prev_rev_uri = _active_revision_uri(tables, concept_uri)
    _supersede_revision(tables, concept_uri)
    revision_uri = _mint_revision(
        tables, metadata, concept_uri=concept_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.ACTIVE
    )
    _record_modified_aspects(tables, revision_uri, event.aspects)

    if breaking:
        # New entity contract
        prev_contract_uri = _active_contract_uri(tables, concept_uri)
        _supersede_contract(tables, concept_uri)
        contract_uri = _mint_contract(tables, metadata, concept_uri=concept_uri, revision_uri=revision_uri)

        log.info(
            "Entity MODIFIED (breaking):\n  label=%s\n  concept_URI=%s"
            "\n  new_revision_URI=%s\n  new_contract_URI=%s\n  prev_contract_URI=%s",
            event.label,
            concept_uri,
            revision_uri,
            contract_uri,
            prev_contract_uri,
        )

        if instances_changed:
            _set_instances(tables, concept_row_idx, new_instances or None)
            # Instance cascade: update bindings only — child property contracts are NOT changed.
            # Old bindings for removed instances are marked REMOVED; new bindings for added
            # instances are appended to each child's existing active contract.
            _cascade_instance_bindings(
                tables,
                metadata,
                entity_concept_uri=concept_uri,
                instances_added=instances_added,
                instances_removed=instances_removed,
            )
        # else: non-instance breaking — entity contract updated; no child cascade
    else:
        log.info(
            "Entity MODIFIED (non-breaking):\n  label=%s\n  concept_URI=%s\n  new_revision_URI=%s",
            event.label,
            concept_uri,
            revision_uri,
        )

        # Non-breaking
        if instances_changed:
            _set_instances(tables, concept_row_idx, new_instances or None)
            # Same binding cascade regardless of breaking/non-breaking:
            # child property contracts are never changed by an instance-list delta.
            _cascade_instance_bindings(
                tables,
                metadata,
                entity_concept_uri=concept_uri,
                instances_added=instances_added,
                instances_removed=instances_removed,
            )


def _entity_removed(
    tables: dict[str, pd.DataFrame],
    event: EntityChanged,
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
    removed_labels: set[str],
) -> None:
    concept_row_idx, concept_uri = _require_concept(tables, event.label)

    # Consistency check: every child PROPERTY concept must have an explicit REMOVED event
    child_df = _child_concepts(tables, concept_uri)
    if not child_df.empty:
        child_labels = set(child_df["current_label"].tolist())
        missing = child_labels - removed_labels
        if missing:
            raise SyncError(
                f"Entity '{event.label}' is REMOVED but child property concepts are missing explicit "
                f"REMOVED events in the diff report: {sorted(missing)}"
            )

    prev_rev_uri = _active_revision_uri(tables, concept_uri)
    _supersede_revision(tables, concept_uri)
    revision_uri = _mint_revision(
        tables, metadata, concept_uri=concept_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.REMOVED
    )
    _record_removed_aspects(tables, revision_uri, event.previous_aspects)

    # All active contracts → REMOVED
    _set_contract_status(tables, concept_uri, ElementStatus.REMOVED)

    # Concept → REMOVED
    tables["concepts"].at[concept_row_idx, "status"] = ElementStatus.REMOVED

    log.info(
        "Entity REMOVED:\n  label=%s\n  concept_URI=%s",
        event.label,
        concept_uri,
    )


# ── Property handlers ─────────────────────────────────────────────────────────


def _effective_instantiate(instantiate: bool | None) -> bool:
    """Resolve a property event's optional ``instantiate`` field to its effective boolean.

    ``None`` (omitted by the adapter) means "inherit the parent's instances" — the
    default, backward-compatible behaviour.  ``False`` means the property exists exactly
    once and must never receive the parent's per-instance expansion.
    """
    return True if instantiate is None else instantiate


def _resolve_property_instances(parent_instances: list[str] | None, instantiate: bool | None) -> list[str] | None:
    """Return the instance list a property concept should store, honouring ``instantiate``.

    Mirrors the parent entity's instance list only when the property is instantiated
    (the default). When ``instantiate`` is explicitly ``False``, the property is pinned
    to a single non-instantiated path and must never inherit the parent's instances.
    """
    if not _effective_instantiate(instantiate):
        return None
    return parent_instances


def _binding_eligible(event: PropertyChanged) -> bool:
    """Resolve whether a PROPERTY event should mint a binding.

    A binding is minted when the property is a leaf (``is_leaf``, the structural default)
    OR when the adapter explicitly marks it eligible via ``binding_eligible`` — e.g. a
    struct-typed property that is read/written as one atomic unit despite referencing
    another entity. This is purely additive: ``binding_eligible`` can never suppress the
    binding a leaf property already receives.
    """
    return bool(event.is_leaf) or event.binding_eligible


def _property_added(
    tables: dict[str, pd.DataFrame],
    event: PropertyChanged,
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
) -> None:
    # Look up parent entity concept for parent_uri and instance list
    parent_idx, parent_uri = _require_concept(tables, event.parent_label)
    parent_instances_json: str | None = tables["concepts"].at[parent_idx, "instances"]
    parent_instances: list[str] | None = _parse_instances(parent_instances_json)

    # A property with instantiate=False is pinned to a single non-instantiated path and
    # must never inherit the parent entity's instance list — see _resolve_property_instances.
    resolved_instances = _resolve_property_instances(parent_instances, event.instantiate)
    resolved_instances_json = _serialize_instances(resolved_instances)

    concept_uri = _mint_concept(
        tables,
        metadata,
        label=event.label,
        kind=event.kind,
        parent_uri=parent_uri,
        instances_json=resolved_instances_json,
    )
    revision_uri = _mint_revision(
        tables, metadata, concept_uri=concept_uri, prev_revision_uri=None, status=ElementStatus.ACTIVE
    )
    _record_added_aspects(tables, revision_uri, event.aspects)
    contract_uri = _mint_contract(tables, metadata, concept_uri=concept_uri, revision_uri=revision_uri)

    # Mint bindings for binding-eligible PROPERTY concepts only — a leaf property always
    # qualifies; a non-leaf property qualifies only when explicitly marked binding_eligible
    # (e.g. a struct-typed property read/written as one atomic unit) — see _binding_eligible.
    if event.kind == ElementKind.PROPERTY and _binding_eligible(event):
        _mint_bindings_for_instances(tables, metadata, contract_uri=contract_uri, instances=resolved_instances)

    log.info(
        "Property ADDED:\n  label=%s\n  concept_URI=%s\n  revision_URI=%s\n  contract_URI=%s",
        event.label,
        concept_uri,
        revision_uri,
        contract_uri,
    )


def _property_modified(
    tables: dict[str, pd.DataFrame],
    event: PropertyChanged,
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
) -> None:
    lookup_label = event.renamed_from if event.renamed_from is not None else event.label
    concept_row_idx, concept_uri = _require_concept(tables, lookup_label, parent_label=event.parent_label)
    aspect_ops = _aspect_ops_for_event(event)

    # A binding-eligibility transition (is_leaf OR binding_eligible) is always breaking,
    # regardless of the config — it changes whether the concept receives a binding at all.
    # "Was this concept previously binding-eligible" is derived from binding-row existence
    # rather than a stored flag, since the ledger is append-only and never deletes binding
    # rows (see _contract_has_any_bindings).
    old_contract_uri = _active_contract_uri(tables, concept_uri) if event.kind == ElementKind.PROPERTY else None
    old_binding_eligible = old_contract_uri is not None and _contract_has_any_bindings(tables, old_contract_uri)
    binding_eligible_changed = event.kind == ElementKind.PROPERTY and old_binding_eligible != _binding_eligible(event)

    # An instantiate transition is likewise always breaking, regardless of the config — it
    # changes whether the property is per-instance-addressable at all. The new resolved
    # instance list is derived from the *current* parent instances plus the event's
    # instantiate field, then compared against what is currently stored on this concept.
    new_instances: list[str] | None = None
    instances_changed = False
    if event.kind == ElementKind.PROPERTY:
        parent_uri = tables["concepts"].at[concept_row_idx, "parent_uri"]
        parent_instances = _instances_by_concept_uri(tables, parent_uri) if parent_uri is not None else None
        new_instances = _resolve_property_instances(parent_instances, event.instantiate)
        old_instances = _parse_instances(tables["concepts"].at[concept_row_idx, "instances"])
        instances_changed = new_instances != old_instances

    breaking = (
        cfg.is_breaking(event.kind, aspect_ops, renamed_from=event.renamed_from)
        or binding_eligible_changed
        or instances_changed
    )

    if event.renamed_from is not None:
        _apply_rename(tables, concept_row_idx, event.label, event.renamed_from)

    prev_rev_uri = _active_revision_uri(tables, concept_uri)
    _supersede_revision(tables, concept_uri)
    revision_uri = _mint_revision(
        tables, metadata, concept_uri=concept_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.ACTIVE
    )
    _record_modified_aspects(tables, revision_uri, event.aspects)

    if breaking:
        _supersede_contract(tables, concept_uri)
        contract_uri = _mint_contract(tables, metadata, concept_uri=concept_uri, revision_uri=revision_uri)

        if event.kind == ElementKind.PROPERTY:
            if instances_changed:
                _set_instances(tables, concept_row_idx, new_instances)
            # Supersede any existing bindings (a safe no-op if the concept never had any),
            # then mint fresh bindings under the new contract only if still binding-eligible.
            instances_json: str | None = tables["concepts"].at[concept_row_idx, "instances"]
            instances = _parse_instances(instances_json)
            _supersede_bindings_by_concept(tables, concept_uri)
            if _binding_eligible(event):
                _mint_bindings_for_instances(tables, metadata, contract_uri=contract_uri, instances=instances)

        log.info(
            "Property MODIFIED (breaking):\n  label=%s\n  concept_URI=%s\n  new_revision_URI=%s\n  new_contract_URI=%s",
            event.label,
            concept_uri,
            revision_uri,
            contract_uri,
        )
    else:
        log.info(
            "Property MODIFIED (non-breaking):\n  label=%s\n  concept_URI=%s\n  new_revision_URI=%s",
            event.label,
            concept_uri,
            revision_uri,
        )


def _property_removed(
    tables: dict[str, pd.DataFrame],
    event: PropertyChanged,
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
) -> None:
    concept_row_idx, concept_uri = _require_concept(tables, event.label, parent_label=event.parent_label)

    prev_rev_uri = _active_revision_uri(tables, concept_uri)
    _supersede_revision(tables, concept_uri)
    revision_uri = _mint_revision(
        tables, metadata, concept_uri=concept_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.REMOVED
    )
    _record_removed_aspects(tables, revision_uri, event.previous_aspects)

    # All active contracts → REMOVED
    _set_contract_status(tables, concept_uri, ElementStatus.REMOVED)

    # All active bindings (PROPERTY kind) → REMOVED
    if tables["concepts"].at[concept_row_idx, "kind"] == ElementKind.PROPERTY:
        _supersede_bindings_by_concept(tables, concept_uri, status=ElementStatus.REMOVED)

    tables["concepts"].at[concept_row_idx, "status"] = ElementStatus.REMOVED

    log.info(
        "Property REMOVED:\n  label=%s\n  concept_URI=%s",
        event.label,
        concept_uri,
    )


# ── Instance cascade helpers ──────────────────────────────────────────────────


def _cascade_instance_bindings(
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    entity_concept_uri: str,
    instances_added: list[str],
    instances_removed: list[str],
) -> None:
    """Update child property bindings for an instance-list change.

    Child property **contracts are never changed** by an instance-list delta — only bindings
    are affected.  This holds regardless of whether the parent entity change is classified as
    breaking or non-breaking:

    - Bindings for *removed* instances are marked ``REMOVED``.
    - New bindings for *added* instances are appended to each child's active contract.
    """
    child_df = _child_concepts(tables, entity_concept_uri)
    if child_df.empty:
        return

    removed_set = set(instances_removed)

    for child_idx in child_df.index.tolist():
        child_idx = int(child_idx)
        child_uri: str = tables["concepts"].at[child_idx, "concept_uri"]
        child_kind: str = tables["concepts"].at[child_idx, "kind"]

        if child_kind != ElementKind.PROPERTY:
            continue

        # A non-instantiated property (its own stored instances is null — e.g. instantiate=False
        # was set when it was added) never mirrors the parent's instance list and never receives
        # per-instance bindings — it keeps its single singleton binding regardless of how the
        # parent's instance list changes.
        if _parse_instances(tables["concepts"].at[child_idx, "instances"]) is None:
            continue

        # Mark bindings for removed instances as REMOVED
        if removed_set:
            active_contracts = set(
                tables["contracts"][
                    (tables["contracts"]["concept_uri"] == child_uri)
                    & (tables["contracts"]["status"] == ElementStatus.ACTIVE)
                ]["contract_uri"]
            )
            mask = (
                tables["bindings"]["contract_uri"].isin(active_contracts)
                & tables["bindings"]["instance_label"].isin(removed_set)
                & (tables["bindings"]["status"] == ElementStatus.ACTIVE)
            )
            tables["bindings"].loc[mask, "status"] = ElementStatus.REMOVED
            child_label: str = tables["concepts"].at[child_idx, "current_label"]
            log.info(
                "  Bindings REMOVED (instance cascade):\n    child=%s\n    instances=%s",
                child_label,
                sorted(removed_set),
            )

        # Append bindings for added instances to the existing active contract — only for
        # leaf properties (a reference property never had bindings, so it gets none here).
        if instances_added:
            active_contract = _active_contract_uri(tables, child_uri)
            if active_contract and _contract_has_any_bindings(tables, active_contract):
                for instance in instances_added:
                    _mint_binding(tables, metadata, contract_uri=active_contract, instance_label=instance)
                child_label = tables["concepts"].at[child_idx, "current_label"]
                log.info(
                    "  Bindings ADDED (instance cascade):\n    child=%s\n    contract_URI=%s\n    instances=%s",
                    child_label,
                    active_contract,
                    instances_added,
                )

        # Keep child concept instances column in sync with the updated parent list
        old_child_instances = _parse_instances(tables["concepts"].at[child_idx, "instances"]) or []
        new_child_instances = [i for i in old_child_instances if i not in removed_set] + instances_added
        _set_instances(tables, child_idx, new_child_instances or None)


# ── Minting helpers ───────────────────────────────────────────────────────────


def _mint_uri(metadata: ModelMetadata, table: str, serial: int) -> str:
    """Build a fully-qualified URI for a new record."""
    return f"{metadata.uri_base(table)}/{b36encode(serial)}"


def _mint_concept(
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    label: str,
    kind: ElementKind,
    parent_uri: str | None,
    instances_json: str | None,
) -> str:
    serial = next_serial(tables["concepts"])
    uri = _mint_uri(metadata, "concepts", serial)
    new_row: dict[str, Any] = {
        "serial": serial,
        "concept_uri": uri,
        "current_label": label,
        "previous_labels": None,
        "kind": kind.value,
        "status": ElementStatus.ACTIVE.value,
        "parent_uri": parent_uri,
        "instances": instances_json,
    }
    tables["concepts"] = pd.concat([tables["concepts"], pd.DataFrame([new_row])], ignore_index=True)
    return uri


def _mint_revision(
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    concept_uri: str,
    prev_revision_uri: str | None,
    status: ElementStatus,
) -> str:
    serial = next_serial(tables["revisions"])
    uri = _mint_uri(metadata, "revisions", serial)
    new_row: dict[str, Any] = {
        "serial": serial,
        "revision_uri": uri,
        "concept_uri": concept_uri,
        "previous_revision_uri": prev_revision_uri,
        "status": status.value,
    }
    tables["revisions"] = pd.concat([tables["revisions"], pd.DataFrame([new_row])], ignore_index=True)
    return uri


def _mint_contract(
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    concept_uri: str,
    revision_uri: str,
) -> str:
    serial = next_serial(tables["contracts"])
    uri = _mint_uri(metadata, "contracts", serial)
    new_row: dict[str, Any] = {
        "serial": serial,
        "contract_uri": uri,
        "concept_uri": concept_uri,
        "revision_uri": revision_uri,
        "status": ElementStatus.ACTIVE.value,
    }
    tables["contracts"] = pd.concat([tables["contracts"], pd.DataFrame([new_row])], ignore_index=True)
    return uri


def _mint_binding(
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    contract_uri: str,
    instance_label: str | None,
) -> str:
    serial = next_serial(tables["bindings"])
    uri = _mint_uri(metadata, "bindings", serial)
    new_row: dict[str, Any] = {
        "serial": serial,
        "binding_uri": uri,
        "contract_uri": contract_uri,
        "instance_label": instance_label,
        "status": ElementStatus.ACTIVE.value,
    }
    tables["bindings"] = pd.concat([tables["bindings"], pd.DataFrame([new_row])], ignore_index=True)
    return uri


def _mint_bindings_for_instances(
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    contract_uri: str,
    instances: list[str] | None,
) -> None:
    """Mint one binding per instance, or a single singleton binding when there are no instances."""
    if instances:
        for inst in instances:
            _mint_binding(tables, metadata, contract_uri=contract_uri, instance_label=inst)
    else:
        _mint_binding(tables, metadata, contract_uri=contract_uri, instance_label=None)


def _serialize_aspect_value(value: Any) -> str | None:
    """JSON-encode an aspect value for storage in revision_aspects; None passes through unchanged."""
    if value is None:
        return None
    return json.dumps(value)


def _append_revision_aspect_row(
    tables: dict[str, pd.DataFrame],
    revision_uri: str,
    aspect_key: str,
    operation: str,
    previous_value: Any,
    newer_value: Any,
    reason: str | None = None,
) -> None:
    """Mint one revision_aspects row for a single changed aspect key."""
    new_row: dict[str, Any] = {
        "revision_uri": revision_uri,
        "aspect_key": aspect_key,
        "operation": operation,
        "previous_value": _serialize_aspect_value(previous_value),
        "newer_value": _serialize_aspect_value(newer_value),
        "reason": reason,
    }
    tables["revision_aspects"] = pd.concat([tables["revision_aspects"], pd.DataFrame([new_row])], ignore_index=True)


def _record_added_aspects(tables: dict[str, pd.DataFrame], revision_uri: str, aspects: dict[str, Any]) -> None:
    """Mint one revision_aspects row per key in an ADDED event's initial-state snapshot."""
    for key, value in aspects.items():
        if key in _INSTANCE_STRUCTURAL_KEYS:
            continue
        _, new_val, _ = extract_op_full(value)
        _append_revision_aspect_row(
            tables, revision_uri, aspect_key=key, operation="added", previous_value=None, newer_value=new_val
        )


def _record_modified_aspects(tables: dict[str, pd.DataFrame], revision_uri: str, aspects: dict[str, Any]) -> None:
    """Mint one revision_aspects row per key in a MODIFIED event's aspect delta."""
    for key, value in aspects.items():
        if key in _INSTANCE_STRUCTURAL_KEYS:
            continue
        op, new_val, prev_val = extract_op_full(value)
        _append_revision_aspect_row(
            tables, revision_uri, aspect_key=key, operation=op, previous_value=prev_val, newer_value=new_val
        )


def _record_removed_aspects(
    tables: dict[str, pd.DataFrame], revision_uri: str, previous_aspects: dict[str, Any]
) -> None:
    """Mint one revision_aspects row per key in a REMOVED event's prior-state snapshot."""
    for key, value in previous_aspects.items():
        if key in _INSTANCE_STRUCTURAL_KEYS:
            continue
        _append_revision_aspect_row(
            tables, revision_uri, aspect_key=key, operation="removed", previous_value=value, newer_value=None
        )


# ── Reference edges and cascade ───────────────────────────────────────────────


def _active_contracts_by_concept(tables: dict[str, pd.DataFrame]) -> dict[str, str]:
    """Map each concept_uri to its ACTIVE contract URI."""
    df = tables["contracts"]
    active = df[df["status"] == ElementStatus.ACTIVE]
    return dict(zip(active["concept_uri"], active["contract_uri"], strict=True))


def _concept_of_contract(tables: dict[str, pd.DataFrame], contract_uri: str) -> str:
    df = tables["contracts"]
    return str(df.loc[df["contract_uri"] == contract_uri, "concept_uri"].iloc[0])


def _edges_for_contract(tables: dict[str, pd.DataFrame], contract_uri: str) -> dict[str, str]:
    """Return ``{role: target_contract_uri}`` for the edges stored on a contract."""
    df = tables["contract_references"]
    rows = df[df["contract_uri"] == contract_uri]
    return dict(zip(rows["role"], rows["target_contract_uri"], strict=True))


def _write_edges(tables: dict[str, pd.DataFrame], contract_uri: str, edges: dict[str, str]) -> None:
    if not edges:
        return
    rows = [{"contract_uri": contract_uri, "role": r, "target_contract_uri": t} for r, t in edges.items()]
    tables["contract_references"] = pd.concat([tables["contract_references"], pd.DataFrame(rows)], ignore_index=True)


def _resolve_reference_target(tables: dict[str, pd.DataFrame], label: str, owner: str) -> str:
    """Return the concept_uri of the ACTIVE entity/enumeration set labelled *label*."""
    df = tables["concepts"]
    is_target = (
        (df["current_label"] == label) & df["kind"].isin(_CONTAINER_KINDS) & (df["status"] == ElementStatus.ACTIVE)
    )
    match = df[is_target]
    if match.empty:
        raise SyncError(f"[{owner}] references target '{label}' is not an active ENTITY or ENUMERATION_SET")
    return str(match.iloc[0]["concept_uri"])


def _require_active_contract(tables: dict[str, pd.DataFrame], concept_uri: str, owner: str) -> str:
    contract_uri = _active_contract_uri(tables, concept_uri)
    if contract_uri is None:
        raise SyncError(f"[{owner}] references a concept that has no active contract")
    return contract_uri


def _bump_property_contract(
    tables: dict[str, pd.DataFrame], metadata: ModelMetadata, concept_uri: str, revision_uri: str
) -> str:
    """Supersede a property's contract and bindings, then mint a new contract (and bindings if it had any)."""
    old_contract_uri = _active_contract_uri(tables, concept_uri)
    had_bindings = old_contract_uri is not None and _contract_has_any_bindings(tables, old_contract_uri)
    _supersede_contract(tables, concept_uri)
    new_contract_uri = _mint_contract(tables, metadata, concept_uri, revision_uri)
    _supersede_bindings_by_concept(tables, concept_uri)
    if had_bindings:
        instances = _instances_by_concept_uri(tables, concept_uri)
        _mint_bindings_for_instances(tables, metadata, new_contract_uri, instances)
    return new_contract_uri


def _has_aspect_row(tables: dict[str, pd.DataFrame], revision_uri: str, aspect_key: str) -> bool:
    df = tables["revision_aspects"]
    return bool(((df["revision_uri"] == revision_uri) & (df["aspect_key"] == aspect_key)).any())


def _missing_reference_warnings(event: PropertyChanged, declared: set[str], container_labels: set[str]) -> list[str]:
    """Warn about aspects whose value names an entity/enumeration set but that declare no reference."""
    out: list[str] = []
    for key, value in event.aspects.items():
        if key in _INSTANCE_STRUCTURAL_KEYS or key in declared:
            continue
        op, new_val, _ = extract_op_full(value)
        if op != "removed" and isinstance(new_val, str) and new_val in container_labels:
            out.append(
                f"[{event.label}] aspect '{key}' names '{new_val}' but no references.{key} entry was declared; "
                "a change to that type will not cascade"
            )
    return out


def _apply_references(
    tables: dict[str, pd.DataFrame],
    report: DiffReport,
    metadata: ModelMetadata,
    contracts_before: set[str],
    active_before: dict[str, str],
    *,
    strict: bool,
) -> None:
    """Record reference edges on contracts minted this run and force a contract when a reference is retargeted.

    Declared ``references`` on ADDED/MODIFIED property events are authoritative.  Contracts minted
    for any other reason (an aspect change, an instance cascade) carry their previous edges forward,
    re-pointed at the current ACTIVE contract of each target.
    """
    minted = set(tables["contracts"]["contract_uri"]) - contracts_before
    concepts = tables["concepts"]
    container_mask = concepts["kind"].isin(_CONTAINER_KINDS) & (concepts["status"] == ElementStatus.ACTIVE)
    container_labels = set(concepts[container_mask]["current_label"])
    warnings: list[str] = []
    handled: set[str] = set()

    for event in report.changes:
        if (
            not isinstance(event, PropertyChanged)
            or event.kind != ElementKind.PROPERTY
            or event.change_type == ChangeType.REMOVED
        ):
            continue
        try:
            _, concept_uri = _require_concept(tables, event.label, parent_label=event.parent_label)
        except SyncError:
            continue  # superseded within the same report (e.g. renamed by a later event)
        contract_uri = _active_contract_uri(tables, concept_uri)
        if contract_uri is None:
            continue
        prev_contract_uri = active_before.get(concept_uri)
        prev_edges = _edges_for_contract(tables, prev_contract_uri) if prev_contract_uri else {}
        declared = set(prev_edges) if event.references is None else set(event.references)
        warnings.extend(_missing_reference_warnings(event, declared, container_labels))

        if event.references is None:
            continue  # edges are carried forward below when the contract was re-minted

        new_concepts = {
            role: _resolve_reference_target(tables, label, event.label) for role, label in event.references.items()
        }
        old_concepts = {role: _concept_of_contract(tables, t) for role, t in prev_edges.items()}
        retargeted = new_concepts != old_concepts
        new_edges = {role: _require_active_contract(tables, c, event.label) for role, c in new_concepts.items()}

        revision_uri = _active_revision_uri(tables, concept_uri)
        if contract_uri not in minted and retargeted and revision_uri is not None:
            contract_uri = _bump_property_contract(tables, metadata, concept_uri, revision_uri)
            minted.add(contract_uri)
        if contract_uri in minted:
            _write_edges(tables, contract_uri, new_edges)
        handled.add(concept_uri)

        if event.change_type == ChangeType.MODIFIED and revision_uri is not None:
            for role in old_concepts.keys() | new_concepts.keys():
                if old_concepts.get(role) == new_concepts.get(role) or _has_aspect_row(tables, revision_uri, role):
                    continue
                old_edge, new_edge = prev_edges.get(role), new_edges.get(role)
                operation = "added" if old_edge is None else "removed" if new_edge is None else "modified"
                _append_revision_aspect_row(
                    tables, revision_uri, role, operation, old_edge, new_edge, reason=_REASON_REFERENCE_RETARGETED
                )

    # Carry edges onto contracts re-minted for other reasons (aspect change, instance cascade).
    for concept_uri, contract_uri in _active_contracts_by_concept(tables).items():
        prev_contract_uri = active_before.get(concept_uri)
        if contract_uri not in minted or concept_uri in handled or prev_contract_uri is None:
            continue
        carried: dict[str, str] = {}
        for role, target_contract_uri in _edges_for_contract(tables, prev_contract_uri).items():
            target_concept_uri = _concept_of_contract(tables, target_contract_uri)
            carried[role] = _active_contract_uri(tables, target_concept_uri) or target_contract_uri
        _write_edges(tables, contract_uri, carried)

    if warnings:
        if strict:
            raise SyncError("\n".join(warnings))
        for w in warnings:
            log.warning("%s", w)


def _cascade_references(
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    cfg: BreakingChangeConfig,
    contracts_before: set[str],
) -> None:
    """Re-contract properties whose referenced contract was replaced this run, per the property config.

    A reference role cascades when the property config marks that key as breaking for ``modified``.
    Targets are entities/enumeration sets only and a re-contracted property is never a target, so a
    single pass reaches a fixed point.
    """
    if tables["contract_references"].empty:
        return
    contracts = tables["contracts"]
    concept_by_contract = dict(zip(contracts["contract_uri"], contracts["concept_uri"], strict=True))
    status_by_contract = dict(zip(contracts["contract_uri"], contracts["status"], strict=True))
    minted = set(contracts["contract_uri"]) - contracts_before
    active = _active_contracts_by_concept(tables)

    stale: dict[str, list[tuple[str, str, str]]] = {}
    edge_rows = tables["contract_references"]
    for source, role, target in zip(
        edge_rows["contract_uri"], edge_rows["role"], edge_rows["target_contract_uri"], strict=True
    ):
        if status_by_contract[source] != ElementStatus.ACTIVE:
            continue
        new_target = active.get(concept_by_contract[target])
        if new_target is None or new_target == target or new_target not in minted:
            continue
        stale.setdefault(source, []).append((role, target, new_target))

    concepts = tables["concepts"]
    for contract_uri, changes in stale.items():
        concept_uri = concept_by_contract[contract_uri]
        label = str(concepts.loc[concepts["concept_uri"] == concept_uri, "current_label"].iloc[0])
        if not any(cfg.is_breaking(ElementKind.PROPERTY, {role: "modified"}) for role, _, _ in changes):
            log.warning(
                "[%s] referenced contract changed for role(s) %s but the property config does not mark them "
                "breaking; contract not re-minted",
                label,
                sorted(role for role, _, _ in changes),
            )
            continue

        prev_revision_uri = _active_revision_uri(tables, concept_uri)
        _supersede_revision(tables, concept_uri)
        revision_uri = _mint_revision(
            tables, metadata, concept_uri=concept_uri, prev_revision_uri=prev_revision_uri, status=ElementStatus.ACTIVE
        )
        for role, old_target, new_target in changes:
            _append_revision_aspect_row(
                tables,
                revision_uri,
                role,
                "modified",
                old_target,
                new_target,
                reason=_REASON_REFERENCED_CONTRACT_CHANGED,
            )
        new_contract_uri = _bump_property_contract(tables, metadata, concept_uri, revision_uri)
        replaced = {role: new_target for role, _, new_target in changes}
        edges = {role: replaced.get(role, t) for role, t in _edges_for_contract(tables, contract_uri).items()}
        _write_edges(tables, new_contract_uri, edges)
        log.info("[%s] re-contracted: referenced contract changed for %s", label, sorted(replaced))


# ── Mutation helpers ──────────────────────────────────────────────────────────


def _supersede_revision(tables: dict[str, pd.DataFrame], concept_uri: str) -> None:
    """Mark the current ACTIVE revision for a concept as SUPERSEDED."""
    mask = (tables["revisions"]["concept_uri"] == concept_uri) & (tables["revisions"]["status"] == ElementStatus.ACTIVE)
    tables["revisions"].loc[mask, "status"] = ElementStatus.SUPERSEDED


def _supersede_contract(tables: dict[str, pd.DataFrame], concept_uri: str) -> None:
    """Mark the current ACTIVE contract for a concept as SUPERSEDED."""
    mask = (tables["contracts"]["concept_uri"] == concept_uri) & (tables["contracts"]["status"] == ElementStatus.ACTIVE)
    tables["contracts"].loc[mask, "status"] = ElementStatus.SUPERSEDED


def _set_contract_status(
    tables: dict[str, pd.DataFrame],
    concept_uri: str,
    status: ElementStatus,
) -> None:
    """Set all ACTIVE contracts for a concept to the given status."""
    mask = (tables["contracts"]["concept_uri"] == concept_uri) & (tables["contracts"]["status"] == ElementStatus.ACTIVE)
    tables["contracts"].loc[mask, "status"] = status


def _supersede_bindings_by_concept(
    tables: dict[str, pd.DataFrame],
    concept_uri: str,
    status: ElementStatus = ElementStatus.SUPERSEDED,
) -> None:
    """Mark all ACTIVE bindings associated with any contract of the given concept."""
    contract_uris = set(tables["contracts"][tables["contracts"]["concept_uri"] == concept_uri]["contract_uri"])
    if not contract_uris:
        return
    mask = tables["bindings"]["contract_uri"].isin(contract_uris) & (
        tables["bindings"]["status"] == ElementStatus.ACTIVE
    )
    tables["bindings"].loc[mask, "status"] = status


def _apply_rename(
    tables: dict[str, pd.DataFrame],
    concept_idx: int,
    new_label: str,
    old_label: str,
) -> None:
    """Update current_label and prepend old_label to previous_labels."""
    raw = tables["concepts"].at[concept_idx, "previous_labels"]
    existing: list[str] = _parse_previous_labels(raw)
    if old_label not in existing:
        existing.insert(0, old_label)
    col = "previous_labels"
    if tables["concepts"][col].dtype != object:
        tables["concepts"][col] = tables["concepts"][col].astype(object)
    tables["concepts"].at[concept_idx, col] = json.dumps(existing)
    tables["concepts"].at[concept_idx, "current_label"] = new_label


def _set_instances(
    tables: dict[str, pd.DataFrame],
    concept_idx: int,
    instances: list[str] | None,
) -> None:
    col = "instances"
    # An all-null column starts as float64; cast to object so strings can be stored.
    if tables["concepts"][col].dtype != object:
        tables["concepts"][col] = tables["concepts"][col].astype(object)
    tables["concepts"].at[concept_idx, col] = _serialize_instances(instances)


# ── Query helpers ─────────────────────────────────────────────────────────────


def _require_concept(tables: dict[str, pd.DataFrame], label: str, parent_label: str | None = None) -> tuple[int, str]:
    """Return the DataFrame index and concept_uri for a concept with the given current_label.

    ``current_label`` is scoped to two independent namespaces (see module docs): ENTITY/
    ENUMERATION_SET labels are globally unique, while PROPERTY/ENUM_VALUE labels are unique only
    among siblings sharing the same parent. Pass *parent_label* to scope the lookup to a specific
    parent's children — required whenever the target may be a PROPERTY or ENUM_VALUE, since those
    labels alone do not guarantee a unique match. *parent_label* itself is resolved via an
    unscoped lookup, since ENTITY/ENUMERATION_SET labels are always globally unique.

    Raises :exc:`SyncError` if no matching concept is found.
    """
    df = tables["concepts"]
    if parent_label is not None:
        _, parent_uri = _require_concept(tables, parent_label)
        match = df[(df["current_label"] == label) & (df["parent_uri"] == parent_uri)]
        if match.empty:
            raise SyncError(f"No concept found with current_label '{label}' under parent '{parent_label}'")
    else:
        match = df[df["current_label"] == label]
        if match.empty:
            raise SyncError(f"No concept found with current_label '{label}'")
    idx = int(match.index[0])
    return idx, str(match.iloc[0]["concept_uri"])


def _active_revision_uri(tables: dict[str, pd.DataFrame], concept_uri: str) -> str | None:
    """Return the URI of the current ACTIVE revision for a concept, or None."""
    df = tables["revisions"]
    mask = (df["concept_uri"] == concept_uri) & (df["status"] == ElementStatus.ACTIVE)
    active = df[mask]
    if active.empty:
        return None
    return str(active.iloc[0]["revision_uri"])


def _active_contract_uri(tables: dict[str, pd.DataFrame], concept_uri: str) -> str | None:
    """Return the URI of the current ACTIVE contract for a concept, or None."""
    df = tables["contracts"]
    mask = (df["concept_uri"] == concept_uri) & (df["status"] == ElementStatus.ACTIVE)
    active = df[mask]
    if active.empty:
        return None
    return str(active.iloc[0]["contract_uri"])


def _contract_has_any_bindings(tables: dict[str, pd.DataFrame], contract_uri: str) -> bool:
    """Return whether any binding row (regardless of status) was ever minted for this contract.

    Used to recover whether a property concept was previously "leaf" (binding-eligible)
    without persisting a separate flag on concepts.csv — the ledger is append-only and
    binding rows are never deleted (only superseded/removed via their ``status`` column),
    so binding-row existence already encodes the fact.
    """
    return bool((tables["bindings"]["contract_uri"] == contract_uri).any())


def _child_concepts(tables: dict[str, pd.DataFrame], parent_uri: str) -> pd.DataFrame:
    """Return all concept rows whose parent_uri matches the given entity concept URI."""
    df = tables["concepts"]
    return df[df["parent_uri"] == parent_uri]


def _instances_by_concept_uri(tables: dict[str, pd.DataFrame], concept_uri: str) -> list[str] | None:
    """Return the parsed instance list stored on the concept row with the given concept_uri."""
    df = tables["concepts"]
    match = df[df["concept_uri"] == concept_uri]
    if match.empty:
        return None
    return _parse_instances(match.iloc[0]["instances"])


# ── Serialisation helpers ─────────────────────────────────────────────────────


def _parse_instances(value: Any) -> list[str] | None:
    """Deserialise a JSON-encoded instance list from the CSV; returns None for missing/null values."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, list):
        return value
    return json.loads(value)  # type: ignore[no-any-return]


def _serialize_instances(instances: list[str] | None) -> str | None:
    """Serialise an instance list to a JSON string for storage in the CSV."""
    if not instances:
        return None
    return json.dumps(instances)


def _validate_instances(instances: Any, label: str) -> None:
    """Raise :exc:`SyncError` when *instances* is not a flat, duplicate-free list of strings.

    Catches nested lists, dicts, numbers, and other non-string elements that indicate
    a non-compliant diff adapter (e.g. multi-dimensional instance arrays).  Also rejects
    duplicate values within the list.
    """
    if not isinstance(instances, list):
        raise SyncError(f"[{label}] 'instances' must be a list of strings, got {type(instances).__name__!r}")
    bad = [i for i, v in enumerate(instances) if not isinstance(v, str)]
    if bad:
        bad_types = [type(instances[i]).__name__ for i in bad]
        raise SyncError(
            f"[{label}] 'instances' must be a flat list of strings — "
            f"element(s) at index {bad} are not strings (got {bad_types}). "
            "Multi-dimensional instances must be flattened by the adapter before reaching the diff report."
        )
    seen: set[str] = set()
    dupes = [v for v in instances if v in seen or seen.add(v)]  # type: ignore[func-returns-value]
    if dupes:
        raise SyncError(f"[{label}] 'instances' contains duplicate values: {sorted(set(dupes))}")


def _parse_previous_labels(value: Any) -> list[str]:
    """Deserialise previous_labels from the CSV; returns empty list for missing/null values."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    if isinstance(value, list):
        return value
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
