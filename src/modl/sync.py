"""Sync engine — processes a DiffReport and updates the six ledger tables.

The engine iterates the ordered list of change events from a :class:`~modl.ir.DiffReport`,
determines whether each change is breaking according to the :class:`~modl.config.BreakingChangeConfig`,
and writes the appropriate rows to the concepts, revisions, contracts, bindings,
revision_aspects, and contract_references tables.

After the events are applied, a reconciliation pass compares the report's ``property_paths`` with the
ledger and mints, removes or refreshes bindings; the engine never derives bindings itself. A reference
pass then records the contract-to-contract dependency edges declared by ``PropertyChanged.references``
and cascades a new contract of a referenced entity/enumeration set onto the properties that reference
it, according to the property config.

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
from modl.ir import (
    ChangeType,
    DiffReport,
    EntityChanged,
    PathChange,
    PropertyChanged,
    PropertyPaths,
    _aspect_ops_for_event,
    extract_op_full,
)
from modl.ledger import LedgerValidationError, b36encode, canonical_tags, next_serial, validate_ledger
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
    bindings_before = _active_binding_index(tables)
    delta_entity_uris = _entities_with_instance_delta(tables, report)
    eligible_keys = _validate_report_paths(report)

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

    _reconcile_property_paths(
        tables, metadata, report, bindings_before, active_before, delta_entity_uris, eligible_keys
    )
    _apply_references(tables, report, metadata, contracts_before, active_before, strict=strict)
    _cascade_references(tables, metadata, cfg, contracts_before)

    try:
        validate_ledger(tables)
    except LedgerValidationError as exc:
        raise SyncError(f"Diff report produces an invalid ledger: {exc}") from exc

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
    if instances is not None and event.kind != ElementKind.ENTITY:
        raise SyncError(f"[{event.label}] 'instances' is only valid on ENTITY events")
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
    instances_added: dict[str, list[str]] = event.aspects.get(_INSTANCES_ADDED_KEY) or {}
    instances_removed: dict[str, list[str]] = event.aspects.get(_INSTANCES_REMOVED_KEY) or {}
    instances_changed = bool(instances_added or instances_removed)
    if instances_changed and event.kind != ElementKind.ENTITY:
        raise SyncError(f"[{event.label}] instance deltas are only valid on ENTITY events")

    # Derive the new complete instance dictionary from the stored one + delta
    old_instances: dict[str, list[str]] = _parse_instances(tables["concepts"].at[concept_row_idx, "instances"]) or {}
    new_instances = _apply_instance_delta(event.label, old_instances, instances_added, instances_removed)

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
        # Entity contract updated; bindings of dependent properties follow the report's property_paths.
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
    # Look up parent entity concept for parent_uri
    _, parent_uri = _require_concept(tables, event.parent_label)

    concept_uri = _mint_concept(
        tables,
        metadata,
        label=event.label,
        kind=event.kind,
        parent_uri=parent_uri,
        instances_json=None,
    )
    revision_uri = _mint_revision(
        tables, metadata, concept_uri=concept_uri, prev_revision_uri=None, status=ElementStatus.ACTIVE
    )
    _record_added_aspects(tables, revision_uri, event.aspects)
    contract_uri = _mint_contract(tables, metadata, concept_uri=concept_uri, revision_uri=revision_uri)
    # Bindings are minted later from the report's property_paths.

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

    breaking = cfg.is_breaking(event.kind, aspect_ops, renamed_from=event.renamed_from) or binding_eligible_changed

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
            # Supersede existing bindings (a safe no-op if the concept never had any); the
            # report's property_paths re-mint them under the new contract.
            _supersede_bindings_by_concept(tables, concept_uri)
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


_PropertyKey = tuple[str, str]  # (parent_label, label)


def _active_binding_index(tables: dict[str, pd.DataFrame]) -> dict[str, dict[str, str]]:
    """Map each property concept_uri to ``{canonical instance_tags: binding_uri}`` of its ACTIVE bindings."""
    bindings = tables["bindings"]
    active = bindings[bindings["status"] == ElementStatus.ACTIVE]
    contracts = tables["contracts"]
    concept_by_contract = dict(zip(contracts["contract_uri"], contracts["concept_uri"], strict=True))
    index: dict[str, dict[str, str]] = {}
    for contract_uri, tags, binding_uri in zip(
        active["contract_uri"], active["instance_tags"], active["binding_uri"], strict=True
    ):
        index.setdefault(concept_by_contract[contract_uri], {})[tags] = binding_uri
    return index


def _entities_with_instance_delta(tables: dict[str, pd.DataFrame], report: DiffReport) -> set[str]:
    """Return the concept_uris of existing entities whose MODIFIED event reports an instance delta."""
    uris: set[str] = set()
    for ev in report.changes:
        if not isinstance(ev, EntityChanged) or ev.change_type != ChangeType.MODIFIED:
            continue
        if not (ev.aspects.get(_INSTANCES_ADDED_KEY) or ev.aspects.get(_INSTANCES_REMOVED_KEY)):
            continue
        try:
            _, uri = _require_concept(tables, ev.renamed_from or ev.label)
        except SyncError:
            continue
        uris.add(uri)
    return uris


def _validate_report_paths(report: DiffReport) -> dict[_PropertyKey, bool]:
    """Check the ``property_paths`` of a report in isolation; return binding eligibility per property event.

    Raises :exc:`SyncError` when a binding-eligible ADDED/MODIFIED property has no entry, an entry
    targets a property its event declares not binding-eligible, entries or runtime paths repeat, or
    tags repeat within an entry.
    """
    eligibility: dict[_PropertyKey, bool] = {}
    for ev in report.changes:
        if isinstance(ev, PropertyChanged) and ev.kind == ElementKind.PROPERTY and ev.change_type != ChangeType.REMOVED:
            eligibility[(ev.parent_label, ev.label)] = _binding_eligible(ev)

    seen: set[_PropertyKey] = set()
    owner_by_path: dict[str, str] = {}
    for pp in report.property_paths:
        key = (pp.parent_label, pp.label)
        if key in seen:
            raise SyncError(f"[{pp.label}] duplicate property_paths entry under parent '{pp.parent_label}'")
        seen.add(key)
        if eligibility.get(key) is False:
            raise SyncError(f"[{pp.label}] has property_paths but its event is not binding-eligible")
        tag_sets: set[str] = set()
        for entry in pp.paths:
            canon = canonical_tags(entry.tags)
            if canon in tag_sets:
                raise SyncError(f"[{pp.label}] duplicate instance tags in property_paths: {canon}")
            tag_sets.add(canon)
            if entry.change != PathChange.REMOVED:
                if entry.path in owner_by_path:
                    raise SyncError(
                        f"[{pp.label}] runtime_path '{entry.path}' is also reported for '{owner_by_path[entry.path]}'"
                    )
                owner_by_path[entry.path] = pp.label

    missing = sorted(
        label for (parent, label), eligible in eligibility.items() if eligible and (parent, label) not in seen
    )
    if missing:
        raise SyncError(f"Binding-eligible properties have no property_paths entry: {missing}")
    return eligibility


def _resolve_entry_tags(
    tables: dict[str, pd.DataFrame], tags: dict[str, dict[str, str]], owner: str, *, check_instances: bool
) -> dict[str, dict[str, str]]:
    """Translate report tags keyed by entity label into tags keyed by entity concept_uri.

    With *check_instances*, every dimension/instance must also exist on the entity's stored instances.
    """
    concepts = tables["concepts"]
    resolved: dict[str, dict[str, str]] = {}
    for label, dims in tags.items():
        match = concepts[
            (concepts["current_label"] == label)
            & (concepts["kind"] == ElementKind.ENTITY.value)
            & (concepts["status"] == ElementStatus.ACTIVE.value)
        ]
        if match.empty:
            raise SyncError(f"[{owner}] tags reference unknown or inactive entity '{label}'")
        if check_instances:
            known = _parse_instances(match.iloc[0]["instances"]) or {}
            for dim, value in dims.items():
                if value not in known.get(dim, []):
                    raise SyncError(f"[{owner}] tag {label}.{dim}='{value}' is not an instance declared by the entity")
        resolved[str(match.iloc[0]["concept_uri"])] = dict(dims)
    return resolved


def _shape(tags: dict[str, dict[str, str]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the dimension signature of one binding's tags (entity -> dimension names)."""
    return tuple(sorted((uri, tuple(sorted(dims))) for uri, dims in tags.items()))


def _reconcile_property_paths(
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    report: DiffReport,
    bindings_before: dict[str, dict[str, str]],
    active_before: dict[str, str],
    delta_entity_uris: set[str],
    event_eligibility: dict[_PropertyKey, bool],
) -> None:
    """Cross-check the reported paths against the ledger and mint, remove or refresh bindings."""
    reported: set[str] = set()
    for pp in report.property_paths:
        uri = _reconcile_one(tables, metadata, pp, bindings_before, active_before, event_eligibility)
        reported.add(uri)

    # An instance delta on an entity requires a fresh path set for every property tagged with it.
    concepts = tables["concepts"].set_index("concept_uri")
    for prop_uri, index in bindings_before.items():
        if prop_uri in reported or concepts.at[prop_uri, "status"] != ElementStatus.ACTIVE.value:
            continue
        used = {entity for canon in index for entity in json.loads(canon)}
        if used & delta_entity_uris:
            raise SyncError(
                f"[{concepts.at[prop_uri, 'current_label']}] depends on an entity with an instance change "
                "but has no property_paths entry"
            )


def _reconcile_one(
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    pp: PropertyPaths,
    bindings_before: dict[str, dict[str, str]],
    active_before: dict[str, str],
    event_eligibility: dict[_PropertyKey, bool],
) -> str:
    """Reconcile one property's reported paths; return its concept_uri."""
    _, uri = _require_concept(tables, pp.label, parent_label=pp.parent_label)
    concepts = tables["concepts"]
    row = concepts[concepts["concept_uri"] == uri].iloc[0]
    if row["kind"] != ElementKind.PROPERTY.value or row["status"] != ElementStatus.ACTIVE.value:
        raise SyncError(f"[{pp.label}] property_paths target is not an ACTIVE PROPERTY")

    old = bindings_before.get(uri, {})
    if not old and not event_eligibility.get((pp.parent_label, pp.label), False):
        raise SyncError(f"[{pp.label}] has property_paths but is not binding-eligible")

    groups: dict[PathChange, dict[str, tuple[dict[str, dict[str, str]], str]]] = {c: {} for c in PathChange}
    for entry in pp.paths:
        tags = _resolve_entry_tags(tables, entry.tags, pp.label, check_instances=entry.change != PathChange.REMOVED)
        groups[entry.change][canonical_tags(tags)] = (tags, entry.path)

    reported_old = set(groups[PathChange.UNCHANGED]) | set(groups[PathChange.REMOVED])
    if reported_old != set(old):
        raise SyncError(
            f"[{pp.label}] reported unchanged/removed paths do not match the ledger's ACTIVE bindings: "
            f"missing from report={sorted(set(old) - reported_old)}, "
            f"unknown to ledger={sorted(reported_old - set(old))}"
        )

    keep = {**groups[PathChange.UNCHANGED], **groups[PathChange.ADDED]}
    old_shapes = {_shape(json.loads(c)) for c in old}
    new_shapes = {_shape(tags) for tags, _ in keep.values()}

    contract_uri = _active_contract_uri(tables, uri)
    if contract_uri is None:
        raise SyncError(f"[{pp.label}] has no active contract")
    bumped = active_before.get(uri) != contract_uri
    if not bumped and old_shapes and new_shapes and old_shapes != new_shapes:
        revision_uri = _active_revision_uri(tables, uri)
        if revision_uri is None:
            raise SyncError(f"[{pp.label}] has no active revision")
        contract_uri = _bump_property_contract(tables, metadata, uri, revision_uri, rebind=False)
        bumped = True
        log.info("Property paths changed dimensions:\n  label=%s\n  new_contract_URI=%s", pp.label, contract_uri)

    if bumped:
        for tags, path in keep.values():
            _mint_binding(tables, metadata, contract_uri, tags, path)
        return uri

    for tags, path in groups[PathChange.ADDED].values():
        _mint_binding(tables, metadata, contract_uri, tags, path)
    removed_uris = {old[canon] for canon in groups[PathChange.REMOVED]}
    bindings = tables["bindings"]
    bindings.loc[bindings["binding_uri"].isin(removed_uris), "status"] = ElementStatus.REMOVED.value
    for canon, (_, path) in groups[PathChange.UNCHANGED].items():
        mask = bindings["binding_uri"] == old[canon]
        if (bindings.loc[mask, "runtime_path"] != path).any():
            bindings.loc[mask, "runtime_path"] = path
    return uri


def _apply_instance_delta(
    label: str,
    old: dict[str, list[str]],
    added: dict[str, list[str]],
    removed: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Apply a directional instance delta to a stored dimension dictionary."""
    new = {dim: list(values) for dim, values in old.items()}
    for dim, values in removed.items():
        current = new.get(dim, [])
        missing = [v for v in values if v not in current]
        if missing:
            raise SyncError(f"[{label}] 'instances_removed' dimension '{dim}' has values not stored: {missing}")
        new[dim] = [v for v in current if v not in values]
    for dim, values in added.items():
        current = new.get(dim, [])
        overlap = sorted(set(values) & set(current))
        if overlap:
            raise SyncError(f"[{label}] 'instances_added' dimension '{dim}' has values already stored: {overlap}")
        new[dim] = current + values
    return {dim: values for dim, values in new.items() if values}


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
    instance_tags: dict[str, dict[str, str]],
    runtime_path: str,
) -> str:
    serial = next_serial(tables["bindings"])
    uri = _mint_uri(metadata, "bindings", serial)
    new_row: dict[str, Any] = {
        "serial": serial,
        "binding_uri": uri,
        "contract_uri": contract_uri,
        "instance_tags": canonical_tags(instance_tags),
        "runtime_path": runtime_path,
        "status": ElementStatus.ACTIVE.value,
    }
    tables["bindings"] = pd.concat([tables["bindings"], pd.DataFrame([new_row])], ignore_index=True)
    return uri


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
    tables: dict[str, pd.DataFrame],
    metadata: ModelMetadata,
    concept_uri: str,
    revision_uri: str,
    *,
    rebind: bool = True,
) -> str:
    """Supersede a property's contract and bindings, then mint a new contract.

    With *rebind*, the superseded ACTIVE bindings are re-minted unchanged (same tags and runtime path)
    under the new contract.
    """
    old_contract_uri = _active_contract_uri(tables, concept_uri)
    carried: list[tuple[str, str]] = []
    if rebind and old_contract_uri is not None:
        b = tables["bindings"]
        live = b[(b["contract_uri"] == old_contract_uri) & (b["status"] == ElementStatus.ACTIVE)]
        carried = list(zip(live["instance_tags"], live["runtime_path"], strict=True))
    _supersede_contract(tables, concept_uri)
    new_contract_uri = _mint_contract(tables, metadata, concept_uri, revision_uri)
    _supersede_bindings_by_concept(tables, concept_uri)
    for tags_json, path in carried:
        _mint_binding(tables, metadata, new_contract_uri, json.loads(tags_json), path)
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
    instances: dict[str, list[str]] | None,
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


# ── Serialisation helpers ─────────────────────────────────────────────────────


def _parse_instances(value: Any) -> dict[str, list[str]] | None:
    """Deserialise a JSON-encoded dimension dictionary from the CSV; returns None for missing/null values."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, dict):
        return value
    return json.loads(value)  # type: ignore[no-any-return]


def _serialize_instances(instances: dict[str, list[str]] | None) -> str | None:
    """Serialise a dimension dictionary to canonical JSON for the CSV; None when empty."""
    if not instances:
        return None
    return json.dumps(instances, sort_keys=True)


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
