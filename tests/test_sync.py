"""Tests for the sync engine (modl.sync)."""

from __future__ import annotations

import json

import pytest

from modl.config import BreakingChangeConfig, ModelMetadata
from modl.ir import ChangeType, ContentItem, DiffReport, EntityChanged, PropertyChanged
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


def _report(*changes) -> DiffReport:
    return DiffReport(changes=list(changes))


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
    instantiate: bool | None = None,
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
        instantiate=instantiate,
        aspects=dict(aspects),
    )


def _prop_modified(
    label: str,
    parent: str,
    renamed_from: str | None = None,
    is_leaf: bool = True,
    instantiate: bool | None = None,
    **aspects,
) -> PropertyChanged:
    return PropertyChanged(
        label=label,
        parent_label=parent,
        change_type=ChangeType.MODIFIED,
        renamed_from=renamed_from,
        is_leaf=is_leaf,
        instantiate=instantiate,
        aspects=_wrap_modified_aspects(aspects),
    )


def _prop_removed(label: str, parent: str, is_leaf: bool = True, **previous_aspects) -> PropertyChanged:
    return PropertyChanged(
        label=label,
        parent_label=parent,
        change_type=ChangeType.REMOVED,
        is_leaf=is_leaf,
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
        """Entity ADDED with instances stores them as JSON on the concept row."""
        tables = sync(empty_ledger(), _report(_entity_added("Door", instances=["Left", "Right"])), _meta(), _cfg())
        row = tables["concepts"].iloc[0]
        assert json.loads(row["instances"]) == ["Left", "Right"]

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

    def test_nested_list_instances_raises(self) -> None:
        """Nested-list instances (non-compliant adapter output) raise SyncError on ADDED."""
        bad_event = EntityChanged(
            label="Door",
            change_type=ChangeType.ADDED,
            aspects={"instances": [["Front", "Rear"], ["Left", "Right"]]},
        )
        with pytest.raises(SyncError, match="flat list of strings"):
            sync(empty_ledger(), _report(bad_event), _meta(), _cfg())

    def test_non_string_element_instances_raises(self) -> None:
        """Any non-string element in instances raises SyncError on ADDED."""
        bad_event = EntityChanged(
            label="Seat",
            change_type=ChangeType.ADDED,
            aspects={"instances": [1, 2, 3]},
        )
        with pytest.raises(SyncError, match="flat list of strings"):
            sync(empty_ledger(), _report(bad_event), _meta(), _cfg())

    def test_nested_list_instances_raises_on_modified(self) -> None:
        """Nested-list instances_added raise SyncError on MODIFIED as well."""
        setup = _report(_entity_added("Door", instances=["Front", "Rear"]))
        tables = sync(empty_ledger(), setup, _meta(), _cfg())
        bad_event = EntityChanged(
            label="Door",
            change_type=ChangeType.MODIFIED,
            aspects={"instances_added": [["Front", "Rear"], ["Left", "Right"]]},
        )
        with pytest.raises(SyncError, match="flat list of strings"):
            sync(tables, _report(bad_event), _meta(), _cfg())


# ── Property ADDED ────────────────────────────────────────────────────────────


class TestPropertyAdded:
    def test_singleton_binding_when_no_instances(self) -> None:
        """Property added to entity with no instances gets one binding with null instance_label."""
        report = _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 1
        b = tables["bindings"].iloc[0]
        assert b["instance_label"] is None or str(b["instance_label"]) == "nan"

    def test_binding_per_instance(self) -> None:
        """Property added to instanced entity gets one binding per instance."""
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 2
        instance_labels = set(tables["bindings"]["instance_label"].tolist())
        assert instance_labels == {"Left", "Right"}

    def test_property_concept_stores_parent_uri(self) -> None:
        """Property concept row carries the parent entity's concept_uri as parent_uri."""
        report = _report(_entity_added("Vehicle"), _prop_added("Vehicle.Speed", parent="Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]
        entity_row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle"].iloc[0]
        assert prop_row["parent_uri"] == entity_row["concept_uri"]

    def test_property_concept_copies_instances_from_parent(self) -> None:
        """Property concept stores a copy of its parent entity's instance list."""
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]
        assert json.loads(prop_row["instances"]) == ["Left", "Right"]

    def test_non_instantiated_property_stores_null_instances(self) -> None:
        """A property with instantiate=False never inherits the parent entity's instance list."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Singleton", parent="Container", instantiate=False),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Container.Singleton"].iloc[0]
        assert prop_row["instances"] is None or str(prop_row["instances"]) == "nan"

    def test_non_instantiated_property_gets_singleton_binding(self) -> None:
        """A property with instantiate=False gets exactly one binding, not one per parent instance."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Singleton", parent="Container", instantiate=False),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 1
        b = tables["bindings"].iloc[0]
        assert b["instance_label"] is None or str(b["instance_label"]) == "nan"

    def test_instantiate_omitted_still_inherits_instances(self) -> None:
        """Omitting instantiate (default None) preserves the original inherit-from-parent behavior."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]
        assert json.loads(prop_row["instances"]) == ["A", "B"]
        assert len(tables["bindings"]) == 2

    def test_instantiate_true_explicit_inherits_instances(self) -> None:
        """Explicit instantiate=True behaves the same as the default (inherit parent instances)."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container", instantiate=True),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]
        assert json.loads(prop_row["instances"]) == ["A", "B"]

    def test_ledger_validates_with_non_instantiated_property(self) -> None:
        """Full ledger validation passes when a non-instantiated property coexists with an instanced parent."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Singleton", parent="Container", instantiate=False),
        )
        validate_ledger(sync(empty_ledger(), report, _meta(), _cfg()))

    def test_enum_value_no_binding(self) -> None:
        """ENUM_VALUE property gets concept+revision+variant but no binding."""
        report = _report(
            _entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET),
            _prop_added("SpeedUnit.KMH", parent="SpeedUnit", kind=ElementKind.ENUM_VALUE),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 0

    def test_property_added_raises_on_unknown_parent(self) -> None:
        """Property ADDED for an unknown parent label raises SyncError."""
        report = _report(_prop_added("X.Speed", parent="X"))
        with pytest.raises(SyncError, match="No concept found"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_ledger_validates_after_add(self) -> None:
        """Full ledger validation passes after entity + property ADDED."""
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
        )
        validate_ledger(sync(empty_ledger(), report, _meta(), _cfg()))


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


# ── Entity MODIFIED (instance change, non-breaking) ───────────────────────────


class TestEntityModifiedInstanceNonBreaking:
    def test_new_binding_for_added_instance_only(self) -> None:
        """Non-breaking instance addition mints one new binding per child per new instance."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        # 2 original bindings (Left, Right) + 1 new (Center) = 3
        assert len(tables["bindings"]) == 3
        labels = set(tables["bindings"]["instance_label"].tolist())
        assert labels == {"Left", "Right", "Center"}

    def test_old_bindings_unaffected(self) -> None:
        """Non-breaking instance addition leaves existing bindings ACTIVE."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        assert (tables["bindings"]["status"] == ElementStatus.ACTIVE).all()

    def test_no_new_child_property_variant(self) -> None:
        """Non-breaking instance addition does not create new child property contracts."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]["concept_uri"]
        prop_variants = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
        assert len(prop_variants) == 1  # only the initial contract

    def test_removed_instance_binding_marked_removed(self) -> None:
        """Non-breaking instance removal marks the corresponding child binding as REMOVED."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_removed=["Right"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        removed = tables["bindings"][tables["bindings"]["status"] == ElementStatus.REMOVED]
        assert len(removed) == 1
        assert removed.iloc[0]["instance_label"] == "Right"

    def test_entity_instances_column_updated(self) -> None:
        """Entity concept row reflects updated instance list after non-breaking change."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        entity_row = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]
        assert json.loads(entity_row["instances"]) == ["Left", "Right", "Center"]

    def test_non_instantiated_child_property_unaffected_by_cascade(self) -> None:
        """A non-instantiated child property keeps its singleton binding and null instances
        when the parent entity's instance list changes."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.Singleton", parent="Door", instantiate=False),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Door.Singleton"].iloc[0]
        assert prop_row["instances"] is None or str(prop_row["instances"]) == "nan"
        active = tables["bindings"][tables["bindings"]["status"] == ElementStatus.ACTIVE]
        assert len(active) == 1
        assert active.iloc[0]["instance_label"] is None or str(active.iloc[0]["instance_label"]) == "nan"


# ── Entity MODIFIED (instance change, breaking) ───────────────────────────────


class TestEntityModifiedInstanceBreaking:
    def test_removed_instance_binding_marked_removed(self) -> None:
        """Breaking instance removal marks the corresponding child binding as REMOVED."""
        cfg = _cfg(entity={"instances": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_removed=["Right"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        removed = tables["bindings"][tables["bindings"]["status"] == ElementStatus.REMOVED]
        assert len(removed) == 1
        assert removed.iloc[0]["instance_label"] == "Right"

    def test_new_binding_for_added_instance(self) -> None:
        """Breaking instance addition mints a new binding for the added instance."""
        cfg = _cfg(entity={"instances": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        active_bindings = tables["bindings"][tables["bindings"]["status"] == ElementStatus.ACTIVE]
        assert len(active_bindings) == 3  # Left, Right, Center

    def test_child_property_contract_never_changes(self) -> None:
        """Breaking instance change does NOT create new contracts for child properties."""
        cfg = _cfg(entity={"instances": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]["concept_uri"]
        prop_contracts = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
        assert len(prop_contracts) == 1
        assert prop_contracts.iloc[0]["status"] == ElementStatus.ACTIVE

    def test_new_binding_anchored_to_existing_contract(self) -> None:
        """Active bindings after breaking instance change still point to the original contract."""
        cfg = _cfg(entity={"instances": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]["concept_uri"]
        contract_uri = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri].iloc[0]["contract_uri"]
        active_bindings = tables["bindings"][tables["bindings"]["status"] == ElementStatus.ACTIVE]
        assert set(active_bindings["contract_uri"].tolist()) == {contract_uri}

    def test_ledger_validates_after_breaking_instance_change(self) -> None:
        """Full ledger validation passes after breaking instance change."""
        cfg = _cfg(entity={"instances": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        validate_ledger(sync(empty_ledger(), report, _meta(), cfg))


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
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _entity_removed("Vehicle"),
            _prop_removed("Vehicle.Speed", parent="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())  # must not raise
        assert len(tables["concepts"]) == 2

    def test_no_bindings_affected_directly(self) -> None:
        """Entity REMOVED itself does not touch bindings (property REMOVED handles that)."""
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _entity_removed("Vehicle"),
            _prop_removed("Vehicle.Speed", parent="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert (tables["bindings"]["status"] == ElementStatus.REMOVED).all()


# ── Property MODIFIED (breaking) ──────────────────────────────────────────────


class TestPropertyModifiedBreaking:
    def test_new_variant_supersedes_old(self) -> None:
        """Breaking property MODIFIED supersedes old variant and mints a new one."""
        cfg = _cfg(property={"output_type": True})
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Int"),
            _prop_modified("Vehicle.Speed", parent="Vehicle", output_type="Float"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]["concept_uri"]
        prop_variants = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
        assert len(prop_variants) == 2
        assert (prop_variants["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (prop_variants["status"] == ElementStatus.ACTIVE).sum() == 1

    def test_old_binding_superseded_new_binding_active(self) -> None:
        """Old binding is superseded; a new binding is minted anchored to the new variant."""
        cfg = _cfg(property={"output_type": True})
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Int"),
            _prop_modified("Vehicle.Speed", parent="Vehicle", output_type="Float"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        assert (tables["bindings"]["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (tables["bindings"]["status"] == ElementStatus.ACTIVE).sum() == 1

    def test_instanced_breaking_all_bindings_replaced(self) -> None:
        """Breaking property MODIFIED on instanced property replaces all bindings."""
        cfg = _cfg(property={"output_type": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door", output_type="Boolean"),
            _prop_modified("Door.IsOpen", parent="Door", output_type="Int"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        superseded = tables["bindings"][tables["bindings"]["status"] == ElementStatus.SUPERSEDED]
        active = tables["bindings"][tables["bindings"]["status"] == ElementStatus.ACTIVE]
        assert len(superseded) == 2
        assert len(active) == 2

    def test_rename_with_breaking_change(self) -> None:
        """Rename on a breaking MODIFIED event updates label and creates new variant."""
        cfg = _cfg(property={"output_type": True})
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Velocity", parent="Vehicle", output_type="Int"),
            _prop_modified("Vehicle.Speed", parent="Vehicle", renamed_from="Vehicle.Velocity", output_type="Float"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]
        assert row["current_label"] == "Vehicle.Speed"
        prev = json.loads(row["previous_labels"])
        assert "Vehicle.Velocity" in prev


class TestPropertyModifiedInstantiateTransition:
    """A change in effective instantiation is always breaking, independent of config."""

    def test_false_to_true_forces_new_contract(self) -> None:
        """instantiate flipping False->True mints a new contract even with an empty config."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container", instantiate=False),
            _prop_modified("Container.Member", parent="Container", instantiate=True),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]["concept_uri"]
        contracts = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
        assert len(contracts) == 2
        assert (contracts["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (contracts["status"] == ElementStatus.ACTIVE).sum() == 1

    def test_false_to_true_updates_instances_column(self) -> None:
        """instantiate flipping False->True re-populates the concept's instances from the parent."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container", instantiate=False),
            _prop_modified("Container.Member", parent="Container", instantiate=True),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]
        assert json.loads(prop_row["instances"]) == ["A", "B"]

    def test_false_to_true_replaces_singleton_with_per_instance_bindings(self) -> None:
        """instantiate flipping False->True supersedes the singleton binding and mints one per instance."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container", instantiate=False),
            _prop_modified("Container.Member", parent="Container", instantiate=True),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        active = tables["bindings"][tables["bindings"]["status"] == ElementStatus.ACTIVE]
        superseded = tables["bindings"][tables["bindings"]["status"] == ElementStatus.SUPERSEDED]
        assert len(superseded) == 1
        assert set(active["instance_label"].tolist()) == {"A", "B"}

    def test_true_to_false_forces_new_contract(self) -> None:
        """instantiate flipping True->False mints a new contract even with an empty config."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container"),
            _prop_modified("Container.Member", parent="Container", instantiate=False),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]["concept_uri"]
        contracts = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
        assert len(contracts) == 2

    def test_true_to_false_clears_instances_column(self) -> None:
        """instantiate flipping True->False clears the concept's instances column."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container"),
            _prop_modified("Container.Member", parent="Container", instantiate=False),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]
        assert prop_row["instances"] is None or str(prop_row["instances"]) == "nan"

    def test_true_to_false_replaces_per_instance_bindings_with_singleton(self) -> None:
        """instantiate flipping True->False supersedes per-instance bindings and mints a singleton."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container"),
            _prop_modified("Container.Member", parent="Container", instantiate=False),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        active = tables["bindings"][tables["bindings"]["status"] == ElementStatus.ACTIVE]
        superseded = tables["bindings"][tables["bindings"]["status"] == ElementStatus.SUPERSEDED]
        assert len(superseded) == 2
        assert len(active) == 1
        assert active.iloc[0]["instance_label"] is None or str(active.iloc[0]["instance_label"]) == "nan"

    def test_unchanged_instantiate_is_not_breaking(self) -> None:
        """No instantiate change and no other breaking aspect leaves the contract unchanged."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container", instantiate=False),
            _prop_modified("Container.Member", parent="Container", instantiate=False),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Container.Member"].iloc[0]["concept_uri"]
        contracts = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
        assert len(contracts) == 1

    def test_ledger_validates_after_instantiate_transition(self) -> None:
        """Full ledger validation passes after an instantiate transition either direction."""
        report = _report(
            _entity_added("Container", instances=["A", "B"]),
            _prop_added("Container.Member", parent="Container", instantiate=False),
            _prop_modified("Container.Member", parent="Container", instantiate=True),
        )
        validate_ledger(sync(empty_ledger(), report, _meta(), _cfg()))


# ── Property MODIFIED (non-breaking) ─────────────────────────────────────────


class TestPropertyModifiedNonBreaking:
    def test_no_new_variant(self) -> None:
        """Non-breaking property MODIFIED leaves the variant unchanged."""
        cfg = _cfg(property={"description": False})
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _prop_modified("Vehicle.Speed", parent="Vehicle", description="better docs"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 1

    def test_no_new_bindings(self) -> None:
        """Non-breaking property MODIFIED does not touch bindings."""
        cfg = _cfg(property={"description": False})
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _prop_modified("Vehicle.Speed", parent="Vehicle", description="better docs"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        assert len(tables["bindings"]) == 1
        assert tables["bindings"].iloc[0]["status"] == ElementStatus.ACTIVE


# ── Property REMOVED ──────────────────────────────────────────────────────────


class TestPropertyRemoved:
    def test_concept_status_removed(self) -> None:
        """Property REMOVED sets concept status to REMOVED."""
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _prop_removed("Vehicle.Speed", parent="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]
        assert prop_row["status"] == ElementStatus.REMOVED

    def test_revision_status_removed(self) -> None:
        """Property REMOVED mints a final REMOVED revision."""
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _prop_removed("Vehicle.Speed", parent="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        prop_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Speed"].iloc[0]["concept_uri"]
        prop_revs = tables["revisions"][tables["revisions"]["concept_uri"] == prop_uri]
        assert (prop_revs["status"] == ElementStatus.REMOVED).sum() == 1

    def test_binding_status_removed(self) -> None:
        """All bindings for a REMOVED property are marked REMOVED."""
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            _prop_removed("Vehicle.Speed", parent="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert (tables["bindings"]["status"] == ElementStatus.REMOVED).all()

    def test_enum_value_removed_no_binding_touched(self) -> None:
        """ENUM_VALUE REMOVED does not attempt to supersede any bindings."""
        report = _report(
            _entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET),
            _prop_added("SpeedUnit.KMH", parent="SpeedUnit", kind=ElementKind.ENUM_VALUE),
            _prop_removed("SpeedUnit.KMH", parent="SpeedUnit"),
        )
        tables = sync(empty_ledger(), report, _meta(), _cfg())
        assert len(tables["bindings"]) == 0


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
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door", output_type="Boolean"),
            _prop_added("Door.IsLocked", parent="Door", output_type="Boolean"),
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
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Int"),
            _prop_modified("Vehicle.Speed", parent="Vehicle", output_type="Float"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
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
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
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
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle", output_type="Float"),
            _prop_removed("Vehicle.Speed", parent="Vehicle", output_type="Float"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
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
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Vel", parent="Vehicle"),
            _prop_modified("Vehicle.Velocity", parent="Vehicle", renamed_from="Vehicle.Vel", description="updated"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        row = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle.Velocity"].iloc[0]
        assert "Vehicle.Vel" in json.loads(row["previous_labels"])
        prop_uri = row["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]) == 1


# ── Instance list shrinks (non-breaking) ──────────────────────────────────────


class TestEntityModifiedInstanceShrinks:
    def test_shrink_mints_no_new_bindings(self) -> None:
        """Non-breaking instance removal does not create new bindings."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right", "Rear"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_removed=["Rear"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        # 3 original bindings; Rear is now REMOVED; no new ones minted
        assert len(tables["bindings"]) == 3

    def test_shrink_updates_entity_instances_column(self) -> None:
        """Entity concept row reflects the reduced instance list after a shrink."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right", "Rear"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_removed=["Rear"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        row = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]
        assert json.loads(row["instances"]) == ["Left", "Right"]

    def test_shrink_removed_binding_marked_removed(self) -> None:
        """Child binding for the removed instance is marked REMOVED after a shrink."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right", "Rear"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_removed=["Rear"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        removed_bindings = tables["bindings"][tables["bindings"]["status"] == ElementStatus.REMOVED]
        assert len(removed_bindings) == 1
        assert removed_bindings.iloc[0]["instance_label"] == "Rear"


# ── Multiple child properties — cascade coverage ──────────────────────────────


class TestMultipleChildPropertiesCascade:
    def test_breaking_instance_cascades_new_binding_to_all_children(self) -> None:
        """Breaking instance addition appends new bindings for every child property."""
        cfg = _cfg(entity={"instances": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _prop_added("Door.IsLocked", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        # 2 properties × (2 original + 1 new) = 6 total bindings; all ACTIVE
        assert len(tables["bindings"]) == 6
        assert (tables["bindings"]["status"] == ElementStatus.ACTIVE).all()

    def test_breaking_instance_contracts_unchanged_for_all_children(self) -> None:
        """Breaking instance change never creates new contracts for any child property."""
        cfg = _cfg(entity={"instances": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _prop_added("Door.IsLocked", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        for label in ("Door.IsOpen", "Door.IsLocked"):
            prop_uri = tables["concepts"][tables["concepts"]["current_label"] == label].iloc[0]["concept_uri"]
            prop_contracts = tables["contracts"][tables["contracts"]["concept_uri"] == prop_uri]
            assert len(prop_contracts) == 1, f"Expected 1 contract for {label}, got {len(prop_contracts)}"
            assert prop_contracts.iloc[0]["status"] == ElementStatus.ACTIVE

    def test_nonbreaking_instance_cascades_new_bindings_to_all_children(self) -> None:
        """Non-breaking instance addition appends new bindings for every child property."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _prop_added("Door.IsLocked", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        # 2 properties × (2 original + 1 new) = 6 total bindings, all ACTIVE
        assert len(tables["bindings"]) == 6
        assert (tables["bindings"]["status"] == ElementStatus.ACTIVE).all()


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


# Step 1: simultaneous instances_added + instances_removed classification
class TestSimultaneousInstanceAddAndRemove:
    def test_both_fire_breaking_when_added_is_breaking(self) -> None:
        """When instances.added: true and instances.removed: false, a simultaneous add+remove is breaking."""
        cfg = _cfg(entity={"instances.added": True, "instances.removed": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            EntityChanged(
                label="Door",
                change_type=ChangeType.MODIFIED,
                aspects={"instances_added": ["Center"], "instances_removed": ["Right"]},
            ),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        entity_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]["concept_uri"]
        entity_contracts = tables["contracts"][tables["contracts"]["concept_uri"] == entity_uri]
        # Breaking → new entity contract
        assert len(entity_contracts) == 2

    def test_both_fire_breaking_when_removed_is_breaking(self) -> None:
        """When instances.removed: true and instances.added: false, a simultaneous add+remove is breaking."""
        cfg = _cfg(entity={"instances.added": False, "instances.removed": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            EntityChanged(
                label="Door",
                change_type=ChangeType.MODIFIED,
                aspects={"instances_added": ["Center"], "instances_removed": ["Right"]},
            ),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        entity_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]["concept_uri"]
        entity_contracts = tables["contracts"][tables["contracts"]["concept_uri"] == entity_uri]
        assert len(entity_contracts) == 2

    def test_both_non_breaking_when_both_declared_false(self) -> None:
        """When both instances.added: false and instances.removed: false, the event is non-breaking."""
        cfg = _cfg(entity={"instances.added": False, "instances.removed": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            EntityChanged(
                label="Door",
                change_type=ChangeType.MODIFIED,
                aspects={"instances_added": ["Center"], "instances_removed": ["Right"]},
            ),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        entity_uri = tables["concepts"][tables["concepts"]["current_label"] == "Door"].iloc[0]["concept_uri"]
        entity_contracts = tables["contracts"][tables["contracts"]["concept_uri"] == entity_uri]
        assert len(entity_contracts) == 1  # non-breaking — no new contract


# Step 2: properties.added / properties.removed wired from content
class TestContentDerivedBreaking:
    def test_properties_removed_breaking_mints_new_entity_contract(self) -> None:
        """properties.removed: true in config + REMOVED content item triggers new entity contract."""
        cfg = _cfg(entity={"properties.removed": True})
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            EntityChanged(
                label="Vehicle",
                change_type=ChangeType.MODIFIED,
                content=[ContentItem(label="Vehicle.Speed", change_type=ChangeType.REMOVED)],
            ),
            _prop_removed("Vehicle.Speed", parent="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        entity_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle"].iloc[0]["concept_uri"]
        entity_contracts = tables["contracts"][tables["contracts"]["concept_uri"] == entity_uri]
        assert len(entity_contracts) == 2
        assert (entity_contracts["status"] == ElementStatus.SUPERSEDED).sum() == 1
        assert (entity_contracts["status"] == ElementStatus.ACTIVE).sum() == 1

    def test_properties_removed_false_no_new_entity_contract(self) -> None:
        """properties.removed: false + REMOVED content item does not trigger new entity contract."""
        cfg = _cfg(entity={"properties.removed": False})
        report = _report(
            _entity_added("Vehicle"),
            _prop_added("Vehicle.Speed", parent="Vehicle"),
            EntityChanged(
                label="Vehicle",
                change_type=ChangeType.MODIFIED,
                content=[ContentItem(label="Vehicle.Speed", change_type=ChangeType.REMOVED)],
            ),
            _prop_removed("Vehicle.Speed", parent="Vehicle"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        entity_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == entity_uri]) == 1

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
        entity_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == entity_uri]) == 2

    def test_empty_content_no_new_entity_contract(self) -> None:
        """Entity MODIFIED with no content items does not trigger content-derived breaking."""
        cfg = _cfg(entity={"properties.removed": True})
        report = _report(_entity_added("Vehicle"), _entity_modified("Vehicle"))
        tables = sync(empty_ledger(), report, _meta(), cfg)
        entity_uri = tables["concepts"][tables["concepts"]["current_label"] == "Vehicle"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == entity_uri]) == 1

    def test_values_removed_breaking_on_enumeration_set(self) -> None:
        """values.removed: true + REMOVED content item on ENUMERATION_SET triggers new contract."""
        cfg = _cfg(enumeration_set={"values.removed": True})
        report = _report(
            _entity_added("SpeedUnit", kind=ElementKind.ENUMERATION_SET),
            _prop_added("SpeedUnit.KMH", parent="SpeedUnit", kind=ElementKind.ENUM_VALUE),
            EntityChanged(
                label="SpeedUnit",
                kind=ElementKind.ENUMERATION_SET,
                change_type=ChangeType.MODIFIED,
                content=[ContentItem(label="SpeedUnit.KMH", change_type=ChangeType.REMOVED)],
            ),
            _prop_removed("SpeedUnit.KMH", parent="SpeedUnit"),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        set_uri = tables["concepts"][tables["concepts"]["current_label"] == "SpeedUnit"].iloc[0]["concept_uri"]
        assert len(tables["contracts"][tables["contracts"]["concept_uri"] == set_uri]) == 2


# Step 4: child concept instances kept in sync during cascade
class TestChildConceptInstancesSync:
    def test_child_concept_instances_updated_on_nonbreaking_add(self) -> None:
        """After non-breaking instance addition, child property concept instances reflect new parent list."""
        cfg = _cfg(entity={"instances": False})
        report = _report(
            _entity_added("Door", instances=["Left", "Right"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_added=["Center"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]
        assert json.loads(prop_row["instances"]) == ["Left", "Right", "Center"]

    def test_child_concept_instances_updated_on_breaking_remove(self) -> None:
        """After breaking instance removal, child property concept instances reflect new parent list."""
        cfg = _cfg(entity={"instances": True})
        report = _report(
            _entity_added("Door", instances=["Left", "Right", "Rear"]),
            _prop_added("Door.IsOpen", parent="Door"),
            _entity_modified("Door", instances_removed=["Rear"]),
        )
        tables = sync(empty_ledger(), report, _meta(), cfg)
        prop_row = tables["concepts"][tables["concepts"]["current_label"] == "Door.IsOpen"].iloc[0]
        assert json.loads(prop_row["instances"]) == ["Left", "Right"]


# Step 5: duplicate and overlap validation
class TestInstanceDuplicateValidation:
    def test_duplicate_in_added_snapshot_raises(self) -> None:
        """Duplicate values in the instances list of an ADDED event raise SyncError."""
        report = _report(_entity_added("Door", instances=["Left", "Left"]))
        with pytest.raises(SyncError, match="duplicate"):
            sync(empty_ledger(), report, _meta(), _cfg())

    def test_duplicate_in_instances_added_raises(self) -> None:
        """Duplicate values in instances_added on a MODIFIED event raise SyncError."""
        setup = _report(_entity_added("Door", instances=["Right"]))
        tables = sync(empty_ledger(), setup, _meta(), _cfg())
        bad = EntityChanged(
            label="Door",
            change_type=ChangeType.MODIFIED,
            aspects={"instances_added": ["Left", "Left"]},
        )
        with pytest.raises(SyncError, match="duplicate"):
            sync(tables, _report(bad), _meta(), _cfg())

    def test_overlap_with_existing_instances_raises(self) -> None:
        """instances_added containing a value already in the stored list raises SyncError."""
        setup = _report(_entity_added("Door", instances=["Left", "Right"]))
        tables = sync(empty_ledger(), setup, _meta(), _cfg())
        bad = EntityChanged(
            label="Door",
            change_type=ChangeType.MODIFIED,
            aspects={"instances_added": ["Left"]},
        )
        with pytest.raises(SyncError, match="already present"):
            sync(tables, _report(bad), _meta(), _cfg())
