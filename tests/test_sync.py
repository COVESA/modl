"""Tests for the sync engine (modl.sync)."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from pydantic import ValidationError

from modl.config import BreakingChangeConfig, ModelMetadata
from modl.ir import (
    ChangeType,
    ContentItem,
    DiffReport,
    EntityChanged,
    PathChange,
    PathEntry,
    PropertyChanged,
    PropertyPaths,
)
from modl.ledger import empty_ledger, validate_ledger
from modl.models import ElementKind, ElementStatus
from modl.sync import SyncError, sync

# ── Fixtures ──────────────────────────────────────────────────────────────────

NS = "http://test.example/model/"


def _meta() -> ModelMetadata:
    return ModelMetadata(name="Test", id=NS)


def _cfg(
    *,
    entity: dict | None = None,
    property: dict | None = None,
    enumeration_set: dict | None = None,
    enum_value: dict | None = None,
) -> BreakingChangeConfig:
    raw: dict = {}
    if entity is not None:
        raw["entity"] = entity
    if property is not None:
        raw["property"] = property
    if enumeration_set is not None:
        raw["enumeration_set"] = enumeration_set
    if enum_value is not None:
        raw["enum_value"] = enum_value
    return BreakingChangeConfig.model_validate(raw)


def _paths(label: str, parent: str, *entries: tuple[dict, str, str]) -> PropertyPaths:
    """Build a PropertyPaths from (tags, path, change) tuples."""
    return PropertyPaths(
        label=label,
        parent_label=parent,
        paths=[PathEntry(tags=t, path=p, change=PathChange(c)) for t, p, c in entries],
    )


def _report(*changes, paths: list[PropertyPaths] | None = None, auto_paths: bool = True) -> DiffReport:
    """Build a report; unless *auto_paths* is False, fill in singleton paths for binding-eligible properties.

    An ADDED property gets an ``added`` entry and a MODIFIED one an ``unchanged`` entry, each with empty tags and
    the property label as runtime path. Explicit *paths* entries take precedence.
    """
    entries = list(paths or [])
    if auto_paths:
        have = {(p.parent_label, p.label) for p in entries}
        for c in changes:
            if (
                isinstance(c, PropertyChanged)
                and c.kind == ElementKind.PROPERTY
                and c.change_type != ChangeType.REMOVED
                and (c.is_leaf or c.binding_eligible)
                and (c.parent_label, c.label) not in have
            ):
                change = "added" if c.change_type == ChangeType.ADDED else "unchanged"
                path = c.label if c.label.startswith(f"{c.parent_label}.") else f"{c.parent_label}.{c.label}"
                entries.append(_paths(c.label, c.parent_label, ({}, path, change)))
    return DiffReport(changes=list(changes), property_paths=entries)


def _run(cfg: BreakingChangeConfig, *reports: DiffReport) -> dict:
    """Sync the reports one after another into an empty ledger."""
    tables = empty_ledger()
    for report in reports:
        tables = sync(tables, report, _meta(), cfg)
    return tables


_INSTANCE_STRUCTURAL_KEYS = {"instances_added", "instances_removed", "instances"}


def _wrap_modified_aspects(aspects: dict) -> dict:
    """Wrap plain aspect values as {"_op": "modified", "_value": ..., "_previous": ...} for MODIFIED events."""
    wrapped = {}
    for key, value in aspects.items():
        if key in _INSTANCE_STRUCTURAL_KEYS:
            wrapped[key] = value
        else:
            wrapped[key] = {"_op": "modified", "_value": value, "_previous": f"old_{key}"}
    return wrapped


def _entity_added(label: str, kind: ElementKind = ElementKind.ENTITY, **aspects) -> EntityChanged:
    return EntityChanged(label=label, kind=kind, change_type=ChangeType.ADDED, aspects=dict(aspects))


def _entity_modified(label: str, renamed_from: str | None = None, **aspects) -> EntityChanged:
    return EntityChanged(
        label=label, change_type=ChangeType.MODIFIED, renamed_from=renamed_from, aspects=_wrap_modified_aspects(aspects)
    )


def _entity_removed(label: str, **previous_aspects) -> EntityChanged:
    return EntityChanged(
        label=label, change_type=ChangeType.REMOVED, previous_aspects=previous_aspects or {"type": "branch"}
    )


def _prop_added(
    label: str,
    parent: str,
    kind: ElementKind = ElementKind.PROPERTY,
    is_leaf: bool | None = None,
    binding_eligible: bool = False,
    references: dict[str, str] | None = None,
    **aspects,
) -> PropertyChanged:
    if kind == ElementKind.PROPERTY and is_leaf is None:
        is_leaf = True
    return PropertyChanged(
        label=label,
        parent_label=parent,
        kind=kind,
        change_type=ChangeType.ADDED,
        is_leaf=is_leaf,
        binding_eligible=binding_eligible,
        references=references,
        aspects=dict(aspects),
    )


def _prop_modified(
    label: str,
    parent: str,
    renamed_from: str | None = None,
    is_leaf: bool = True,
    binding_eligible: bool = False,
    references: dict[str, str] | None = None,
    **aspects,
) -> PropertyChanged:
    return PropertyChanged(
        label=label,
        parent_label=parent,
        change_type=ChangeType.MODIFIED,
        renamed_from=renamed_from,
        is_leaf=is_leaf,
        binding_eligible=binding_eligible,
        references=references,
        aspects=_wrap_modified_aspects(aspects),
    )


def _prop_removed(
    label: str, parent: str, is_leaf: bool = True, binding_eligible: bool = False, **previous_aspects
) -> PropertyChanged:
    return PropertyChanged(
        label=label,
        parent_label=parent,
        change_type=ChangeType.REMOVED,
        is_leaf=is_leaf,
        binding_eligible=binding_eligible,
        previous_aspects=previous_aspects or {"output_type": "Float"},
    )


# ── URI helpers ───────────────────────────────────────────────────────────────


def _uri(table: str, serial: int) -> str:
    from modl.ledger import b36encode

    return f"{NS}{table}/{b36encode(serial)}"


# ── Entity ADDED ──────────────────────────────────────────────────────────────


class TestEntityAdded:
    def test_mints_concept_revision_variant(self) -> None:
        """Entity ADDED creates exactly one concept, revision, and variant row."""
        tables = sync(empty_ledger(), _report(_entity_added("Vehicle")), _meta(), _cfg())
        assert len(tables["concepts"]) == 1
        assert len(tables["revisions"]) == 1
        assert len(tables["contracts"]) == 1
        assert len(tables["bindings"]) == 0

    def test_concept_row_values(self) -> None:
        """Concept row has correct label, kind, status, and null parent_uri."""
        tables = sync(empty_ledger(), _report(_entity_added("Vehicle")), _meta(), _cfg())
        row = tables["concepts"].iloc[0]
        assert row["current_label"] == "Vehicle"
        assert row["kind"] == ElementKind.ENTITY
        assert row["status"] == ElementStatus.ACTIVE
        assert row["parent_uri"] is None or (isinstance(row["parent_uri"], float))  # null in DataFrame

    def test_serial_and_uri(self) -> None:
        """Concept URI encodes serial 0 in base-36."""
        tables = sync(empty_ledger(), _report(_entity_added("Vehicle")), _meta(), _cfg())
        row = tables["concepts"].iloc[0]
        assert row["serial"] == 0
        assert row["concept_uri"] == _uri("concepts", 0)

    def test_revision_status_active(self) -> None:
        """First revision is ACTIVE with no previous_revision_uri."""
        tables = sync(empty_ledger(), _report(_entity_added("Vehicle")), _meta(), _cfg())
        rev = tables["revisions"].iloc[0]
        assert rev["status"] == ElementStatus.ACTIVE
        assert rev["previous_revision_uri"] is None or str(rev["previous_revision_uri"]) == "nan"

    def test_instances_stored_on_concept(self) -> None:
        """Entity ADDED with instances stores the dimension dictionary as JSON on the concept row."""
        report = _report(_entity_added("Door", instances={"side": ["Left", "Right"], "row": ["1"]}))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        row = tables["concepts"].iloc[0]
        assert json.loads(row["instances"]) == {"row": ["1"], "side": ["Left", "Right"]}

    def test_no_instances_stored_as_null(self) -> None:
        """Entity with no instances has null instances column."""
        tables = sync(empty_ledger(), _report(_entity_added("Vehicle")), _meta(), _cfg())
        row = tables["concepts"].iloc[0]
        assert row["instances"] is None or str(row["instances"]) == "nan"

    def test_multiple_entities_incrementing_serials(self) -> None:
        """Two ADDED entities get serials 0 and 1."""
        tables = sync(empty_ledger(), _report(_entity_added("A"), _entity_added("B")), _meta(), _cfg())
        serials = sorted(tables["concepts"]["serial"].tolist())
        assert serials == [0, 1]

    def test_ledger_validates_after_add(self) -> None:
        """Resulting ledger passes full validation."""
        tables = sync(empty_ledger(), _report(_entity_added("Vehicle")), _meta(), _cfg())
        validate_ledger(tables)  # must not raise

    def test_instances_must_be_a_dimension_dict(self) -> None:
        """A flat instance list is rejected when the report is built."""
        with pytest.raises(ValueError, match="dimension"):
            _entity_added("Door", instances=["Left", "Right"])


# ── Property ADDED ────────────────────────────────────────────────────────────


class TestPropertyAdded:
    def test_singleton_binding_when_no_instances(self) -> None:
        """Property reported with empty tags gets one binding with empty instance_tags and its runtime path."""
        report = _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 1
        b = tables["bindings"].iloc[0]
        assert json.loads(b["instance_tags"]) == {}
        assert b["runtime_path"] == "Vehicle.Speed"

    def test_binding_per_reported_path(self) -> None:
        """One binding is minted per reported path, tagged by entity concept_uri."""
        report = _report(
            _entity_added("Door", instances={"side": ["Left", "Right"]}),
            _prop_added("Door.IsOpen", parent="Door"),
            paths=[
                _paths(
                    "Door.IsOpen",
                    "Door",
                    ({"Door": {"side": "Left"}}, "Door.Left.IsOpen", "added"),
                    ({"Door": {"side": "Right"}}, "Door.Right.IsOpen", "added"),
                )
            ],
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        door_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]["concept_uri"]
        bindings = tables["bindings"]
        assert len(bindings) == 2
        assert set(bindings["runtime_path"]) == {"Door.Left.IsOpen", "Door.Right.IsOpen"}
        assert {json.loads(t)[door_uri]["side"] for t in bindings["instance_tags"]} == {"Left", "Right"}

    def test_property_concept_stores_parent_uri(self) -> None:
        """Property concept row carries the parent entity's concept_uri as parent_uri."""
        report = _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]
        entity_row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle"].iloc[0]
        assert prop_row["parent_uri"] == entity_row["concept_uri"]

    def test_property_concept_has_null_instances(self) -> None:
        """Only entities carry instances; the property row stores null."""
        report = _report(
            _entity_added("Door", instances={"side": ["Left", "Right"]}),
            _prop_added("Door.IsOpen", parent="Door"),
            paths=[_paths("Door.IsOpen", "Door", ({"Door": {"side": "Left"}}, "Door.Left.IsOpen", "added"))],
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]
        assert prop_row["instances"] is None or str(prop_row["instances"]) == "nan"

    def test_enum_value_no_binding(self) -> None:
        """ENUM_VALUE property gets concept+revision+variant but no binding."""
        report = _report(
            _entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET),
            _prop_added("SpeedUnit.KMH", parent="SpeedUnit", kind=ElementKind.ENUM_VALUE),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 0

    def test_non_leaf_property_gets_no_binding_by_default(self) -> None:
        """A non-leaf property (is_leaf=False) gets no binding when binding_eligible is left at its default."""
        report = _report(
            _entity_added("Object"),
            _prop_added("Object.Position", parent="Object", is_leaf=False, output_type="Structs.Position"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 0

    def test_non_leaf_property_with_binding_eligible_gets_binding(self) -> None:
        """A non-leaf property marked binding_eligible=True still mints a binding (e.g. a struct root)."""
        report = _report(
            _entity_added("Object"),
            _prop_added(
                "Object.Position",
                parent="Object",
                is_leaf=False,
                binding_eligible=True,
                output_type="Structs.Position",
            ),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 1
        b = tables["bindings"].iloc[0]
        assert json.loads(b["instance_tags"]) == {}

    def test_property_added_raises_on_unknown_parent(self) -> None:
        """Property ADDED for an unknown parent label raises SyncError."""
        report = _report(_prop_added("X.Speed", parent="X"))
        with pytest.raises(SyncError, match="No concept found"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_ledger_validates_after_add(self) -> None:
        """Full ledger validation passes after entity + property ADDED."""
        validate_ledger(sync(empty_ledger(), _door_setup("Left", "Right"), _meta(), _cfg()))


# ── Entity MODIFIED (non-breaking) ────────────────────────────────────────────


class TestEntityModifiedNonBreaking:
    def test_no_new_variant(self) -> None:
        """Non-breaking entity MODIFIED does not create a new variant."""
        report = _report(_entity_added("Vehicle"), _entity_modified("Vehicle", description="updated"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["contracts"]) == 1

    def test_new_revision_supersedes_old(self) -> None:
        """Non-breaking MODIFIED mints a new revision and supersedes the previous one."""
        cfg = _cfg(entity={"description": False})
        report = _report(_entity_added("Vehicle"), _entity_modified("Vehicle", description="updated"))
        tables = sync(empty_ledger(), report, _meta(), cfg)
        revs = tables["revisions"]
        assert len(revs) == 2
        assert (revs["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (revs["status"] == ElementStatus.ACTIVE).sum() == 1

    def test_revision_chaining(self) -> None:
        """New revision's previous_revision_uri points to the superseded revision."""
        report = _report(_entity_added("Vehicle"), _entity_modified("Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        revs = tables["revisions"]
        old_rev_uri = revs[revs["status"] == ElementStatus.SUPERSEDED].iloc[0]["revision_uri"]
        new_rev = revs[revs["status"] == ElementStatus.ACTIVE].iloc[0]
        assert new_rev["previous_revision_uri"] == old_rev_uri

    def test_rename_updates_label_and_previous_labels(self) -> None:
        """Rename updates current_label and appends old label to previous_labels."""
        report = _report(
            _entity_added("Vehicl"),
            _entity_modified("Vehicle", renamed_from="Vehicl"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        row = tables["concepts"].iloc[0]
        assert row["current_label"] == "Vehicle"
        prev = json.loads(row["previous_labels"])
        assert "Vehicl" in prev


# ── Entity MODIFIED (breaking, non-instance) ──────────────────────────────────


class TestEntityModifiedBreakingNonInstance:
    def test_new_entity_variant(self) -> None:
        """Breaking entity MODIFIED creates a new entity variant and supersedes the old one."""
        cfg = _cfg(entity={"type": True})
        report = _report(_entity_added("Vehicle", type="branch"), _entity_modified("Vehicle", type="object"))
        tables = sync(empty_ledger(), report, _meta(), cfg)
        variants = tables["contracts"]
        assert len(variants) == 2
        assert (variants["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (variants["status"] == ElementStatus.ACTIVE).sum() == 1

    def test_no_child_property_cascade(self) -> None:
        """Breaking non-instance entity change does not cascade to child properties."""
        cfg = _cfg(entity={"type": True})
        report = _report(
            _entity_added("Vehicle", type="branch"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _entity_modified("Vehicle", type="object"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        # Only the entity variant created at ADDED is superseded; one new entity variant; property variant unchanged
        prop_variants = tables["contracts"][
            tables["contracts"]["concept_uri"]
            == tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]["concept_uri"]
        ]
        assert (prop_variants["status"] == ElementStatus.ACTIVE).sum() == 1
        assert (prop_variants["status"] == ElementStatus.SUPERSEDED).sum() == 0


def _door_entries(*, added=(), unchanged=(), removed=(), prop: str = "IsOpen") -> PropertyPaths:
    """Paths for a Door property tagged by the Door ``side`` dimension."""
    entries = [
        ({"Door": {"side": side}}, f"Door.{side}.{prop}", change)
        for change, sides in (("added", added), ("unchanged", unchanged), ("removed", removed))
        for side in sides
    ]
    return _paths(f"Door.{prop}", "Door", *entries)


def _door_setup(*sides: str) -> DiffReport:
    """Report that adds Door (with the given sides) and its IsOpen property bound once per side."""
    return _report(
        _entity_added("Door", instances={"side": list(sides)}),
        _prop_added("Door.IsOpen", parent="Door"),
        paths=[_door_entries(added=sides)],
    )


def _active(tables: dict) -> pd.DataFrame:
    return tables["bindings"][tables["bindings"]["status"] == ElementStatus.ACTIVE]


# ── Entity MODIFIED (instance change, non-breaking) ───────────────────────────


class TestEntityModifiedInstanceNonBreaking:
    CFG = _cfg(entity={"instances": False})

    def _grow(self) -> dict:
        tables = sync(empty_ledger(), _door_setup("Left", "Right"), _meta(), self.CFG)
        step = _report(
            _entity_modified("Door", instances_added={"side": ["Center"]}),
            paths=[_door_entries(unchanged=("Left", "Right"), added=("Center",))],
        )
        return sync(tables, step, _meta(), self.CFG)

    def test_new_binding_for_added_instance_only(self) -> None:
        """Instance addition mints one new binding per reported added path."""
        tables = self._grow()
        assert len(tables["bindings"]) == 3
        assert set(tables["bindings"]["runtime_path"]) == {
            "Door.Left.IsOpen",
            "Door.Right.IsOpen",
            "Door.Center.IsOpen",
        }

    def test_old_bindings_unaffected(self) -> None:
        """Instance addition leaves existing bindings ACTIVE."""
        assert (self._grow()["bindings"]["status"] == ElementStatus.ACTIVE).all()

    def test_no_new_child_property_contract(self) -> None:
        """Instance addition does not create new child property contracts."""
        tables = self._grow()
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 1

    def test_removed_instance_binding_marked_removed(self) -> None:
        """Instance removal marks the binding of the removed path as REMOVED."""
        tables = sync(empty_ledger(), _door_setup("Left", "Right"), _meta(), self.CFG)
        step = _report(
            _entity_modified("Door", instances_removed={"side": ["Right"]}),
            paths=[_door_entries(unchanged=("Left",), removed=("Right",))],
        )
        tables = sync(tables, step, _meta(), self.CFG)
        removed = tables["bindings"][tables["bindings"]["status"] == ElementStatus.REMOVED]
        assert removed["runtime_path"].tolist() == ["Door.Right.IsOpen"]
        assert _active(tables)["runtime_path"].tolist() == ["Door.Left.IsOpen"]

    def test_entity_instances_column_updated(self) -> None:
        """Entity concept row reflects the updated instance dictionary."""
        tables = self._grow()
        entity_row = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]
        assert json.loads(entity_row["instances"]) == {"side": ["Left", "Right", "Center"]}

    def test_new_dimension_value_pairs_are_tagged_per_dimension(self) -> None:
        """Two-dimension instances are tagged with both dimension values."""
        report = _report(
            _entity_added("Seat", instances={"row": ["1", "2"], "side": ["L", "R"]}),
            _prop_added("Seat.Heat", parent="Seat"),
            paths=[
                _paths(
                    "Seat.Heat",
                    "Seat",
                    *[
                        ({"Seat": {"row": r, "side": s}}, f"Seat[{r}][{s}].Heat", "added")
                        for r in ("1", "2")
                        for s in ("L", "R")
                    ],
                )
            ],
        )
        tables = sync(empty_ledger(), report, _meta(), self.CFG)
        assert len(tables["bindings"]) == 4

    def test_singleton_child_unaffected_by_instance_change(self) -> None:
        """A property bound with empty tags keeps its binding when the parent's instances change."""
        tables = sync(
            empty_ledger(),
            _report(_entity_added("Door", instances={"side": ["Left"]}), _prop_added("Door.Singleton", parent="Door")),
            _meta(),
            self.CFG,
        )
        step = _report(_entity_modified("Door", instances_added={"side": ["Center"]}))
        tables = sync(tables, step, _meta(), self.CFG)
        active = _active(tables)
        assert len(active) == 1
        assert json.loads(active.iloc[0]["instance_tags"]) == {}

    def test_instance_change_without_paths_for_dependent_property_fails(self) -> None:
        """A property tagged with an entity whose instances change must be reported again."""
        tables = sync(empty_ledger(), _door_setup("Left", "Right"), _meta(), self.CFG)
        step = _report(_entity_modified("Door", instances_added={"side": ["Center"]}))
        with pytest.raises(SyncError, match="no property_paths entry"):
            sync(tables, step, _meta(), self.CFG)

    def test_added_value_must_not_already_exist(self) -> None:
        """Adding an instance that is already stored raises SyncError."""
        tables = sync(empty_ledger(), _door_setup("Left", "Right"), _meta(), self.CFG)
        step = _report(_entity_modified("Door", instances_added={"side": ["Left"]}))
        with pytest.raises(SyncError, match="already stored"):
            sync(tables, step, _meta(), self.CFG)

    def test_removed_value_must_exist(self) -> None:
        """Removing an instance that is not stored raises SyncError."""
        tables = sync(empty_ledger(), _door_setup("Left", "Right"), _meta(), self.CFG)
        step = _report(_entity_modified("Door", instances_removed={"side": ["Center"]}))
        with pytest.raises(SyncError, match="not stored"):
            sync(tables, step, _meta(), self.CFG)


# ── Entity MODIFIED (instance change, breaking) ───────────────────────────────


class TestEntityModifiedInstanceBreaking:
    CFG = _cfg(entity={"instances": True})

    def _grow(self) -> dict:
        tables = sync(empty_ledger(), _door_setup("Left", "Right"), _meta(), self.CFG)
        step = _report(
            _entity_modified("Door", instances_added={"side": ["Center"]}),
            paths=[_door_entries(unchanged=("Left", "Right"), added=("Center",))],
        )
        return sync(tables, step, _meta(), self.CFG)

    def test_removed_instance_binding_marked_removed(self) -> None:
        """Breaking instance removal marks the binding of the removed path as REMOVED."""
        tables = sync(empty_ledger(), _door_setup("Left", "Right"), _meta(), self.CFG)
        step = _report(
            _entity_modified("Door", instances_removed={"side": ["Right"]}),
            paths=[_door_entries(unchanged=("Left",), removed=("Right",))],
        )
        tables = sync(tables, step, _meta(), self.CFG)
        removed = tables["bindings"][tables["bindings"]["status"] == ElementStatus.REMOVED]
        assert removed["runtime_path"].tolist() == ["Door.Right.IsOpen"]

    def test_new_binding_for_added_instance(self) -> None:
        """Breaking instance addition mints a new binding for the added path."""
        assert len(_active(self._grow())) == 3

    def test_child_property_contract_never_changes(self) -> None:
        """An instance change on the parent does NOT create new contracts for child properties."""
        tables = self._grow()
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]["concept_uri"]
        prop_contracts = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
        assert len(prop_contracts) == 1
        assert prop_contracts.iloc[0]["status"] == ElementStatus.ACTIVE

    def test_new_binding_anchored_to_existing_contract(self) -> None:
        """Active bindings after the instance change still point to the original contract."""
        tables = self._grow()
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]["concept_uri"]
        contract_uri = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri].iloc[0]["contract_uri"]
        assert set(_active(tables)["contract_uri"]) == {contract_uri}

    def test_entity_gets_new_contract(self) -> None:
        """The entity itself receives a new contract because 'instances' is configured as breaking."""
        tables = self._grow()
        door_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == door_uri]) == 2

    def test_ledger_validates_after_breaking_instance_change(self) -> None:
        """Full ledger validation passes after a breaking instance change."""
        validate_ledger(self._grow())


# ── Entity REMOVED ────────────────────────────────────────────────────────────


class TestEntityRemoved:
    def test_concept_status_removed(self) -> None:
        """Entity REMOVED sets the concept status to REMOVED."""
        report = _report(_entity_added("Vehicle"), _entity_removed("Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        row = tables["concepts"].iloc[0]
        assert row["status"] == ElementStatus.REMOVED

    def test_revision_status_removed(self) -> None:
        """Entity REMOVED mints a final revision with REMOVED status."""
        report = _report(_entity_added("Vehicle"), _entity_removed("Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        revs = tables["revisions"]
        assert (revs["status"] == ElementStatus.REMOVED).sum() == 1

    def test_variants_removed(self) -> None:
        """All active variants for a removed entity are marked REMOVED."""
        report = _report(_entity_added("Vehicle"), _entity_removed("Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert (tables["contracts"]["status"] == ElementStatus.REMOVED).all()

    def test_consistency_check_raises_on_missing_child_removed(self) -> None:
        """REMOVED entity without explicit REMOVED events for all child properties raises SyncError."""
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _entity_removed("Vehicle"),  # Missing REMOVED for Vehicle.Speed
        )
        with pytest.raises(SyncError, match="Vehicle.Speed"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_consistency_check_passes_with_all_children_removed(self) -> None:
        """REMOVED entity with explicit REMOVED for all children completes without error."""
        tables = _run(
            _cfg(),
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle")),
            _report(_entity_removed("Vehicle"), _prop_removed("Vehicle.Speed", parent="Vehicle")),
        )  # must not raise
        assert len(tables["concepts"]) == 2

    def test_no_bindings_affected_directly(self) -> None:
        """Entity REMOVED itself does not touch bindings (property REMOVED handles that)."""
        tables = _run(
            _cfg(),
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle")),
            _report(_entity_removed("Vehicle"), _prop_removed("Vehicle.Speed", parent="Vehicle")),
        )
        assert (tables["bindings"]["status"] == ElementStatus.REMOVED).all()


# ── Property MODIFIED (breaking) ──────────────────────────────────────────────


class TestPropertyModifiedBreaking:
    CFG = _cfg(property={"output_type": True})

    def test_new_variant_supersedes_old(self) -> None:
        """Breaking property MODIFIED supersedes old variant and mints a new one."""
        tables = _run(
            self.CFG,
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Int")),
            _report(_prop_modified("Vehicle.Speed", parent="Vehicle", output_type="Float")),
        )
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]["concept_uri"]
        prop_variants = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
        assert len(prop_variants) == 2
        assert (prop_variants["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (prop_variants["status"] == ElementStatus.ACTIVE).sum() == 1

    def test_old_binding_superseded_new_binding_active(self) -> None:
        """Old binding is superseded; a new binding is minted anchored to the new variant."""
        tables = _run(
            self.CFG,
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Int")),
            _report(_prop_modified("Vehicle.Speed", parent="Vehicle", output_type="Float")),
        )
        assert (tables["bindings"]["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (tables["bindings"]["status"] == ElementStatus.ACTIVE).sum() == 1
        active_contract = tables["contracts"][tables["contracts"]["status"] == ElementStatus.ACTIVE]
        new_binding = _active(tables).iloc[0]
        assert new_binding["contract_uri"] in set(active_contract["contract_uri"])
        assert new_binding["runtime_path"] == "Vehicle.Speed"

    def test_instanced_breaking_all_bindings_replaced(self) -> None:
        """Breaking property MODIFIED on an instanced property replaces all bindings, keeping tags and paths."""
        setup = _report(
            _entity_added("Door", instances={"side": ["Left", "Right"]}),
            _prop_added("Door.IsOpen", parent="Door", output_type="Boolean"),
            paths=[_door_entries(added=("Left", "Right"))],
        )
        step = _report(
            _prop_modified("Door.IsOpen", parent="Door", output_type="Int"),
            paths=[_door_entries(unchanged=("Left", "Right"))],
        )
        tables = _run(self.CFG, setup, step)
        superseded = tables["bindings"][tables["bindings"]["status"] == ElementStatus.SUPERSEDED]
        assert len(superseded) == 2
        assert set(_active(tables)["runtime_path"]) == {"Door.Left.IsOpen", "Door.Right.IsOpen"}

    def test_rename_with_breaking_change(self) -> None:
        """Rename on a breaking MODIFIED event updates label and creates new variant."""
        tables = _run(
            self.CFG,
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Velocity", parent="Vehicle", output_type="Int")),
            _report(
                _prop_modified("Vehicle.Speed", parent="Vehicle", renamed_from="Vehicle.Velocity", output_type="Float")
            ),
        )
        row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]
        assert "Vehicle.Velocity" in json.loads(row["previous_labels"])
        assert _active(tables).iloc[0]["runtime_path"] == "Vehicle.Speed"


class TestPropertyPathShapeChange:
    """A change in the dimension shape of a property's bindings is always breaking, independent of config."""

    def _container(self) -> DiffReport:
        return _report(
            _entity_added("Container", instances={"d": ["A", "B"]}),
            _prop_added("Container.Member", parent="Container"),
        )

    def _to_instanced(self) -> dict:
        step = _report(
            _prop_modified("Container.Member", parent="Container"),
            paths=[
                _paths(
                    "Container.Member",
                    "Container",
                    ({}, "Container.Member", "removed"),
                    ({"Container": {"d": "A"}}, "Container[A].Member", "added"),
                    ({"Container": {"d": "B"}}, "Container[B].Member", "added"),
                )
            ],
        )
        return _run(_cfg(), self._container(), step)

    def test_singleton_to_instanced_forces_new_contract(self) -> None:
        """Replacing a singleton binding with tagged bindings mints a new contract even with an empty config."""
        tables = self._to_instanced()
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]["concept_uri"]
        contracts = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
        assert len(contracts) == 2
        assert (contracts["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (contracts["status"] == ElementStatus.ACTIVE).sum() == 1

    def test_singleton_to_instanced_replaces_bindings(self) -> None:
        """The singleton binding is superseded and one binding per instance is minted on the new contract."""
        tables = self._to_instanced()
        superseded = tables["bindings"][tables["bindings"]["status"] == ElementStatus.SUPERSEDED]
        assert len(superseded) == 1
        assert set(_active(tables)["runtime_path"]) == {"Container[A].Member", "Container[B].Member"}

    def test_instanced_to_singleton_forces_new_contract(self) -> None:
        """Replacing tagged bindings with a singleton binding mints a new contract."""
        setup = _report(
            _entity_added("Container", instances={"d": ["A", "B"]}),
            _prop_added("Container.Member", parent="Container"),
            paths=[
                _paths(
                    "Container.Member",
                    "Container",
                    ({"Container": {"d": "A"}}, "Container[A].Member", "added"),
                    ({"Container": {"d": "B"}}, "Container[B].Member", "added"),
                )
            ],
        )
        step = _report(
            _prop_modified("Container.Member", parent="Container"),
            paths=[
                _paths(
                    "Container.Member",
                    "Container",
                    ({"Container": {"d": "A"}}, "Container[A].Member", "removed"),
                    ({"Container": {"d": "B"}}, "Container[B].Member", "removed"),
                    ({}, "Container.Member", "added"),
                )
            ],
        )
        tables = _run(_cfg(), setup, step)
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 2
        assert (tables["bindings"]["status"] == ElementStatus.SUPERSEDED).sum() == 2
        assert _active(tables)["runtime_path"].tolist() == ["Container.Member"]

    def test_same_shape_path_change_is_not_breaking(self) -> None:
        """Adding a path of the same shape keeps the contract."""
        setup = _report(
            _entity_added("Container", instances={"d": ["A"]}),
            _prop_added("Container.Member", parent="Container"),
            paths=[
                _paths("Container.Member", "Container", ({"Container": {"d": "A"}}, "Container[A].Member", "added"))
            ],
        )
        step = _report(
            _entity_modified("Container", instances_added={"d": ["B"]}),
            paths=[
                _paths(
                    "Container.Member",
                    "Container",
                    ({"Container": {"d": "A"}}, "Container[A].Member", "unchanged"),
                    ({"Container": {"d": "B"}}, "Container[B].Member", "added"),
                )
            ],
        )
        tables = _run(_cfg(), setup, step)
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 1

    def test_ledger_validates_after_shape_change(self) -> None:
        """Full ledger validation passes after a shape change."""
        validate_ledger(self._to_instanced())


class TestPropertyModifiedBindingEligibleTransition:
    """A change in the effective binding-eligibility outcome (is_leaf OR binding_eligible) is
    always breaking, independent of config, and binding_eligible can never suppress a leaf's binding.
    """

    def _object(self) -> DiffReport:
        return _report(
            _entity_added("Object"),
            _prop_added("Object.Position", parent="Object", is_leaf=False, output_type="Structs.Position"),
        )

    def test_non_eligible_to_eligible_forces_new_contract_and_mints_binding(self) -> None:
        """binding_eligible flipping False->True on a non-leaf property mints a new contract and a binding."""
        step = _report(
            _prop_modified(
                "Object.Position", parent="Object", is_leaf=False, binding_eligible=True, output_type="Structs.Position"
            ),
            paths=[_paths("Object.Position", "Object", ({}, "Object.Position", "added"))],
        )
        tables = _run(_cfg(), self._object(), step)
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Object.Position"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 2
        assert len(_active(tables)) == 1

    def test_eligible_to_non_eligible_forces_new_contract_and_supersedes_binding(self) -> None:
        """binding_eligible flipping True->False on a non-leaf property supersedes the binding and mints none."""
        setup = _report(
            _entity_added("Object"),
            _prop_added(
                "Object.Position", parent="Object", is_leaf=False, binding_eligible=True, output_type="Structs.Position"
            ),
        )
        step = _report(
            _prop_modified("Object.Position", parent="Object", is_leaf=False, output_type="Structs.Position")
        )
        tables = _run(_cfg(), setup, step)
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Object.Position"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 2
        assert (tables["bindings"]["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert len(_active(tables)) == 0

    def test_leaf_property_binding_eligible_flip_is_not_a_transition(self) -> None:
        """On a leaf property, flipping binding_eligible never changes the effective outcome, so no new contract."""
        tables = _run(
            _cfg(),
            _report(
                _entity_added("Vehicle"),
                _prop_added("Vehicle.Speed", parent="Vehicle", is_leaf=True, output_type="Float"),
            ),
            _report(
                _prop_modified(
                    "Vehicle.Speed", parent="Vehicle", is_leaf=True, binding_eligible=True, output_type="Float"
                )
            ),
        )
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 1
        assert len(tables["bindings"]) == 1

    def test_ledger_validates_after_binding_eligible_transition(self) -> None:
        """Full ledger validation passes after a binding_eligible transition on a non-leaf property."""
        step = _report(
            _prop_modified(
                "Object.Position", parent="Object", is_leaf=False, binding_eligible=True, output_type="Structs.Position"
            ),
            paths=[_paths("Object.Position", "Object", ({}, "Object.Position", "added"))],
        )
        validate_ledger(_run(_cfg(), self._object(), step))


# ── Property MODIFIED (non-breaking) ─────────────────────────────────────────


class TestPropertyModifiedNonBreaking:
    CFG = _cfg(property={"description": False})

    def _modified(self) -> dict:
        return _run(
            self.CFG,
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle")),
            _report(_prop_modified("Vehicle.Speed", parent="Vehicle", description="better docs")),
        )

    def test_no_new_variant(self) -> None:
        """Non-breaking property MODIFIED leaves the variant unchanged."""
        tables = self._modified()
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 1

    def test_no_new_bindings(self) -> None:
        """Non-breaking property MODIFIED does not touch bindings."""
        tables = self._modified()
        assert len(tables["bindings"]) == 1
        assert tables["bindings"].iloc[0]["status"] == ElementStatus.ACTIVE


# ── Property REMOVED ──────────────────────────────────────────────────────────


class TestPropertyRemoved:
    def _removed(self) -> dict:
        return _run(
            _cfg(),
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle")),
            _report(_prop_removed("Vehicle.Speed", parent="Vehicle")),
        )

    def test_concept_status_removed(self) -> None:
        """Property REMOVED sets concept status to REMOVED."""
        tables = self._removed()
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]
        assert prop_row["status"] == ElementStatus.REMOVED

    def test_revision_status_removed(self) -> None:
        """Property REMOVED mints a final REMOVED revision."""
        tables = self._removed()
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]["concept_uri"]
        prop_revs = tables["revisions"][tables["revisions"]["concept_uri"] == prop_uri]
        assert (prop_revs["status"] == ElementStatus.REMOVED).sum() == 1

    def test_binding_status_removed(self) -> None:
        """All bindings for a REMOVED property are marked REMOVED."""
        assert (self._removed()["bindings"]["status"] == ElementStatus.REMOVED).all()

    def test_enum_value_removed_no_binding_touched(self) -> None:
        """ENUM_VALUE REMOVED does not attempt to supersede any bindings."""
        tables = _run(
            _cfg(),
            _report(
                _entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET),
                _prop_added("SpeedUnit.KMH", parent="SpeedUnit", kind=ElementKind.ENUM_VALUE),
            ),
            _report(_prop_removed("SpeedUnit.KMH", parent="SpeedUnit")),
        )
        assert len(tables["bindings"]) == 0

    def test_property_paths_for_removed_property_fail(self) -> None:
        """Reporting paths for a property that the same report removes raises SyncError."""
        tables = _run(
            _cfg(),
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle")),
        )
        step = _report(
            _prop_removed("Vehicle.Speed", parent="Vehicle"),
            paths=[_paths("Vehicle.Speed", "Vehicle", ({}, "Vehicle.Speed", "unchanged"))],
        )
        with pytest.raises(SyncError, match="not an ACTIVE PROPERTY"):
            sync(tables, step, _meta(), _cfg())


# ── Label namespace scoping (ENTITY/PROPERTY may share a label; PROPERTY labels are ──
# ── unique only among siblings of the same parent) ───────────────────────────────────


class TestLabelNamespaceScoping:
    def test_entity_and_property_share_label(self) -> None:
        """An ENTITY and a PROPERTY (under a different parent) may share the same label."""
        report = _report(
            _entity_added("Door"),
            _entity_added("Vehicle"),
            _prop_added("Door", parent="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        entity_uri = tables["concepts"][
            (tables["concepts"]["current_label"] == "Door") & (tables["concepts"]["kind"] == ElementKind.ENTITY)
        ].iloc[0]["concept_uri"]
        prop_uri = tables["concepts"][
            (tables["concepts"]["current_label"] == "Door") & (tables["concepts"]["kind"] == ElementKind.PROPERTY)
        ].iloc[0]["concept_uri"]
        assert entity_uri != prop_uri
        validate_ledger(tables)  # must not raise

    def test_same_property_label_different_parents_modify_disambiguated(self) -> None:
        """MODIFY on a sibling-scoped label updates only the property under the matching parent."""
        cfg = _cfg(property={"output_type": True})
        setup = _report(
            _entity_added("Left"),
            _entity_added("Right"),
            _prop_added("IsOpen", parent="Left", output_type="Boolean"),
            _prop_added("IsOpen", parent="Right", output_type="Boolean"),
        )
        tables = sync(empty_ledger(), setup, _meta(), cfg)
        report = _report(_prop_modified("IsOpen", parent="Left", output_type="Int"))
        tables = sync(tables, report, _meta(), cfg)

        left_uri = tables["concepts"][
            (tables["concepts"]["current_label"] == "IsOpen") & (tables["concepts"]["kind"] == ElementKind.PROPERTY)
        ]
        left_uri = left_uri[
            left_uri["parent_uri"]
            == tables["concepts"][tables["concepts"]["current_label"] == "Left"].iloc[0]["concept_uri"]
        ].iloc[0]["concept_uri"]
        right_uri = tables["concepts"][
            (tables["concepts"]["current_label"] == "IsOpen") & (tables["concepts"]["kind"] == ElementKind.PROPERTY)
        ]
        right_uri = right_uri[
            right_uri["parent_uri"]
            == tables["concepts"][tables["concepts"]["current_label"] == "Right"].iloc[0]["concept_uri"]
        ].iloc[0]["concept_uri"]

        left_variants = tables["contracts"][tables["contracts"]["concept_uri"] == left_uri]
        right_variants = tables["contracts"][tables["contracts"]["concept_uri"] == right_uri]
        assert len(left_variants) == 2  # superseded + new active variant
        assert len(right_variants) == 1  # untouched

    def test_same_property_label_different_parents_remove_disambiguated(self) -> None:
        """REMOVE on a sibling-scoped label removes only the property under the matching parent."""
        setup = _report(
            _entity_added("Left"),
            _entity_added("Right"),
            _prop_added("IsOpen", parent="Left"),
            _prop_added("IsOpen", parent="Right"),
        )
        tables = sync(empty_ledger(), setup, _meta(), _cfg())
        report = _report(_prop_removed("IsOpen", parent="Left"))
        tables = sync(tables, report, _meta(), _cfg())

        concepts = tables["concepts"]
        props = concepts[(concepts["current_label"] == "IsOpen") & (concepts["kind"] == ElementKind.PROPERTY)]
        left_entity_uri = concepts[concepts["current_label"] == "Left"].iloc[0]["concept_uri"]
        right_entity_uri = concepts[concepts["current_label"] == "Right"].iloc[0]["concept_uri"]
        left_prop = props[props["parent_uri"] == left_entity_uri].iloc[0]
        right_prop = props[props["parent_uri"] == right_entity_uri].iloc[0]
        assert left_prop["status"] == ElementStatus.REMOVED
        assert right_prop["status"] == ElementStatus.ACTIVE


# ── Round-trip ────────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_write_read_validate(self, tmp_path) -> None:
        """sync → write_ledger → read_ledger → validate_ledger round-trips cleanly."""
        from modl.ledger import read_ledger, write_ledger

        cfg = _cfg(entity={"instances": True}, property={"output_type": True})
        report = _report(
            _entity_added("Door", instances={"side": ["Left", "Right"]}),
            _prop_added("Door.IsOpen", parent="Door", output_type="Boolean"),
            _prop_added("Door.IsLocked", parent="Door", output_type="Boolean"),
            paths=[
                _door_entries(added=("Left", "Right")),
                _door_entries(added=("Left", "Right"), prop="IsLocked"),
            ],
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        ledger_dir = tmp_path / "ledger"
        write_ledger(tables, ledger_dir)
        reloaded = read_ledger(ledger_dir)
        validate_ledger(reloaded)  # must not raise

    def test_incremental_sync(self, tmp_path) -> None:
        """Two successive syncs accumulate rows correctly."""
        from modl.ledger import read_ledger, write_ledger

        cfg = _cfg(property={"output_type": True})
        ledger_dir = tmp_path / "ledger"

        # First run: add entity + property
        r1 = _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Int"))
        t1 = sync(empty_ledger(), r1, _meta(), cfg)
        write_ledger(t1, ledger_dir)

        # Second run: modify property (breaking)
        t_loaded = read_ledger(ledger_dir)
        r2 = _report(_prop_modified("Vehicle.Speed", parent="Vehicle", output_type="Float"))
        t2 = sync(t_loaded, r2, _meta(), cfg)
        write_ledger(t2, ledger_dir)

        t_final = read_ledger(ledger_dir)
        validate_ledger(t_final)

        prop_uri = t_final["concepts"][t_final["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]["concept_uri"]
        prop_variants = t_final["contracts"][t_final["contracts"]["concept_uri"] == prop_uri]
        assert len(prop_variants) == 2
        assert (prop_variants["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (prop_variants["status"] == ElementStatus.ACTIVE).sum() == 1


# ── revision_aspects ──────────────────────────────────────────────────────────


class TestRevisionAspectsAdded:
    def test_entity_added_mints_one_row_per_aspect(self) -> None:
        """ADDED entity event mints one revision_aspects row per aspect key with operation='added'."""
        cfg = _cfg()
        report = _report(_entity_added("Vehicle", type="branch"))
        tables = sync(empty_ledger(), report, _meta(), cfg)
        rows = tables["revision_aspects"]
        assert len(rows) == 1
        row = rows.iloc[0]
        assert row["aspect_key"] == "type"
        assert row["operation"] == "added"
        assert row["previous_value"] is None
        assert json.loads(row["newer_value"]) == "branch"

    def test_property_added_mints_one_row_per_aspect(self) -> None:
        """ADDED property event mints one revision_aspects row per aspect key with operation='added'."""
        cfg = _cfg()
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Float", unit="km/h"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        rows = tables["revision_aspects"]
        assert len(rows) == 2
        assert set(rows["aspect_key"]) == {"output_type", "unit"}
        assert (rows["operation"] == "added").all()
        assert rows["previous_value"].isnull().all()


class TestRevisionAspectsModified:
    def test_entity_modified_mints_row_with_previous_and_newer(self) -> None:
        """MODIFIED entity event mints a revision_aspects row with both previous_value and newer_value."""
        cfg = _cfg()
        report = _report(_entity_added("Vehicle", type="branch"), _entity_modified("Vehicle", type="object"))
        tables = sync(empty_ledger(), report, _meta(), cfg)
        rows = tables["revision_aspects"]
        modified_rows = rows[rows["operation"] == "modified"]
        assert len(modified_rows) == 1
        row = modified_rows.iloc[0]
        assert row["aspect_key"] == "type"
        assert json.loads(row["previous_value"]) == "old_type"
        assert json.loads(row["newer_value"]) == "object"

    def test_property_modified_mints_row_with_previous_and_newer(self) -> None:
        """MODIFIED property event mints a revision_aspects row with both previous_value and newer_value."""
        cfg = _cfg(property={"output_type": True})
        tables = _run(
            cfg,
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Int")),
            _report(_prop_modified("Vehicle.Speed", parent="Vehicle", output_type="Float")),
        )
        rows = tables["revision_aspects"]
        modified_rows = rows[rows["operation"] == "modified"]
        assert len(modified_rows) == 1
        row = modified_rows.iloc[0]
        assert row["aspect_key"] == "output_type"
        assert json.loads(row["previous_value"]) == "old_output_type"
        assert json.loads(row["newer_value"]) == "Float"

    def test_instance_structural_keys_excluded(self) -> None:
        """instances_added / instances_removed keys are excluded from revision_aspects rows."""
        cfg = _cfg(entity={"instances.added": True})
        tables = _run(
            cfg,
            _report(_entity_added("Door", instances={"side": ["Left", "Right"]})),
            _report(_entity_modified("Door", instances_added={"side": ["Center"]})),
        )
        assert len(tables["revision_aspects"]) == 0


class TestRevisionAspectsRemoved:
    def test_entity_removed_mints_row_per_previous_aspect(self) -> None:
        """REMOVED entity event mints one revision_aspects row per previous_aspects key with operation='removed'."""
        cfg = _cfg()
        report = _report(_entity_added("Vehicle", type="branch"), _entity_removed("Vehicle", type="branch"))
        tables = sync(empty_ledger(), report, _meta(), cfg)
        rows = tables["revision_aspects"]
        removed_rows = rows[rows["operation"] == "removed"]
        assert len(removed_rows) == 1
        row = removed_rows.iloc[0]
        assert row["aspect_key"] == "type"
        assert json.loads(row["previous_value"]) == "branch"
        assert row["newer_value"] is None

    def test_property_removed_mints_row_per_previous_aspect(self) -> None:
        """REMOVED property event mints one revision_aspects row per previous_aspects key with operation='removed'."""
        cfg = _cfg()
        tables = _run(
            cfg,
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Float")),
            _report(_prop_removed("Vehicle.Speed", parent="Vehicle", output_type="Float")),
        )
        rows = tables["revision_aspects"]
        removed_rows = rows[rows["operation"] == "removed"]
        assert len(removed_rows) == 1
        row = removed_rows.iloc[0]
        assert row["aspect_key"] == "output_type"
        assert json.loads(row["previous_value"]) == "Float"
        assert row["newer_value"] is None


# ── Second rename ─────────────────────────────────────────────────────────────


class TestEntitySecondRename:
    def test_accumulates_both_previous_labels(self) -> None:
        """Two successive renames keep both old labels in previous_labels, most-recent first."""
        report = _report(
            _entity_added("Vehicl"),
            _entity_modified("Vehicle", renamed_from="Vehicl"),
            _entity_modified("VehicleNode", renamed_from="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        row = tables["concepts"].iloc[0]
        assert row["current_label"] == "VehicleNode"
        prev = json.loads(row["previous_labels"])
        assert prev == ["Vehicle", "Vehicl"]

    def test_three_renames_full_history(self) -> None:
        """Three successive renames accumulate all three old labels in order."""
        report = _report(
            _entity_added("A"),
            _entity_modified("B", renamed_from="A"),
            _entity_modified("C", renamed_from="B"),
            _entity_modified("D", renamed_from="C"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        row = tables["concepts"].iloc[0]
        assert row["current_label"] == "D"
        assert json.loads(row["previous_labels"]) == ["C", "B", "A"]


# ── renamed_from pointing to non-existent concept ─────────────────────────────


class TestRenameNonexistentConcept:
    def test_entity_renamed_from_unknown_label_raises(self) -> None:
        """Entity renamed_from pointing to an absent label raises SyncError."""
        report = _report(_entity_modified("Vehicle", renamed_from="Vehicl"))
        with pytest.raises(SyncError, match="No concept found"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_property_renamed_from_unknown_label_raises(self) -> None:
        """Property renamed_from pointing to an absent label raises SyncError."""
        report = _report(
            _entity_added("Vehicle"),
            _prop_modified("Vehicle.Velocity", parent="Vehicle", renamed_from="Vehicle.Speed"),
        )
        with pytest.raises(SyncError, match="No concept found"):
            sync(empty_ledger(), report, _meta(), _cfg())


# ── ENUMERATION_SET ADDED ─────────────────────────────────────────────────────


class TestEnumerationSetAdded:
    def test_kind_stored_correctly(self) -> None:
        """ENUMERATION_SET ADDED stores the correct kind on the concept row."""
        report = _report(_entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert tables["concepts"].iloc[0]["kind"] == ElementKind.ENUMERATION_SET

    def test_no_bindings(self) -> None:
        """ENUMERATION_SET ADDED does not create any bindings."""
        report = _report(_entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 0

    def test_mints_concept_revision_variant(self) -> None:
        """ENUMERATION_SET ADDED mints exactly one concept, revision, and variant."""
        report = _report(_entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["concepts"]) == 1
        assert len(tables["revisions"]) == 1
        assert len(tables["contracts"]) == 1

    def test_enum_value_parent_uri_links_to_set(self) -> None:
        """ENUM_VALUE concept carries the ENUMERATION_SET concept URI as parent_uri."""
        report = _report(
            _entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET),
            _prop_added("SpeedUnit.KMH", parent="SpeedUnit", kind=ElementKind.ENUM_VALUE),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        set_uri = tables["concepts"][tables["concepts"]["current_label"] == "SpeedUnit"].iloc[0]["concept_uri"]
        val_row = tables["concepts"][tables["concepts"]["current_label"] == "SpeedUnit.KMH"].iloc[0]
        assert val_row["parent_uri"] == set_uri

    def test_ledger_validates_after_enum_set_and_value(self) -> None:
        """Full ledger validation passes after ENUMERATION_SET + ENUM_VALUE ADDED."""
        report = _report(
            _entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET),
            _prop_added("SpeedUnit.KMH", parent="SpeedUnit", kind=ElementKind.ENUM_VALUE),
        )
        validate_ledger(sync(empty_ledger(), report, _meta(), _cfg()))


# ── Rename AND breaking aspect in the same event ──────────────────────────────


class TestEntityModifiedRenameAndBreaking:
    def test_both_applied_simultaneously(self) -> None:
        """Breaking MODIFIED with rename updates the label and creates a new variant in one event."""
        cfg = _cfg(entity={"type": True})
        report = _report(
            _entity_added("Vehicl", type="branch"),
            _entity_modified("Vehicle", renamed_from="Vehicl", type="object"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        row = tables["concepts"].iloc[0]
        assert row["current_label"] == "Vehicle"
        assert "Vehicl" in json.loads(row["previous_labels"])
        variants = tables["contracts"]
        assert len(variants) == 2
        assert (variants["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (variants["status"] == ElementStatus.ACTIVE).sum() == 1


class TestPropertyModifiedRenameAndNonBreaking:
    def test_rename_non_breaking_updates_label_no_new_variant(self) -> None:
        """Non-breaking property MODIFIED with rename updates label but does not create a new variant."""
        cfg = _cfg(property={"description": False})
        tables = _run(
            cfg,
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Vel", parent="Vehicle")),
            _report(
                _prop_modified("Vehicle.Velocity", parent="Vehicle", renamed_from="Vehicle.Vel", description="updated")
            ),
        )
        row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Velocity"].iloc[0]
        assert "Vehicle.Vel" in json.loads(row["previous_labels"])
        prop_uri = row["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 1


# ── Instance list shrinks (non-breaking) ──────────────────────────────────────


class TestEntityModifiedInstanceShrinks:
    def _shrunk(self) -> dict:
        step = _report(
            _entity_modified("Door", instances_removed={"side": ["Rear"]}),
            paths=[_door_entries(unchanged=("Left", "Right"), removed=("Rear",))],
        )
        return _run(_cfg(entity={"instances": False}), _door_setup("Left", "Right", "Rear"), step)

    def test_shrink_mints_no_new_bindings(self) -> None:
        """Non-breaking instance removal does not create new bindings."""
        assert len(self._shrunk()["bindings"]) == 3

    def test_shrink_updates_entity_instances_column(self) -> None:
        """Entity concept row reflects the reduced instance list after a shrink."""
        tables = self._shrunk()
        row = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]
        assert json.loads(row["instances"]) == {"side": ["Left", "Right"]}

    def test_shrink_removed_binding_marked_removed(self) -> None:
        """The binding of the removed instance path is marked REMOVED after a shrink."""
        tables = self._shrunk()
        removed = tables["bindings"][tables["bindings"]["status"] == ElementStatus.REMOVED]
        assert removed["runtime_path"].tolist() == ["Door.Rear.IsOpen"]

    def test_property_concepts_carry_no_instances(self) -> None:
        """Only ENTITY concepts store instances; property concepts keep the column empty."""
        tables = self._shrunk()
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]
        assert prop_row["instances"] is None or str(prop_row["instances"]) == "nan"


# ── Multiple child properties ─────────────────────────────────────────────────


class TestMultipleChildPropertiesCascade:
    def _grown(self, instances_breaking: bool) -> dict:
        cfg = _cfg(entity={"instances": instances_breaking})
        setup = _report(
            _entity_added("Door", instances={"side": ["Left", "Right"]}),
            _prop_added("Door.IsOpen", parent="Door"),
            _prop_added("Door.IsLocked", parent="Door"),
            paths=[
                _door_entries(added=("Left", "Right")),
                _door_entries(added=("Left", "Right"), prop="IsLocked"),
            ],
        )
        step = _report(
            _entity_modified("Door", instances_added={"side": ["Center"]}),
            paths=[
                _door_entries(unchanged=("Left", "Right"), added=("Center",)),
                _door_entries(unchanged=("Left", "Right"), added=("Center",), prop="IsLocked"),
            ],
        )
        return _run(cfg, setup, step)

    def test_breaking_instance_adds_binding_for_all_children(self) -> None:
        """Instance addition adds new bindings for every dependent property."""
        tables = self._grown(True)
        assert len(tables["bindings"]) == 6
        assert (tables["bindings"]["status"] == ElementStatus.ACTIVE).all()

    def test_breaking_instance_contracts_unchanged_for_all_children(self) -> None:
        """An instance change never creates new contracts for any child property."""
        tables = self._grown(True)
        for label in ("Door.IsOpen", "Door.IsLocked"):
            prop_uri = tables["concepts"][tables["concepts"]["current_label"] == label].iloc[0]["concept_uri"]
            prop_contracts = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
            assert len(prop_contracts) == 1, label
            assert prop_contracts.iloc[0]["status"] == ElementStatus.ACTIVE

    def test_nonbreaking_instance_adds_bindings_for_all_children(self) -> None:
        """Non-breaking instance addition adds new bindings for every dependent property."""
        tables = self._grown(False)
        assert len(tables["bindings"]) == 6
        assert (tables["bindings"]["status"] == ElementStatus.ACTIVE).all()

    def test_missing_dependent_property_fails_and_leaves_ledger_untouched(self) -> None:
        """Omitting one dependent property from the report raises SyncError."""
        cfg = _cfg(entity={"instances": False})
        setup = _report(
            _entity_added("Door", instances={"side": ["Left"]}),
            _prop_added("Door.IsOpen", parent="Door"),
            _prop_added("Door.IsLocked", parent="Door"),
            paths=[_door_entries(added=("Left",)), _door_entries(added=("Left",), prop="IsLocked")],
        )
        tables = _run(cfg, setup)
        step = _report(
            _entity_modified("Door", instances_added={"side": ["Center"]}),
            paths=[_door_entries(unchanged=("Left",), added=("Center",))],
        )
        with pytest.raises(SyncError, match="IsLocked"):
            sync(tables, step, _meta(), cfg)


# ── Three successive syncs — serial continuity ────────────────────────────────


class TestThreeSuccessiveSyncs:
    def test_serial_continuity_across_three_syncs(self, tmp_path) -> None:
        """Three successive syncs produce unique, monotonically-increasing serials in every table."""
        from modl.ledger import read_ledger, write_ledger

        cfg = _cfg(property={"output_type": True})
        ledger_dir = tmp_path / "ledger"

        r1 = _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Int"))
        write_ledger(sync(empty_ledger(), r1, _meta(), cfg), ledger_dir)

        r2 = _report(_prop_modified("Vehicle.Speed", parent="Vehicle", output_type="Float"))
        write_ledger(sync(read_ledger(ledger_dir), r2, _meta(), cfg), ledger_dir)

        r3 = _report(_prop_added("Vehicle.Mass", parent="Vehicle", output_type="Float"))
        write_ledger(sync(read_ledger(ledger_dir), r3, _meta(), cfg), ledger_dir)

        final = read_ledger(ledger_dir)
        validate_ledger(final)
        for table in ("concepts", "revisions", "contracts", "bindings"):
            serials = sorted(final[table]["serial"].tolist())
            unique_serials = sorted(set(serials))
            assert serials == unique_serials, f"Duplicate serials in {table}"
            assert serials == list(range(len(serials))), f"Serial gap in {table}: {serials}"

    def test_incremental_concept_referenced_by_label_across_syncs(self, tmp_path) -> None:
        """A concept added in run 1 can be modified by label in run 3 after a no-op run 2."""
        from modl.ledger import read_ledger, write_ledger

        cfg = _cfg(entity={"type": True})
        ledger_dir = tmp_path / "ledger"

        r1 = _report(_entity_added("Vehicle", type="branch"))
        write_ledger(sync(empty_ledger(), r1, _meta(), cfg), ledger_dir)

        # Run 2: unrelated change
        r2 = _report(_entity_added("Door"))
        write_ledger(sync(read_ledger(ledger_dir), r2, _meta(), cfg), ledger_dir)

        # Run 3: break Vehicle added in run 1
        r3 = _report(_entity_modified("Vehicle", type="object"))
        write_ledger(sync(read_ledger(ledger_dir), r3, _meta(), cfg), ledger_dir)

        final = read_ledger(ledger_dir)
        validate_ledger(final)
        vehicle_uri = final["concepts"][final["concepts"]["current_label"] == "Vehicle"].iloc[0]["concept_uri"]
        v_variants = final["contracts"][final["contracts"]["concept_uri"] == vehicle_uri]
        assert len(v_variants) == 2


# ── New tests for implemented improvements ────────────────────────────────────


class TestSimultaneousInstanceAddAndRemove:
    def _swap(self, cfg: BreakingChangeConfig) -> dict:
        step = _report(
            _entity_modified("Door", instances_added={"side": ["Center"]}, instances_removed={"side": ["Right"]}),
            paths=[_door_entries(unchanged=("Left",), added=("Center",), removed=("Right",))],
        )
        return _run(cfg, _door_setup("Left", "Right"), step)

    @staticmethod
    def _door_contracts(tables: dict) -> int:
        entity_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]["concept_uri"]
        return len(tables["contracts"][tables["contracts"]["concept_uri"] == entity_uri])

    def test_both_fire_breaking_when_added_is_breaking(self) -> None:
        """When instances.added: true and instances.removed: false, a simultaneous add+remove is breaking."""
        tables = self._swap(_cfg(entity={"instances.added": True, "instances.removed": False}))
        assert self._door_contracts(tables) == 2

    def test_both_fire_breaking_when_removed_is_breaking(self) -> None:
        """When instances.removed: true and instances.added: false, a simultaneous add+remove is breaking."""
        tables = self._swap(_cfg(entity={"instances.added": False, "instances.removed": True}))
        assert self._door_contracts(tables) == 2

    def test_both_non_breaking_when_both_declared_false(self) -> None:
        """When both instances.added: false and instances.removed: false, the event is non-breaking."""
        tables = self._swap(_cfg(entity={"instances.added": False, "instances.removed": False}))
        assert self._door_contracts(tables) == 1


# Content-derived breaking (properties.added / properties.removed / values.removed)
class TestContentDerivedBreaking:
    @staticmethod
    def _entity_contracts(tables: dict, label: str) -> pd.DataFrame:
        uri = tables["concepts"][tables["concepts"]["current_label"] == label].iloc[0]["concept_uri"]
        return tables["contracts"][tables["contracts"]["concept_uri"] == uri]

    def _remove_speed(self, cfg: BreakingChangeConfig) -> dict:
        return _run(
            cfg,
            _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle")),
            _report(
                EntityChanged(
                    label="Vehicle",
                    change_type=ChangeType.MODIFIED,
                    content=[ContentItem(label="Vehicle.Speed", change_type=ChangeType.REMOVED)],
                ),
                _prop_removed("Vehicle.Speed", parent="Vehicle"),
            ),
        )

    def test_properties_removed_breaking_mints_new_entity_contract(self) -> None:
        """properties.removed: true in config + REMOVED content item triggers new entity contract."""
        contracts = self._entity_contracts(self._remove_speed(_cfg(entity={"properties.removed": True})), "Vehicle")
        assert len(contracts) == 2
        assert (contracts["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (contracts["status"] == ElementStatus.ACTIVE).sum() == 1

    def test_properties_removed_false_no_new_entity_contract(self) -> None:
        """properties.removed: false + REMOVED content item does not trigger new entity contract."""
        tables = self._remove_speed(_cfg(entity={"properties.removed": False}))
        assert len(self._entity_contracts(tables, "Vehicle")) == 1

    def test_properties_added_breaking_mints_new_entity_contract(self) -> None:
        """properties.added: true + ADDED content item triggers new entity contract."""
        cfg = _cfg(entity={"properties.added": True})
        report = _report(
            _entity_added("Vehicle"),
            EntityChanged(
                label="Vehicle",
                change_type=ChangeType.MODIFIED,
                content=[ContentItem(label="Vehicle.Mass", change_type=ChangeType.ADDED)],
            ),
            _prop_added("Vehicle.Mass", parent="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        assert len(self._entity_contracts(tables, "Vehicle")) == 2

    def test_empty_content_no_new_entity_contract(self) -> None:
        """Entity MODIFIED with no content items does not trigger content-derived breaking."""
        cfg = _cfg(entity={"properties.removed": True})
        report = _report(_entity_added("Vehicle"), _entity_modified("Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), cfg)
        assert len(self._entity_contracts(tables, "Vehicle")) == 1

    def test_values_removed_breaking_on_enumeration_set(self) -> None:
        """values.removed: true + REMOVED content item on ENUMERATION_SET triggers new contract."""
        cfg = _cfg(enumeration_set={"values.removed": True})
        tables = _run(
            cfg,
            _report(
                _entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET),
                _prop_added("SpeedUnit.KMH", parent="SpeedUnit", kind=ElementKind.ENUM_VALUE),
            ),
            _report(
                EntityChanged(
                    label="SpeedUnit",
                    kind=ElementKind.ENUMERATION_SET,
                    change_type=ChangeType.MODIFIED,
                    content=[ContentItem(label="SpeedUnit.KMH", change_type=ChangeType.REMOVED)],
                ),
                _prop_removed("SpeedUnit.KMH", parent="SpeedUnit"),
            ),
        )
        assert len(self._entity_contracts(tables, "SpeedUnit")) == 2


class TestInstanceDuplicateValidation:
    def test_duplicate_in_added_snapshot_raises(self) -> None:
        """Duplicate values within a dimension of an ADDED event are rejected by the IR."""
        with pytest.raises(ValidationError, match="duplicate"):
            _entity_added("Door", instances={"side": ["Left", "Left"]})

    def test_duplicate_in_instances_added_raises(self) -> None:
        """Duplicate values in instances_added on a MODIFIED event are rejected by the IR."""
        with pytest.raises(ValidationError, match="duplicate"):
            _entity_modified("Door", instances_added={"side": ["Left", "Left"]})

    def test_overlap_with_existing_instances_raises(self) -> None:
        """instances_added containing a value already in the stored dictionary raises SyncError."""
        tables = _run(_cfg(), _report(_entity_added("Door", instances={"side": ["Left", "Right"]})))
        with pytest.raises(SyncError, match="already stored"):
            sync(tables, _report(_entity_modified("Door", instances_added={"side": ["Left"]})), _meta(), _cfg())


# ── Nested instanced entities (Axle > Wheel > Tire) ───────────────────────────


def _axle_tags(row: str, side: str) -> dict:
    return {"Axle": {"row": row}, "Wheel": {"side": side}}


def _axle_paths(label: str, suffix: str, *, added=(), unchanged=(), removed=(), axle: str = "Axle") -> PropertyPaths:
    """Paths for a property under Wheel, one per (row, side) pair, tagged with both entities."""
    entries = [
        ({axle: {"row": r}, "Wheel": {"side": sd}}, f"{axle}{r}.Wheel{sd}.{suffix}", change)
        for change, pairs in (("added", added), ("unchanged", unchanged), ("removed", removed))
        for r, sd in pairs
    ]
    return _paths(label, "Wheel", *entries)


_ROWS = ("1", "2", "3")
_SIDES = ("L", "R")
_GRID = tuple((r, sd) for r in _ROWS for sd in _SIDES)


def _axle_setup() -> DiffReport:
    return _report(
        _entity_added("Axle", instances={"row": list(_ROWS)}),
        _entity_added("Wheel", instances={"side": list(_SIDES)}),
        _prop_added("Wheel.Speed", parent="Wheel"),
        _prop_added("Wheel.Pressure", parent="Wheel"),
        paths=[
            _axle_paths("Wheel.Speed", "Speed", added=_GRID),
            _axle_paths("Wheel.Pressure", "Tire.Pressure", added=_GRID),
        ],
    )


class TestNestedInstanceBindings:
    def test_one_binding_per_combination_for_each_leaf(self) -> None:
        """Each leaf under two instanced entities gets one binding per (row, side) combination."""
        tables = _run(_cfg(), _axle_setup())
        bindings = tables["bindings"]
        assert len(bindings) == 12
        assert set(bindings["runtime_path"]) == {
            f"Axle{r}.Wheel{sd}.{leaf}" for r, sd in _GRID for leaf in ("Speed", "Tire.Pressure")
        }

    def test_tags_are_keyed_by_entity_concept_uri(self) -> None:
        """Stored tags name entity concept URIs, not labels."""
        tables = _run(_cfg(), _axle_setup())
        axle_uri = _concept_uri(tables, "Axle")
        wheel_uri = _concept_uri(tables, "Wheel")
        tags = json.loads(tables["bindings"].iloc[0]["instance_tags"])
        assert set(tags) == {axle_uri, wheel_uri}

    def test_adding_an_instance_mints_only_new_bindings(self) -> None:
        """Adding row 4 mints bindings for the new row only and keeps every contract."""
        step = _report(
            _entity_modified("Axle", instances_added={"row": ["4"]}),
            paths=[
                _axle_paths("Wheel.Speed", "Speed", unchanged=_GRID, added=(("4", "L"), ("4", "R"))),
                _axle_paths("Wheel.Pressure", "Tire.Pressure", unchanged=_GRID, added=(("4", "L"), ("4", "R"))),
            ],
        )
        tables = _run(_cfg(), _axle_setup(), step)
        assert len(tables["bindings"]) == 16
        assert (tables["bindings"]["status"] == ElementStatus.ACTIVE).all()
        assert len(tables["contracts"]) == 4  # Axle, Wheel, Wheel.Speed, Wheel.Pressure

    def test_removing_an_instance_removes_its_bindings(self) -> None:
        """Removing row 3 marks the bindings of both leaves for that row as REMOVED."""
        rest = tuple(g for g in _GRID if g[0] != "3")
        gone = tuple(g for g in _GRID if g[0] == "3")
        step = _report(
            _entity_modified("Axle", instances_removed={"row": ["3"]}),
            paths=[
                _axle_paths("Wheel.Speed", "Speed", unchanged=rest, removed=gone),
                _axle_paths("Wheel.Pressure", "Tire.Pressure", unchanged=rest, removed=gone),
            ],
        )
        tables = _run(_cfg(), _axle_setup(), step)
        removed = tables["bindings"][tables["bindings"]["status"] == ElementStatus.REMOVED]
        assert len(removed) == 4
        assert all(".Wheel" in path and path.startswith("Axle3") for path in removed["runtime_path"])
        assert len(_active(tables)) == 8

    def test_entity_rename_keeps_tags_and_refreshes_paths(self) -> None:
        """Renaming an entity keeps binding URIs and tags; the reported runtime paths replace the old ones."""
        before = _run(_cfg(), _axle_setup())
        step = _report(
            _entity_modified("Chassis", renamed_from="Axle"),
            paths=[
                _axle_paths("Wheel.Speed", "Speed", unchanged=_GRID, axle="Chassis"),
                _axle_paths("Wheel.Pressure", "Tire.Pressure", unchanged=_GRID, axle="Chassis"),
            ],
        )
        after = sync(before, step, _meta(), _cfg())
        assert sorted(after["bindings"]["binding_uri"]) == sorted(before["bindings"]["binding_uri"])
        assert sorted(after["bindings"]["instance_tags"]) == sorted(before["bindings"]["instance_tags"])
        assert all(path.startswith("Chassis") for path in after["bindings"]["runtime_path"])
        assert len(after["revisions"]) == len(before["revisions"]) + 1  # only the entity rename

    def test_empty_report_changes_nothing(self) -> None:
        """Syncing a report without changes leaves every table identical."""
        before = _run(_cfg(), _axle_setup())
        after = sync(before, DiffReport(changes=[]), _meta(), _cfg())
        for name, frame in before.items():
            assert after[name].equals(frame), name

    def test_missing_entry_fails(self) -> None:
        """A binding-eligible property without a property_paths entry raises SyncError."""
        report = _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle"), auto_paths=False)
        with pytest.raises(SyncError, match="no property_paths entry"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_failed_sync_does_not_modify_input_tables(self) -> None:
        """A rejected report leaves the supplied tables untouched."""
        before = _run(_cfg(), _axle_setup())
        snapshot = {name: frame.copy() for name, frame in before.items()}
        step = _report(
            _entity_modified("Axle", instances_added={"row": ["4"]}),
            paths=[_axle_paths("Wheel.Speed", "Speed", unchanged=_GRID, added=(("4", "L"), ("4", "R")))],
        )
        with pytest.raises(SyncError, match="Pressure"):
            sync(before, step, _meta(), _cfg())
        for name, frame in before.items():
            assert frame.equals(snapshot[name]), name

    def test_unchanged_set_must_match_ledger(self) -> None:
        """Omitting an existing binding from the report raises SyncError."""
        step = _report(
            _prop_modified("Wheel.Speed", parent="Wheel"),
            paths=[_axle_paths("Wheel.Speed", "Speed", unchanged=_GRID[:-1])],
        )
        with pytest.raises(SyncError, match="do not match"):
            _run(_cfg(), _axle_setup(), step)

    def test_unknown_entity_in_tags_fails(self) -> None:
        """Tags naming an entity that does not exist raise SyncError."""
        report = _report(
            _entity_added("Door", instances={"side": ["Left"]}),
            _prop_added("Door.IsOpen", parent="Door"),
            paths=[_paths("Door.IsOpen", "Door", ({"Gate": {"side": "Left"}}, "Door.Left.IsOpen", "added"))],
        )
        with pytest.raises(SyncError, match="unknown or inactive entity"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_tag_value_must_be_declared_instance(self) -> None:
        """Tags using an undeclared instance value raise SyncError."""
        report = _report(
            _entity_added("Door", instances={"side": ["Left"]}),
            _prop_added("Door.IsOpen", parent="Door"),
            paths=[_paths("Door.IsOpen", "Door", ({"Door": {"side": "Up"}}, "Door.Up.IsOpen", "added"))],
        )
        with pytest.raises(SyncError, match="not an instance declared"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_duplicate_runtime_path_fails(self) -> None:
        """Two bindings reporting the same runtime path raise SyncError."""
        report = _report(
            _entity_added("Door", instances={"side": ["Left", "Right"]}),
            _prop_added("Door.IsOpen", parent="Door"),
            paths=[
                _paths(
                    "Door.IsOpen",
                    "Door",
                    ({"Door": {"side": "Left"}}, "Door.IsOpen", "added"),
                    ({"Door": {"side": "Right"}}, "Door.IsOpen", "added"),
                )
            ],
        )
        with pytest.raises(SyncError, match="runtime_path"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_ledger_validates(self) -> None:
        """The resulting ledger passes validation."""
        validate_ledger(_run(_cfg(), _axle_setup()))


# ── Shared instance_tags across multiple bindings ──────────────────────────────


def _shared_tag_setup(*paths: tuple[str, str]) -> DiffReport:
    """A singleton property ('TypeA.Field') with one or more added paths, all tagged ``{}``."""
    return _report(
        _entity_added("TypeA"),
        _prop_added("TypeA.Field", parent="TypeA"),
        paths=[_paths("TypeA.Field", "TypeA", *[({}, path, change) for path, change in paths])],
    )


class TestSharedInstanceTags:
    """A property may have several ACTIVE bindings sharing the same instance_tags, keyed by runtime_path."""

    def test_two_added_paths_with_same_tags_mint_two_bindings(self) -> None:
        """Two added paths with empty tags each mint a distinct ACTIVE binding on the same contract."""
        report = _shared_tag_setup(("Root.X.Alpha.Field", "added"), ("Root.Y.Beta.Field", "added"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        active = _active(tables)
        assert len(active) == 2
        assert active["binding_uri"].nunique() == 2
        assert active["contract_uri"].nunique() == 1
        assert all(json.loads(t) == {} for t in active["instance_tags"])
        assert set(active["runtime_path"]) == {"Root.X.Alpha.Field", "Root.Y.Beta.Field"}

    def test_both_unchanged_leaves_ledger_untouched(self) -> None:
        """Reporting both existing paths as unchanged makes no changes to any table."""
        before = sync(
            empty_ledger(),
            _shared_tag_setup(("Root.X.Alpha.Field", "added"), ("Root.Y.Beta.Field", "added")),
            _meta(),
            _cfg(),
        )
        step = _report(
            paths=[
                _paths(
                    "TypeA.Field",
                    "TypeA",
                    ({}, "Root.X.Alpha.Field", "unchanged"),
                    ({}, "Root.Y.Beta.Field", "unchanged"),
                )
            ]
        )
        after = sync(before, step, _meta(), _cfg())
        for name, frame in before.items():
            assert after[name].equals(frame), name

    def test_one_removed_one_unchanged(self) -> None:
        """Reporting one path removed and the other unchanged removes only that one binding."""
        before = sync(
            empty_ledger(),
            _shared_tag_setup(("Root.X.Alpha.Field", "added"), ("Root.Y.Beta.Field", "added")),
            _meta(),
            _cfg(),
        )
        step = _report(
            paths=[
                _paths(
                    "TypeA.Field",
                    "TypeA",
                    ({}, "Root.X.Alpha.Field", "removed"),
                    ({}, "Root.Y.Beta.Field", "unchanged"),
                )
            ]
        )
        after = sync(before, step, _meta(), _cfg())
        removed = after["bindings"][after["bindings"]["status"] == ElementStatus.REMOVED]
        assert removed["runtime_path"].tolist() == ["Root.X.Alpha.Field"]
        assert _active(after)["runtime_path"].tolist() == ["Root.Y.Beta.Field"]

    def test_adding_a_third_path_mints_only_one_new_binding(self) -> None:
        """A third added path with the same empty tags mints one new binding; the other two are untouched."""
        before = sync(
            empty_ledger(),
            _shared_tag_setup(("Root.X.Alpha.Field", "added"), ("Root.Y.Beta.Field", "added")),
            _meta(),
            _cfg(),
        )
        before_uris = set(_active(before)["binding_uri"])
        step = _report(
            paths=[
                _paths(
                    "TypeA.Field",
                    "TypeA",
                    ({}, "Root.X.Alpha.Field", "unchanged"),
                    ({}, "Root.Y.Beta.Field", "unchanged"),
                    ({}, "Root.Z.Gamma.Field", "added"),
                )
            ]
        )
        after = sync(before, step, _meta(), _cfg())
        active = _active(after)
        assert len(active) == 3
        assert before_uris <= set(active["binding_uri"])
        assert set(active["runtime_path"]) == {"Root.X.Alpha.Field", "Root.Y.Beta.Field", "Root.Z.Gamma.Field"}

    def test_same_path_twice_in_one_property_fails(self) -> None:
        """Two entries reporting the same runtime path for one property raise SyncError."""
        report = _report(
            _entity_added("TypeA"),
            _prop_added("TypeA.Field", parent="TypeA"),
            paths=[
                _paths(
                    "TypeA.Field",
                    "TypeA",
                    ({}, "Root.X.Alpha.Field", "added"),
                    ({}, "Root.X.Alpha.Field", "added"),
                )
            ],
        )
        with pytest.raises(SyncError, match="duplicate runtime_path"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_same_path_across_two_properties_fails(self) -> None:
        """The same runtime path reported for two different properties raises SyncError."""
        report = _report(
            _entity_added("TypeA"),
            _prop_added("TypeA.Field", parent="TypeA"),
            _prop_added("TypeA.Other", parent="TypeA"),
            paths=[
                _paths("TypeA.Field", "TypeA", ({}, "Root.Shared", "added")),
                _paths("TypeA.Other", "TypeA", ({}, "Root.Shared", "added")),
            ],
        )
        with pytest.raises(SyncError, match="also reported for"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_ledger_validates(self) -> None:
        """The resulting ledger with shared instance_tags passes validation."""
        report = _shared_tag_setup(("Root.X.Alpha.Field", "added"), ("Root.Y.Beta.Field", "added"))
        validate_ledger(sync(empty_ledger(), report, _meta(), _cfg()))


# ── Contract references and cascade ────────────────────────────────────────────────────────


def _concept_uri(tables: dict, label: str) -> str:
    df = tables["concepts"]
    return str(df[df["current_label"] == label].iloc[0]["concept_uri"])


def _contracts_of(tables: dict, label: str) -> list[tuple[str, str]]:
    """Return ``(contract_uri, status)`` pairs for a concept, oldest first."""
    df = tables["contracts"]
    rows = df[df["concept_uri"] == _concept_uri(tables, label)]
    return [(str(r["contract_uri"]), str(r["status"])) for _, r in rows.iterrows()]


def _active_contract(tables: dict, label: str) -> str:
    return next(uri for uri, status in _contracts_of(tables, label) if status == "ACTIVE")


def _edges(tables: dict) -> list[tuple[str, str, str]]:
    df = tables["contract_references"]
    return [(str(r["contract_uri"]), str(r["role"]), str(r["target_contract_uri"])) for _, r in df.iterrows()]


def _typed_property_report(**prop_kwargs) -> DiffReport:
    """Entities Vehicle and Door plus property Vehicle.Door typed by Door."""
    return _report(
        _entity_added("Vehicle"),
        _entity_added("Door", type="branch"),
        _prop_added("Vehicle.Door", parent="Vehicle", output_type="Door", **prop_kwargs),
    )


class TestReferences:
    def test_added_records_edge_to_active_target_contract(self) -> None:
        """An ADDED property with references stores one edge to the target's active contract."""
        report = _typed_property_report(references={"output_type": "Door"})
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert _edges(tables) == [
            (_active_contract(tables, "Vehicle.Door"), "output_type", _active_contract(tables, "Door"))
        ]
        validate_ledger(tables)

    def test_no_references_no_edges(self) -> None:
        """A property without references stores no edges."""
        tables = sync(empty_ledger(), _typed_property_report(), _meta(), _cfg())
        assert _edges(tables) == []

    def test_enumeration_set_is_a_valid_target(self) -> None:
        """An ENUMERATION_SET may be referenced."""
        report = _report(
            _entity_added("Vehicle"),
            _entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET),
            _prop_added("Vehicle.Unit", parent="Vehicle", references={"unit": "SpeedUnit"}),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(_edges(tables)) == 1
        validate_ledger(tables)

    def test_unresolvable_label_raises(self) -> None:
        """A references label that names no active entity/enumeration set raises SyncError."""
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Door", parent="Vehicle", references={"output_type": "Missing"}),
        )
        with pytest.raises(SyncError, match="Missing"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_property_label_is_not_a_valid_target(self) -> None:
        """Referencing a property label raises SyncError."""
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _prop_added("Vehicle.Door", parent="Vehicle", references={"output_type": "Vehicle.Speed"}),
        )
        with pytest.raises(SyncError, match="ENTITY or ENUMERATION_SET"):
            sync(empty_ledger(), report, _meta(), _cfg())


class TestReferenceCascade:
    def _setup(self) -> dict:
        report = _typed_property_report(references={"output_type": "Door"})
        return sync(empty_ledger(), report, _meta(), _cfg())

    def test_breaking_target_change_recontracts_dependent(self) -> None:
        """A new target contract cascades a new contract, bindings, edge and audit row onto the dependent."""
        tables = self._setup()
        old_prop, old_door = _active_contract(tables, "Vehicle.Door"), _active_contract(tables, "Door")
        cfg = _cfg(entity={"type": True}, property={"output_type": True})
        tables = sync(tables, _report(_entity_modified("Door", type="struct")), _meta(), cfg)

        new_prop, new_door = _active_contract(tables, "Vehicle.Door"), _active_contract(tables, "Door")
        assert new_prop != old_prop and new_door != old_door
        assert dict((u, s) for u, s in _contracts_of(tables, "Vehicle.Door"))[old_prop] == "SUPERSEDED"
        assert (new_prop, "output_type", new_door) in _edges(tables)
        active_bindings = tables["bindings"][tables["bindings"]["status"] == "ACTIVE"]
        assert list(active_bindings["contract_uri"]) == [new_prop]

        aspects = tables["revision_aspects"]
        row = aspects[aspects["reason"] == "referenced-contract-changed"].iloc[0]
        assert row["aspect_key"] == "output_type" and row["operation"] == "modified"
        assert json.loads(row["previous_value"]) == old_door and json.loads(row["newer_value"]) == new_door
        validate_ledger(tables)

    def test_policy_false_does_not_cascade_and_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """With the role marked non-breaking the dependent keeps its contract and a warning is logged."""
        tables = self._setup()
        old_prop = _active_contract(tables, "Vehicle.Door")
        cfg = _cfg(entity={"type": True}, property={"output_type": False})
        with caplog.at_level("WARNING"):
            tables = sync(tables, _report(_entity_modified("Door", type="struct")), _meta(), cfg)
        assert _active_contract(tables, "Vehicle.Door") == old_prop
        assert "does not mark them breaking" in caplog.text
        validate_ledger(tables)

    def test_non_breaking_target_change_does_not_cascade(self) -> None:
        """A target change that keeps its contract leaves dependents untouched."""
        tables = self._setup()
        old_prop = _active_contract(tables, "Vehicle.Door")
        cfg = _cfg(entity={"type": False}, property={"output_type": True})
        tables = sync(tables, _report(_entity_modified("Door", type="struct")), _meta(), cfg)
        assert _active_contract(tables, "Vehicle.Door") == old_prop

    def test_dependent_recontracted_in_same_run_is_not_cascaded_again(self) -> None:
        """A dependent already re-minted this run points at the new target and gets no extra contract."""
        tables = self._setup()
        cfg = _cfg(entity={"type": True}, property={"output_type": True, "unit": True})
        report = _report(
            _entity_modified("Door", type="struct"),
            _prop_modified("Vehicle.Door", parent="Vehicle", unit="m"),
        )
        tables = sync(tables, report, _meta(), cfg)
        assert len(_contracts_of(tables, "Vehicle.Door")) == 2
        assert (_active_contract(tables, "Vehicle.Door"), "output_type", _active_contract(tables, "Door")) in _edges(
            tables
        )
        validate_ledger(tables)

    def test_edges_carried_forward_on_unrelated_breaking_change(self) -> None:
        """A property re-contracted for another aspect keeps its reference edge."""
        tables = self._setup()
        cfg = _cfg(property={"unit": True})
        tables = sync(tables, _report(_prop_modified("Vehicle.Door", parent="Vehicle", unit="m")), _meta(), cfg)
        assert (_active_contract(tables, "Vehicle.Door"), "output_type", _active_contract(tables, "Door")) in _edges(
            tables
        )
        validate_ledger(tables)

    def test_retarget_forces_new_contract_even_when_non_breaking(self) -> None:
        """Pointing a role at a different element re-contracts regardless of the property config."""
        report = _report(
            _entity_added("Vehicle"),
            _entity_added("Door"),
            _entity_added("Window"),
            _prop_added("Vehicle.Opening", parent="Vehicle", output_type="Door", references={"output_type": "Door"}),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        old = _active_contract(tables, "Vehicle.Opening")
        modified = _prop_modified(
            "Vehicle.Opening", parent="Vehicle", references={"output_type": "Window"}, description="x"
        )
        tables = sync(tables, _report(modified), _meta(), _cfg(property={"output_type": False, "description": False}))
        new = _active_contract(tables, "Vehicle.Opening")
        assert new != old
        assert (new, "output_type", _active_contract(tables, "Window")) in _edges(tables)
        retargeted = tables["revision_aspects"]
        row = retargeted[retargeted["reason"] == "reference-retargeted"].iloc[0]
        assert row["aspect_key"] == "output_type"
        validate_ledger(tables)

    def test_unchanged_references_keep_contract(self) -> None:
        """Re-declaring identical references on a non-breaking MODIFIED event mints no contract."""
        tables = self._setup()
        old = _active_contract(tables, "Vehicle.Door")
        modified = _prop_modified("Vehicle.Door", parent="Vehicle", references={"output_type": "Door"}, description="x")
        tables = sync(tables, _report(modified), _meta(), _cfg(property={"description": False}))
        assert _active_contract(tables, "Vehicle.Door") == old
        assert len(_edges(tables)) == 1

    def test_ledger_round_trips_through_csv(self, tmp_path) -> None:
        """The contract_references table survives write and read."""
        from modl.ledger import read_ledger, write_ledger

        tables = self._setup()
        write_ledger(tables, tmp_path)
        reread = read_ledger(tmp_path)
        assert _edges(reread) == _edges(tables)


class TestMissingReferenceGuard:
    def test_warns_when_aspect_names_a_container_without_reference(self, caplog: pytest.LogCaptureFixture) -> None:
        """An aspect value equal to an entity label without a references entry logs a warning."""
        with caplog.at_level("WARNING"):
            sync(empty_ledger(), _typed_property_report(), _meta(), _cfg())
        assert "no references.output_type entry" in caplog.text

    def test_strict_raises(self) -> None:
        """In strict mode the guard raises SyncError."""
        with pytest.raises(SyncError, match="references.output_type"):
            sync(empty_ledger(), _typed_property_report(), _meta(), _cfg(), strict=True)

    def test_declared_reference_is_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        """No warning when the role is declared."""
        report = _typed_property_report(references={"output_type": "Door"})
        with caplog.at_level("WARNING"):
            sync(empty_ledger(), report, _meta(), _cfg(), strict=True)
        assert "references." not in caplog.text
