"""Sync engine — processes a DiffReport and updates the four ledger tables.

The engine iterates the ordered list of change events from a :class:`~modl.ir.DiffReport`,
determines whether each change is breaking according to the :class:`~modl.config.BreakingChangeConfig`,
and writes the appropriate rows to the concepts, revisions, variants, and bindings tables.

Usage::

    updated_tables = sync(tables, report, cfg)
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd

from modl.config import BreakingChangeConfig
from modl.ir import ChangeType, DiffReport, EntityChanged, PropertyChanged
from modl.ledger import b36encode, next_serial
from modl.models import ElementKind, ElementStatus

log = logging.getLogger(__name__)

# Kinds whose concepts receive bindings
_BINDING_KINDS = {ElementKind.PROPERTY}

# Canonical instances aspect key on entity events
_INSTANCES_KEY = "instances"


class SyncError(Exception):
    """Raised when the diff report violates a consistency constraint the engine cannot resolve."""


# ── Public API ────────────────────────────────────────────────────────────────


def sync(
    tables: dict[str, pd.DataFrame],
    report: DiffReport,
    cfg: BreakingChangeConfig,
) -> dict[str, pd.DataFrame]:
    """Apply a diff report to the ledger tables and return the updated tables.

    The input tables are not modified; the function returns deep copies with all
    changes applied.  The caller is responsible for writing the result to disk.

    Raises :exc:`SyncError` on structural consistency violations (e.g. entity removed
    without corresponding child property REMOVED events).
    """
    # Work on copies so the caller's state is unaffected on error
    tables = {name: df.copy() for name, df in tables.items()}

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
        _process_entity(tables, event, cfg, removed_labels)
    for event in added_properties:
        _process_property(tables, event, cfg)
    for event in rest:
        if isinstance(event, EntityChanged):
            _process_entity(tables, event, cfg, removed_labels)
        else:
            _process_property(tables, event, cfg)

    return tables


# ── Event processors ──────────────────────────────────────────────────────────


def _process_entity(
    tables: dict[str, pd.DataFrame],
    event: EntityChanged,
    cfg: BreakingChangeConfig,
    removed_labels: set[str],
) -> None:
    ct = event.change_type

    if ct == ChangeType.ADDED:
        _entity_added(tables, event, cfg)
    elif ct == ChangeType.MODIFIED:
        _entity_modified(tables, event, cfg)
    else:
        _entity_removed(tables, event, cfg, removed_labels)


def _process_property(
    tables: dict[str, pd.DataFrame],
    event: PropertyChanged,
    cfg: BreakingChangeConfig,
) -> None:
    ct = event.change_type

    if ct == ChangeType.ADDED:
        _property_added(tables, event, cfg)
    elif ct == ChangeType.MODIFIED:
        _property_modified(tables, event, cfg)
    else:
        _property_removed(tables, event, cfg)


# ── Entity handlers ───────────────────────────────────────────────────────────


def _entity_added(
    tables: dict[str, pd.DataFrame],
    event: EntityChanged,
    cfg: BreakingChangeConfig,
) -> None:
    instances = event.aspects.get(_INSTANCES_KEY)
    if instances is not None:
        _validate_instances(instances, event.label)
    instances_json = _serialize_instances(instances)

    concept_uri = _mint_concept(
        tables,
        cfg,
        label=event.label,
        kind=event.kind,
        parent_uri=None,
        instances_json=instances_json,
    )
    revision_uri = _mint_revision(
        tables, cfg, concept_uri=concept_uri, prev_revision_uri=None, status=ElementStatus.ACTIVE
    )
    _mint_variant(tables, cfg, concept_uri=concept_uri, revision_uri=revision_uri)

    log.info("Entity ADDED: concept=%s revision=%s", concept_uri, revision_uri)


def _entity_modified(
    tables: dict[str, pd.DataFrame],
    event: EntityChanged,
    cfg: BreakingChangeConfig,
) -> None:
    lookup_label = event.renamed_from if event.renamed_from is not None else event.label
    concept_row_idx, concept_uri = _require_concept(tables, lookup_label)
    breaking = cfg.is_breaking(event.kind, event.aspects, renamed_from=event.renamed_from)

    # Rename
    if event.renamed_from is not None:
        _apply_rename(tables, concept_row_idx, event.label, event.renamed_from)

    # Determine old/new instance lists
    old_instances_json: str | None = tables["concepts"].at[concept_row_idx, "instances"]
    old_instances: list[str] | None = _parse_instances(old_instances_json)

    new_instances: list[str] | None = None
    instances_changed = False
    if _INSTANCES_KEY in event.aspects:
        new_instances = event.aspects[_INSTANCES_KEY]
        if new_instances is not None:
            _validate_instances(new_instances, event.label)
        instances_changed = new_instances != old_instances

    # --- Entity revision ---
    prev_rev_uri = _active_revision_uri(tables, concept_uri)
    _supersede_revision(tables, concept_uri)
    revision_uri = _mint_revision(
        tables, cfg, concept_uri=concept_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.ACTIVE
    )

    if breaking:
        # New entity variant
        prev_variant_uri = _active_variant_uri(tables, concept_uri)
        _supersede_variant(tables, concept_uri)
        variant_uri = _mint_variant(tables, cfg, concept_uri=concept_uri, revision_uri=revision_uri)

        if instances_changed:
            # Instance-breaking: update entity instances column, cascade to all child properties
            _set_instances(tables, concept_row_idx, new_instances)
            _cascade_instance_breaking(tables, cfg, entity_concept_uri=concept_uri, new_instances=new_instances or [])
        # else: non-instance breaking — entity variant already updated; no child cascade

        log.info(
            "Entity MODIFIED (breaking): concept=%s new_revision=%s new_variant=%s prev_variant=%s",
            concept_uri,
            revision_uri,
            variant_uri,
            prev_variant_uri,
        )
    else:
        # Non-breaking
        if instances_changed:
            # Update entity instances column
            _set_instances(tables, concept_row_idx, new_instances)
            # Cascade to children: new revision per child + new bindings for NEW instances only
            _cascade_instance_nonbreaking(
                tables,
                cfg,
                entity_concept_uri=concept_uri,
                old_instances=old_instances or [],
                new_instances=new_instances or [],
            )

        log.info("Entity MODIFIED (non-breaking): concept=%s new_revision=%s", concept_uri, revision_uri)


def _entity_removed(
    tables: dict[str, pd.DataFrame],
    event: EntityChanged,
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
    _mint_revision(tables, cfg, concept_uri=concept_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.REMOVED)

    # All active variants → REMOVED
    _set_variant_status(tables, concept_uri, ElementStatus.REMOVED)

    # Concept → REMOVED
    tables["concepts"].at[concept_row_idx, "status"] = ElementStatus.REMOVED

    log.info("Entity REMOVED: concept=%s", concept_uri)


# ── Property handlers ─────────────────────────────────────────────────────────


def _property_added(
    tables: dict[str, pd.DataFrame],
    event: PropertyChanged,
    cfg: BreakingChangeConfig,
) -> None:
    # Look up parent entity concept for parent_uri and instance list
    parent_idx, parent_uri = _require_concept(tables, event.parent_label)
    parent_instances_json: str | None = tables["concepts"].at[parent_idx, "instances"]
    parent_instances: list[str] | None = _parse_instances(parent_instances_json)

    concept_uri = _mint_concept(
        tables,
        cfg,
        label=event.label,
        kind=event.kind,
        parent_uri=parent_uri,
        instances_json=parent_instances_json,
    )
    revision_uri = _mint_revision(
        tables, cfg, concept_uri=concept_uri, prev_revision_uri=None, status=ElementStatus.ACTIVE
    )
    variant_uri = _mint_variant(tables, cfg, concept_uri=concept_uri, revision_uri=revision_uri)

    # Mint bindings for PROPERTY kind only
    if event.kind == ElementKind.PROPERTY:
        _mint_bindings_for_instances(tables, cfg, variant_uri=variant_uri, instances=parent_instances)

    log.info("Property ADDED: concept=%s revision=%s variant=%s", concept_uri, revision_uri, variant_uri)


def _property_modified(
    tables: dict[str, pd.DataFrame],
    event: PropertyChanged,
    cfg: BreakingChangeConfig,
) -> None:
    lookup_label = event.renamed_from if event.renamed_from is not None else event.label
    concept_row_idx, concept_uri = _require_concept(tables, lookup_label)
    breaking = cfg.is_breaking(event.kind, event.aspects, renamed_from=event.renamed_from)

    if event.renamed_from is not None:
        _apply_rename(tables, concept_row_idx, event.label, event.renamed_from)

    prev_rev_uri = _active_revision_uri(tables, concept_uri)
    _supersede_revision(tables, concept_uri)
    revision_uri = _mint_revision(
        tables, cfg, concept_uri=concept_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.ACTIVE
    )

    if breaking:
        _supersede_variant(tables, concept_uri)
        variant_uri = _mint_variant(tables, cfg, concept_uri=concept_uri, revision_uri=revision_uri)

        if event.kind == ElementKind.PROPERTY:
            # Supersede old bindings and mint new ones under new variant
            instances_json: str | None = tables["concepts"].at[concept_row_idx, "instances"]
            instances = _parse_instances(instances_json)
            _supersede_bindings_by_concept(tables, concept_uri)
            _mint_bindings_for_instances(tables, cfg, variant_uri=variant_uri, instances=instances)

        log.info(
            "Property MODIFIED (breaking): concept=%s new_revision=%s new_variant=%s",
            concept_uri,
            revision_uri,
            variant_uri,
        )
    else:
        log.info("Property MODIFIED (non-breaking): concept=%s new_revision=%s", concept_uri, revision_uri)


def _property_removed(
    tables: dict[str, pd.DataFrame],
    event: PropertyChanged,
    cfg: BreakingChangeConfig,
) -> None:
    concept_row_idx, concept_uri = _require_concept(tables, event.label)

    prev_rev_uri = _active_revision_uri(tables, concept_uri)
    _supersede_revision(tables, concept_uri)
    _mint_revision(tables, cfg, concept_uri=concept_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.REMOVED)

    # All active variants → REMOVED
    _set_variant_status(tables, concept_uri, ElementStatus.REMOVED)

    # All active bindings (PROPERTY kind) → REMOVED
    if tables["concepts"].at[concept_row_idx, "kind"] == ElementKind.PROPERTY:
        _supersede_bindings_by_concept(tables, concept_uri, status=ElementStatus.REMOVED)

    tables["concepts"].at[concept_row_idx, "status"] = ElementStatus.REMOVED

    log.info("Property REMOVED: concept=%s", concept_uri)


# ── Instance cascade helpers ──────────────────────────────────────────────────


def _cascade_instance_breaking(
    tables: dict[str, pd.DataFrame],
    cfg: BreakingChangeConfig,
    entity_concept_uri: str,
    new_instances: list[str],
) -> None:
    """New variants + bindings for all child properties; supersede old bindings."""
    child_df = _child_concepts(tables, entity_concept_uri)
    if child_df.empty:
        return

    new_instances_json = _serialize_instances(new_instances if new_instances else None)

    for _, child_row in child_df.iterrows():
        child_uri: str = child_row["concept_uri"]
        child_idx = tables["concepts"].index[tables["concepts"]["concept_uri"] == child_uri][0]
        child_kind: str = child_row["kind"]

        # Update child instances column
        tables["concepts"].at[child_idx, "instances"] = new_instances_json

        prev_rev_uri = _active_revision_uri(tables, child_uri)
        _supersede_revision(tables, child_uri)
        revision_uri = _mint_revision(
            tables, cfg, concept_uri=child_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.ACTIVE
        )

        _supersede_variant(tables, child_uri)
        variant_uri = _mint_variant(tables, cfg, concept_uri=child_uri, revision_uri=revision_uri)

        if child_kind == ElementKind.PROPERTY:
            _supersede_bindings_by_concept(tables, child_uri)
            _mint_bindings_for_instances(tables, cfg, variant_uri=variant_uri, instances=new_instances or None)


def _cascade_instance_nonbreaking(
    tables: dict[str, pd.DataFrame],
    cfg: BreakingChangeConfig,
    entity_concept_uri: str,
    old_instances: list[str],
    new_instances: list[str],
) -> None:
    """New revisions for all child properties; new bindings only for newly added instances."""
    child_df = _child_concepts(tables, entity_concept_uri)
    if child_df.empty:
        return

    added_instances = [inst for inst in new_instances if inst not in old_instances]
    new_instances_json = _serialize_instances(new_instances if new_instances else None)

    for _, child_row in child_df.iterrows():
        child_uri: str = child_row["concept_uri"]
        child_idx = tables["concepts"].index[tables["concepts"]["concept_uri"] == child_uri][0]
        child_kind: str = child_row["kind"]

        # Update child instances column
        tables["concepts"].at[child_idx, "instances"] = new_instances_json

        prev_rev_uri = _active_revision_uri(tables, child_uri)
        _supersede_revision(tables, child_uri)
        _mint_revision(tables, cfg, concept_uri=child_uri, prev_revision_uri=prev_rev_uri, status=ElementStatus.ACTIVE)

        # Append new bindings for added instances only (to the existing active variant)
        if child_kind == ElementKind.PROPERTY and added_instances:
            active_variant = _active_variant_uri(tables, child_uri)
            if active_variant:
                for instance in added_instances:
                    _mint_binding(tables, cfg, variant_uri=active_variant, instance_label=instance)


# ── Minting helpers ───────────────────────────────────────────────────────────


def _mint_uri(cfg: BreakingChangeConfig, table: str, serial: int) -> str:
    """Build a fully-qualified URI for a new record."""
    return f"{cfg.namespace.uri_base(table)}/{b36encode(serial)}"


def _mint_concept(
    tables: dict[str, pd.DataFrame],
    cfg: BreakingChangeConfig,
    label: str,
    kind: ElementKind,
    parent_uri: str | None,
    instances_json: str | None,
) -> str:
    serial = next_serial(tables["concepts"])
    uri = _mint_uri(cfg, "concepts", serial)
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
    cfg: BreakingChangeConfig,
    concept_uri: str,
    prev_revision_uri: str | None,
    status: ElementStatus,
) -> str:
    serial = next_serial(tables["revisions"])
    uri = _mint_uri(cfg, "revisions", serial)
    new_row: dict[str, Any] = {
        "serial": serial,
        "revision_uri": uri,
        "concept_uri": concept_uri,
        "previous_revision_uri": prev_revision_uri,
        "status": status.value,
    }
    tables["revisions"] = pd.concat([tables["revisions"], pd.DataFrame([new_row])], ignore_index=True)
    return uri


def _mint_variant(
    tables: dict[str, pd.DataFrame],
    cfg: BreakingChangeConfig,
    concept_uri: str,
    revision_uri: str,
) -> str:
    serial = next_serial(tables["variants"])
    uri = _mint_uri(cfg, "variants", serial)
    new_row: dict[str, Any] = {
        "serial": serial,
        "variant_uri": uri,
        "concept_uri": concept_uri,
        "revision_uri": revision_uri,
        "status": ElementStatus.ACTIVE.value,
    }
    tables["variants"] = pd.concat([tables["variants"], pd.DataFrame([new_row])], ignore_index=True)
    return uri


def _mint_binding(
    tables: dict[str, pd.DataFrame],
    cfg: BreakingChangeConfig,
    variant_uri: str,
    instance_label: str | None,
) -> str:
    serial = next_serial(tables["bindings"])
    uri = _mint_uri(cfg, "bindings", serial)
    new_row: dict[str, Any] = {
        "serial": serial,
        "binding_uri": uri,
        "variant_uri": variant_uri,
        "instance_label": instance_label,
        "status": ElementStatus.ACTIVE.value,
    }
    tables["bindings"] = pd.concat([tables["bindings"], pd.DataFrame([new_row])], ignore_index=True)
    return uri


def _mint_bindings_for_instances(
    tables: dict[str, pd.DataFrame],
    cfg: BreakingChangeConfig,
    variant_uri: str,
    instances: list[str] | None,
) -> None:
    """Mint one binding per instance, or a single singleton binding when there are no instances."""
    if instances:
        for inst in instances:
            _mint_binding(tables, cfg, variant_uri=variant_uri, instance_label=inst)
    else:
        _mint_binding(tables, cfg, variant_uri=variant_uri, instance_label=None)


# ── Mutation helpers ──────────────────────────────────────────────────────────


def _supersede_revision(tables: dict[str, pd.DataFrame], concept_uri: str) -> None:
    """Mark the current ACTIVE revision for a concept as SUPERSEDED."""
    mask = (tables["revisions"]["concept_uri"] == concept_uri) & (tables["revisions"]["status"] == ElementStatus.ACTIVE)
    tables["revisions"].loc[mask, "status"] = ElementStatus.SUPERSEDED


def _supersede_variant(tables: dict[str, pd.DataFrame], concept_uri: str) -> None:
    """Mark the current ACTIVE variant for a concept as SUPERSEDED."""
    mask = (tables["variants"]["concept_uri"] == concept_uri) & (tables["variants"]["status"] == ElementStatus.ACTIVE)
    tables["variants"].loc[mask, "status"] = ElementStatus.SUPERSEDED


def _set_variant_status(
    tables: dict[str, pd.DataFrame],
    concept_uri: str,
    status: ElementStatus,
) -> None:
    """Set all ACTIVE variants for a concept to the given status."""
    mask = (tables["variants"]["concept_uri"] == concept_uri) & (tables["variants"]["status"] == ElementStatus.ACTIVE)
    tables["variants"].loc[mask, "status"] = status


def _supersede_bindings_by_concept(
    tables: dict[str, pd.DataFrame],
    concept_uri: str,
    status: ElementStatus = ElementStatus.SUPERSEDED,
) -> None:
    """Mark all ACTIVE bindings associated with any variant of the given concept."""
    variant_uris = set(tables["variants"][tables["variants"]["concept_uri"] == concept_uri]["variant_uri"])
    if not variant_uris:
        return
    mask = tables["bindings"]["variant_uri"].isin(variant_uris) & (tables["bindings"]["status"] == ElementStatus.ACTIVE)
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
    tables["concepts"].at[concept_idx, "previous_labels"] = json.dumps(existing)
    tables["concepts"].at[concept_idx, "current_label"] = new_label


def _set_instances(
    tables: dict[str, pd.DataFrame],
    concept_idx: int,
    instances: list[str] | None,
) -> None:
    tables["concepts"].at[concept_idx, "instances"] = _serialize_instances(instances)


# ── Query helpers ─────────────────────────────────────────────────────────────


def _require_concept(tables: dict[str, pd.DataFrame], label: str) -> tuple[int, str]:
    """Return the DataFrame index and concept_uri for a concept with the given current_label.

    Raises :exc:`SyncError` if no matching concept is found.
    """
    df = tables["concepts"]
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


def _active_variant_uri(tables: dict[str, pd.DataFrame], concept_uri: str) -> str | None:
    """Return the URI of the current ACTIVE variant for a concept, or None."""
    df = tables["variants"]
    mask = (df["concept_uri"] == concept_uri) & (df["status"] == ElementStatus.ACTIVE)
    active = df[mask]
    if active.empty:
        return None
    return str(active.iloc[0]["variant_uri"])


def _child_concepts(tables: dict[str, pd.DataFrame], parent_uri: str) -> pd.DataFrame:
    """Return all concept rows whose parent_uri matches the given entity concept URI."""
    df = tables["concepts"]
    return df[df["parent_uri"] == parent_uri]


# ── Serialisation helpers ─────────────────────────────────────────────────────


def _parse_instances(value: Any) -> list[str] | None:
    """Deserialise a JSON-encoded instance list from the CSV; returns None for missing/null values."""
    if value is None or (isinstance(value, float) and __import__("math").isnan(value)):
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
    """Raise :exc:`SyncError` when *instances* is not a flat list of strings.

    Catches nested lists, dicts, numbers, and other non-string elements that indicate
    a non-compliant diff adapter (e.g. multi-dimensional instance arrays).
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


def _parse_previous_labels(value: Any) -> list[str]:
    """Deserialise previous_labels from the CSV; returns empty list for missing/null values."""
    if value is None or (isinstance(value, float) and __import__("math").isnan(value)):
        return []
    if isinstance(value, list):
        return value
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []
