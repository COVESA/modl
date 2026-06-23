import json

import pytest
from pydantic import ValidationError

from modl.config import BreakingChangeConfig
from modl.ir import (
    ChangeType,
    ContentItem,
    DiffReport,
    DiffReportValidationError,
    EntityChanged,
    PropertyChanged,
    _aspect_ops_for_event,
    extract_aspect_ops,
    extract_op,
    validate_report_aspects,
)
from modl.models import ElementKind

# ── Fixtures ──────────────────────────────────────────────────────────────────

NS = "http://example.org/myns/"


def _config(**kwargs) -> BreakingChangeConfig:
    """Return a minimal config, optionally overriding any of the four section dicts."""
    return BreakingChangeConfig.model_validate(
        {
            "entity": kwargs.get("entity", {}),
            "property": kwargs.get("property_", {}),
            "enumeration_set": kwargs.get("enumeration_set", {}),
            "enum_value": kwargs.get("enum_value", {}),
        }
    )


VALID_REPORT_DICT = {
    "changes": [
        {
            "label": "Vehicle",
            "kind": "ENTITY",
            "change_type": "ADDED",
            "aspects": {"type": "branch"},
        },
        {
            "label": "Vehicle.Speed",
            "parent_label": "Vehicle",
            "kind": "PROPERTY",
            "change_type": "ADDED",
            "aspects": {"output_type": "Float", "unit": "km/h"},
        },
    ]
}


# ── ChangeType ────────────────────────────────────────────────────────────────


class TestChangeType:
    def test_values(self) -> None:
        """All three change types are string-compatible."""
        assert ChangeType.ADDED == "ADDED"
        assert ChangeType.REMOVED == "REMOVED"
        assert ChangeType.MODIFIED == "MODIFIED"


# ── ContentItem ───────────────────────────────────────────────────────────────


class TestContentItem:
    def test_valid(self) -> None:
        """label and change_type are stored correctly."""
        item = ContentItem(label="Vehicle.Door.IsOpen", change_type=ChangeType.REMOVED)
        assert item.label == "Vehicle.Door.IsOpen"
        assert item.change_type == ChangeType.REMOVED

    def test_invalid_change_type_rejected(self) -> None:
        """Unrecognised change_type fails validation."""
        with pytest.raises(ValidationError):
            ContentItem(label="X", change_type="RENAMED")  # ty: ignore[invalid-argument-type]


# ── EntityChanged ─────────────────────────────────────────────────────────────


class TestEntityChanged:
    def test_added_full_snapshot(self) -> None:
        """ADDED entity stores full aspects snapshot; defaults are correct."""
        event = EntityChanged(
            label="Vehicle",
            change_type=ChangeType.ADDED,
            aspects={"type": "branch"},
        )
        assert event.kind == ElementKind.ENTITY
        assert event.aspects == {"type": "branch"}
        assert event.content == []
        assert event.renamed_from is None

    def test_added_no_content_allowed(self) -> None:
        """ADDED entity must not carry content."""
        with pytest.raises(ValidationError, match="ADDED events must not carry content"):
            EntityChanged(
                label="Vehicle",
                change_type=ChangeType.ADDED,
                content=[ContentItem(label="Vehicle.Speed", change_type=ChangeType.ADDED)],
            )

    def test_modified_delta_aspects(self) -> None:
        """MODIFIED entity carries only changed keys in aspects; directional instance keys accepted."""
        event = EntityChanged(
            label="Vehicle.Door",
            change_type=ChangeType.MODIFIED,
            aspects={"instances_added": ["Center"], "instances_removed": ["Front"]},
            content=[ContentItem(label="Vehicle.Door.IsLocked", change_type=ChangeType.ADDED)],
        )
        assert event.aspects == {"instances_added": ["Center"], "instances_removed": ["Front"]}
        assert len(event.content) == 1
        assert event.content[0].label == "Vehicle.Door.IsLocked"

    def test_modified_with_rename(self) -> None:
        """MODIFIED entity with rename sets renamed_from."""
        event = EntityChanged(
            label="Vehicle.Window",
            change_type=ChangeType.MODIFIED,
            renamed_from="Vehicle.Glass",
        )
        assert event.renamed_from == "Vehicle.Glass"

    def test_instances_on_modified_rejected(self) -> None:
        """Plain 'instances' key on a MODIFIED entity event is forbidden; use instances_added/removed."""
        with pytest.raises(ValidationError, match="instances_added"):
            EntityChanged(
                label="Vehicle.Door",
                change_type=ChangeType.MODIFIED,
                aspects={"instances": ["Left", "Right", "Center"]},
            )

    def test_name_key_in_aspects_rejected_entity(self) -> None:
        """'name' key in aspects is forbidden on EntityChanged; use renamed_from."""
        with pytest.raises(ValidationError, match="renamed_from"):
            EntityChanged(
                label="Vehicle",
                change_type=ChangeType.MODIFIED,
                aspects={"name": "NewVehicle"},
            )
        """REMOVED entity carries no aspects or content."""
        event = EntityChanged(label="Vehicle.OldFeature", change_type=ChangeType.REMOVED)
        assert event.aspects == {}
        assert event.content == []

    def test_removed_with_aspects_rejected(self) -> None:
        """REMOVED entity carrying aspects fails validation."""
        with pytest.raises(ValidationError, match="REMOVED events must not carry aspects or content"):
            EntityChanged(
                label="Vehicle",
                change_type=ChangeType.REMOVED,
                aspects={"type": "branch"},
            )

    def test_removed_with_content_rejected(self) -> None:
        """REMOVED entity carrying content fails validation."""
        with pytest.raises(ValidationError, match="REMOVED events must not carry aspects or content"):
            EntityChanged(
                label="Vehicle",
                change_type=ChangeType.REMOVED,
                content=[ContentItem(label="Vehicle.Speed", change_type=ChangeType.REMOVED)],
            )

    def test_renamed_from_on_added_rejected(self) -> None:
        """renamed_from on ADDED event fails validation."""
        with pytest.raises(ValidationError, match="renamed_from is only valid on MODIFIED events"):
            EntityChanged(label="Vehicle", change_type=ChangeType.ADDED, renamed_from="OldVehicle")

    def test_renamed_from_on_removed_rejected(self) -> None:
        """renamed_from on REMOVED event fails validation."""
        with pytest.raises(ValidationError, match="renamed_from is only valid on MODIFIED events"):
            EntityChanged(label="Vehicle", change_type=ChangeType.REMOVED, renamed_from="OldVehicle")

    def test_invalid_change_type_rejected(self) -> None:
        """Unrecognised change_type fails validation."""
        with pytest.raises(ValidationError):
            EntityChanged(label="Vehicle", change_type="RENAMED")  # ty: ignore[invalid-argument-type]


# ── PropertyChanged ───────────────────────────────────────────────────────────


class TestPropertyChanged:
    def test_added_full_snapshot(self) -> None:
        """ADDED property carries full aspects including output_type."""
        event = PropertyChanged(
            label="Vehicle.Speed",
            parent_label="Vehicle",
            change_type=ChangeType.ADDED,
            aspects={"output_type": "Float", "unit": "km/h", "is_list": False, "is_required": False},
        )
        assert event.kind == ElementKind.PROPERTY
        assert event.parent_label == "Vehicle"
        assert event.aspects["output_type"] == "Float"
        assert event.aspects["unit"] == "km/h"
        assert event.renamed_from is None

    def test_modified_delta(self) -> None:
        """MODIFIED property carries only changed aspect keys."""
        event = PropertyChanged(
            label="Vehicle.Speed",
            parent_label="Vehicle",
            change_type=ChangeType.MODIFIED,
            aspects={"output_type": "Float"},
        )
        assert event.aspects == {"output_type": "Float"}

    def test_modified_with_rename(self) -> None:
        """MODIFIED property with rename sets renamed_from."""
        event = PropertyChanged(
            label="Vehicle.Velocity",
            parent_label="Vehicle",
            change_type=ChangeType.MODIFIED,
            renamed_from="Vehicle.Speed",
        )
        assert event.renamed_from == "Vehicle.Speed"

    def test_removed_empty_aspects(self) -> None:
        """REMOVED property carries no aspects."""
        event = PropertyChanged(
            label="Vehicle.OldField",
            parent_label="Vehicle",
            change_type=ChangeType.REMOVED,
        )
        assert event.aspects == {}

    def test_removed_with_aspects_rejected(self) -> None:
        """REMOVED property carrying aspects fails validation."""
        with pytest.raises(ValidationError, match="REMOVED events must not carry aspects"):
            PropertyChanged(
                label="Vehicle.Speed",
                parent_label="Vehicle",
                change_type=ChangeType.REMOVED,
                aspects={"output_type": "Float"},
            )

    def test_renamed_from_on_added_rejected(self) -> None:
        """renamed_from on ADDED property fails validation."""
        with pytest.raises(ValidationError, match="renamed_from is only valid on MODIFIED events"):
            PropertyChanged(
                label="Vehicle.Speed",
                parent_label="Vehicle",
                change_type=ChangeType.ADDED,
                renamed_from="OldLabel",
            )

    def test_missing_parent_label_rejected(self) -> None:
        """parent_label is required; omitting it fails validation."""
        with pytest.raises(ValidationError):
            PropertyChanged(label="Vehicle.Speed", change_type=ChangeType.ADDED)  # ty: ignore[missing-argument]

    def test_custom_aspect_keys_stored(self) -> None:
        """Adapter-defined aspect keys (non-canonical) are stored verbatim."""
        event = PropertyChanged(
            label="Vehicle.Temperature",
            parent_label="Vehicle",
            change_type=ChangeType.ADDED,
            aspects={"output_type": "Float", "unit": "DEG_C", "min": -40, "max": 125, "accuracy": 0.5},
        )
        assert event.aspects["unit"] == "DEG_C"
        assert event.aspects["min"] == -40
        assert event.aspects["accuracy"] == 0.5

    def test_name_key_in_aspects_rejected_property(self) -> None:
        """'name' key in aspects is forbidden on PropertyChanged; use renamed_from."""
        with pytest.raises(ValidationError, match="renamed_from"):
            PropertyChanged(
                label="Vehicle.Speed",
                parent_label="Vehicle",
                change_type=ChangeType.MODIFIED,
                aspects={"name": "NewSpeed"},
            )


# ── DiffReport ────────────────────────────────────────────────────────────────


class TestDiffReport:
    def test_parse_valid_json(self) -> None:
        """Two-item report parses into the correct concrete event types."""
        report = DiffReport.from_json(json.dumps(VALID_REPORT_DICT))
        assert len(report.changes) == 2
        assert isinstance(report.changes[0], EntityChanged)
        assert isinstance(report.changes[1], PropertyChanged)

    def test_entity_event_parsed(self) -> None:
        """Entity event preserves label and change_type."""
        report = DiffReport.from_json(json.dumps(VALID_REPORT_DICT))
        entity = report.changes[0]
        assert isinstance(entity, EntityChanged)
        assert entity.label == "Vehicle"
        assert entity.change_type == ChangeType.ADDED
        assert entity.aspects == {"type": "branch"}

    def test_property_event_parsed(self) -> None:
        """Property event preserves label, parent_label, and aspects."""
        report = DiffReport.from_json(json.dumps(VALID_REPORT_DICT))
        prop = report.changes[1]
        assert isinstance(prop, PropertyChanged)
        assert prop.label == "Vehicle.Speed"
        assert prop.parent_label == "Vehicle"
        assert prop.aspects == {"output_type": "Float", "unit": "km/h"}

    def test_empty_changes(self) -> None:
        """Empty changes list is a valid report."""
        report = DiffReport.from_json(json.dumps({"changes": []}))
        assert report.changes == []

    def test_invalid_json_rejected(self) -> None:
        """Non-JSON string raises ValidationError."""
        with pytest.raises(ValidationError):
            DiffReport.from_json("not valid json")

    def test_missing_changes_key_rejected(self) -> None:
        """Wrong top-level key ('events' instead of 'changes') raises ValidationError."""
        with pytest.raises(ValidationError):
            DiffReport.from_json(json.dumps({"events": []}))

    def test_modified_with_content(self) -> None:
        """MODIFIED entity with content items round-trips correctly."""
        payload = {
            "changes": [
                {
                    "label": "Vehicle.Door",
                    "kind": "ENTITY",
                    "change_type": "MODIFIED",
                    "content": [{"label": "Vehicle.Door.IsLocked", "change_type": "ADDED"}],
                },
                {
                    "label": "Vehicle.Door.IsLocked",
                    "parent_label": "Vehicle.Door",
                    "kind": "PROPERTY",
                    "change_type": "ADDED",
                    "aspects": {"output_type": "Boolean"},
                },
            ]
        }
        report = DiffReport.from_json(json.dumps(payload))
        entity = report.changes[0]
        assert isinstance(entity, EntityChanged)
        assert entity.content[0].label == "Vehicle.Door.IsLocked"
        assert entity.content[0].change_type == ChangeType.ADDED

    def test_renamed_entity_round_trips(self) -> None:
        """MODIFIED entity with renamed_from round-trips correctly."""
        payload = {
            "changes": [
                {
                    "label": "Vehicle.Window",
                    "kind": "ENTITY",
                    "change_type": "MODIFIED",
                    "renamed_from": "Vehicle.Glass",
                }
            ]
        }
        report = DiffReport.from_json(json.dumps(payload))
        entity = report.changes[0]
        assert isinstance(entity, EntityChanged)
        assert entity.renamed_from == "Vehicle.Glass"

    def test_renamed_property_round_trips(self) -> None:
        """MODIFIED property with renamed_from round-trips correctly."""
        payload = {
            "changes": [
                {
                    "label": "Vehicle.Velocity",
                    "parent_label": "Vehicle",
                    "kind": "PROPERTY",
                    "change_type": "MODIFIED",
                    "renamed_from": "Vehicle.Speed",
                    "aspects": {},
                }
            ]
        }
        report = DiffReport.from_json(json.dumps(payload))
        prop = report.changes[0]
        assert isinstance(prop, PropertyChanged)
        assert prop.renamed_from == "Vehicle.Speed"


# ── DiffReport.validate_structure ─────────────────────────────────────────────


class TestValidateStructure:
    def test_clean_report_no_warnings(self) -> None:
        """Report with unique labels produces no warnings."""
        report = DiffReport.from_json(json.dumps(VALID_REPORT_DICT))
        assert report.validate_structure() == []

    def test_duplicate_entity_label_warns(self) -> None:
        """Two events for the same entity label produce a warning."""
        payload = {
            "changes": [
                {"label": "Vehicle.Door", "kind": "ENTITY", "change_type": "REMOVED"},
                {"label": "Vehicle.Door", "kind": "ENTITY", "change_type": "REMOVED"},
            ]
        }
        report = DiffReport.from_json(json.dumps(payload))
        warnings = report.validate_structure()
        assert len(warnings) == 1
        assert "Vehicle.Door" in warnings[0]

    def test_duplicate_property_label_warns(self) -> None:
        """Two events for the same (label, parent_label) property produce a warning."""
        payload = {
            "changes": [
                {
                    "label": "Vehicle.Speed",
                    "parent_label": "Vehicle",
                    "kind": "PROPERTY",
                    "change_type": "REMOVED",
                },
                {
                    "label": "Vehicle.Speed",
                    "parent_label": "Vehicle",
                    "kind": "PROPERTY",
                    "change_type": "ADDED",
                    "aspects": {"output_type": "Float"},
                },
            ]
        }
        report = DiffReport.from_json(json.dumps(payload))
        warnings = report.validate_structure()
        assert len(warnings) == 1
        assert "Vehicle.Speed" in warnings[0]

    def test_strict_mode_raises_on_duplicate(self) -> None:
        """strict=True turns warnings into DiffReportValidationError."""
        payload = {
            "changes": [
                {"label": "Vehicle", "kind": "ENTITY", "change_type": "REMOVED"},
                {"label": "Vehicle", "kind": "ENTITY", "change_type": "REMOVED"},
            ]
        }
        report = DiffReport.from_json(json.dumps(payload))
        with pytest.raises(DiffReportValidationError):
            report.validate_structure(strict=True)

    def test_same_label_different_parents_no_warning(self) -> None:
        """Properties with the same label under different parents are distinct."""
        payload = {
            "changes": [
                {
                    "label": "IsOpen",
                    "parent_label": "Vehicle.Door",
                    "kind": "PROPERTY",
                    "change_type": "REMOVED",
                },
                {
                    "label": "IsOpen",
                    "parent_label": "Vehicle.Window",
                    "kind": "PROPERTY",
                    "change_type": "REMOVED",
                },
            ]
        }
        report = DiffReport.from_json(json.dumps(payload))
        assert report.validate_structure() == []


# ── validate_report_aspects ───────────────────────────────────────────────────


class TestValidateReportAspects:
    def test_all_configured_no_warnings(self) -> None:
        """All aspect keys declared in config → no warnings (no section-level, no per-key)."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Speed",
                            "parent_label": "Vehicle",
                            "kind": "PROPERTY",
                            "change_type": "MODIFIED",
                            "aspects": {"output_type": "Float", "unit": "mph"},
                        }
                    ]
                }
            )
        )
        cfg = _config(property_={"unit": True, "output_type": True})
        assert validate_report_aspects(report, cfg) == []

    def test_section_level_warning_when_config_empty(self) -> None:
        """Empty config section for a kind produces exactly one section-level warning."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Speed",
                            "parent_label": "Vehicle",
                            "kind": "PROPERTY",
                            "change_type": "MODIFIED",
                            "aspects": {"unit": "mph"},
                        }
                    ]
                }
            )
        )
        cfg = _config()  # no aspects configured
        warnings = validate_report_aspects(report, cfg)
        # Exactly one section-level warning, no per-key warning
        assert len(warnings) == 1
        assert "property" in warnings[0].lower()

    def test_section_level_warning_emitted_once_per_kind(self) -> None:
        """Two MODIFIED events for the same kind produce only one section-level warning."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Speed",
                            "parent_label": "Vehicle",
                            "kind": "PROPERTY",
                            "change_type": "MODIFIED",
                            "aspects": {"unit": "mph"},
                        },
                        {
                            "label": "Vehicle.Mass",
                            "parent_label": "Vehicle",
                            "kind": "PROPERTY",
                            "change_type": "MODIFIED",
                            "aspects": {"unit": "kg"},
                        },
                    ]
                }
            )
        )
        cfg = _config()  # no property aspects configured
        warnings = validate_report_aspects(report, cfg)
        section_warnings = [w for w in warnings if "property" in w.lower() and "non-breaking" in w]
        assert len(section_warnings) == 1

    def test_unknown_key_produces_warning(self) -> None:
        """An aspect key not in config produces a per-key warning (plus section-level if section empty)."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Speed",
                            "parent_label": "Vehicle",
                            "kind": "PROPERTY",
                            "change_type": "MODIFIED",
                            "aspects": {"unit": "mph"},
                        }
                    ]
                }
            )
        )
        cfg = _config(property_={"output_type": True})  # unit not configured; section is non-empty
        warnings = validate_report_aspects(report, cfg)
        assert len(warnings) == 1
        assert "unit" in warnings[0]
        assert "Vehicle.Speed" in warnings[0]

    def test_strict_mode_raises_on_unknown_key(self) -> None:
        """strict=True turns warnings into DiffReportValidationError."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Speed",
                            "parent_label": "Vehicle",
                            "kind": "PROPERTY",
                            "change_type": "MODIFIED",
                            "aspects": {"unit": "mph"},
                        }
                    ]
                }
            )
        )
        cfg = _config(property_={"output_type": True})  # unit not configured; section non-empty so per-key warning
        with pytest.raises(DiffReportValidationError):
            validate_report_aspects(report, cfg, strict=True)

    def test_added_events_skipped(self) -> None:
        """ADDED events are not subject to aspect-key validation."""
        report = DiffReport.from_json(json.dumps(VALID_REPORT_DICT))
        cfg = _config(entity={"type": True}, property_={"output_type": True, "unit": True})
        assert validate_report_aspects(report, cfg) == []

    def test_removed_events_skipped(self) -> None:
        """REMOVED events carry no aspects and are always clean."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {"label": "Vehicle", "kind": "ENTITY", "change_type": "REMOVED"},
                    ]
                }
            )
        )
        cfg = _config()
        assert validate_report_aspects(report, cfg) == []

    def test_multiple_unknown_keys_multiple_warnings(self) -> None:
        """Each unknown key produces its own warning entry (when section is non-empty)."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Temp",
                            "parent_label": "Vehicle",
                            "kind": "PROPERTY",
                            "change_type": "MODIFIED",
                            "aspects": {"unit": "DEG_C", "min": -40, "accuracy": 0.5},
                        }
                    ]
                }
            )
        )
        cfg = _config(property_={"output_type": True})  # section non-empty; unit/min/accuracy undeclared
        warnings = validate_report_aspects(report, cfg)
        assert len(warnings) == 3


# ── extract_op ───────────────────────────────────────────────────────────────


class TestExtractOp:
    def test_plain_value_defaults_to_modified(self) -> None:
        """Plain (non-annotated) value returns op 'modified'."""
        assert extract_op("mph") == ("modified", "mph")
        assert extract_op(42) == ("modified", 42)
        assert extract_op(None) == ("modified", None)

    def test_annotated_added(self) -> None:
        """Dict with _op='added' and _value returns that op and value."""
        assert extract_op({"_op": "added", "_value": "mph"}) == ("added", "mph")

    def test_annotated_removed_no_value(self) -> None:
        """Dict with _op='removed' (no _value) returns ('removed', None)."""
        assert extract_op({"_op": "removed"}) == ("removed", None)

    def test_annotated_modified_with_value(self) -> None:
        """Dict with _op='modified' and _value returns that op and value."""
        assert extract_op({"_op": "modified", "_value": "new text"}) == ("modified", "new text")

    def test_dict_without_op_treated_as_plain(self) -> None:
        """Dict lacking '_op' key is treated as a plain value (op='modified')."""
        d = {"key": "value"}
        assert extract_op(d) == ("modified", d)


# ── extract_aspect_ops ────────────────────────────────────────────────────────


class TestExtractAspectOps:
    def test_plain_values_all_modified(self) -> None:
        """Plain values in aspects dict all map to 'modified'."""
        ops = extract_aspect_ops({"unit": "mph", "description": "text"})
        assert ops == {"unit": "modified", "description": "modified"}

    def test_annotated_values_use_declared_op(self) -> None:
        """Annotated values use the declared op from _op."""
        ops = extract_aspect_ops(
            {
                "unit": {"_op": "added", "_value": "mph"},
                "accuracy": {"_op": "removed"},
            }
        )
        assert ops["unit"] == "added"
        assert ops["accuracy"] == "removed"

    def test_empty_dict(self) -> None:
        """Empty aspects dict produces empty ops dict."""
        assert extract_aspect_ops({}) == {}


# ── _aspect_ops_for_event ─────────────────────────────────────────────────────


class TestAspectOpsForEvent:
    def test_entity_instances_added_preserved_as_distinct_key(self) -> None:
        """instances_added is preserved as a distinct key so both add and remove can coexist."""
        event = EntityChanged(
            label="Door",
            change_type=ChangeType.MODIFIED,
            aspects={"instances_added": ["Center"]},
        )
        ops = _aspect_ops_for_event(event)
        assert ops.get("instances_added") == "added"
        assert "instances" not in ops

    def test_entity_instances_removed_preserved_as_distinct_key(self) -> None:
        """instances_removed is preserved as a distinct key so both add and remove can coexist."""
        event = EntityChanged(
            label="Door",
            change_type=ChangeType.MODIFIED,
            aspects={"instances_removed": ["Front"]},
        )
        ops = _aspect_ops_for_event(event)
        assert ops.get("instances_removed") == "removed"
        assert "instances" not in ops

    def test_entity_instances_both_directions_no_collision(self) -> None:
        """instances_added and instances_removed coexist as distinct keys without overwriting each other."""
        event = EntityChanged(
            label="Door",
            change_type=ChangeType.MODIFIED,
            aspects={"instances_added": ["Center"], "instances_removed": ["Rear"]},
        )
        ops = _aspect_ops_for_event(event)
        assert ops.get("instances_added") == "added"
        assert ops.get("instances_removed") == "removed"

    def test_non_instance_keys_passed_through(self) -> None:
        """Non-instance aspect keys are passed through with their extracted op."""
        event = EntityChanged(
            label="Vehicle",
            change_type=ChangeType.MODIFIED,
            aspects={"type": "branch"},
        )
        ops = _aspect_ops_for_event(event)
        assert ops["type"] == "modified"

    def test_property_event_plain_passthrough(self) -> None:
        """PropertyChanged aspects are returned unchanged with extracted ops."""
        event = PropertyChanged(
            label="Vehicle.Speed",
            parent_label="Vehicle",
            change_type=ChangeType.MODIFIED,
            aspects={"unit": "mph", "description": {"_op": "modified", "_value": "text"}},
        )
        ops = _aspect_ops_for_event(event)
        assert ops["unit"] == "modified"
        assert ops["description"] == "modified"


# ── CANONICAL_ASPECT_KEYS (removed — now structural_keys in config) ────────────────────────────


class TestStructuralKeys:
    def test_property_canonical_keys_no_warning(self) -> None:
        """output_type, is_list, is_required are not structural; absent from config → per-key warning."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Speed",
                            "parent_label": "Vehicle",
                            "kind": "PROPERTY",
                            "change_type": "MODIFIED",
                            "aspects": {"output_type": "Float"},
                        }
                    ]
                }
            )
        )
        # output_type not in config; section is non-empty so per-key warning is produced
        cfg = _config(property_={"unit": True})
        warnings = validate_report_aspects(report, cfg)
        assert any("output_type" in w for w in warnings)

    def test_entity_instances_added_no_warning(self) -> None:
        """instances_added / instances_removed are structural → no unknown-key warning."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Door",
                            "kind": "ENTITY",
                            "change_type": "MODIFIED",
                            "aspects": {"instances_added": ["Center"]},
                            "content": [],
                        }
                    ]
                }
            )
        )
        cfg = _config()  # no entity aspects configured
        warnings = validate_report_aspects(report, cfg)
        # Section-level warning (no rules configured) but no per-key warning for instances
        assert not any("instances" in w and "not declared" in w for w in warnings)


# ── Vocabulary kinds (ENUMERATION_SET / ENUM_VALUE) ──────────────────────────────────────────────


class TestVocabularyKinds:
    def test_enumeration_set_entity_event(self) -> None:
        """EntityChanged accepts ENUMERATION_SET kind."""
        event = EntityChanged(
            label="SpeedUnit",
            kind=ElementKind.ENUMERATION_SET,
            change_type=ChangeType.ADDED,
            aspects={"type": "enum"},
        )
        assert event.kind == ElementKind.ENUMERATION_SET

    def test_enum_value_property_event(self) -> None:
        """PropertyChanged accepts ENUM_VALUE kind."""
        event = PropertyChanged(
            label="SpeedUnit.KMH",
            parent_label="SpeedUnit",
            kind=ElementKind.ENUM_VALUE,
            change_type=ChangeType.ADDED,
            aspects={"symbol": "km/h"},
        )
        assert event.kind == ElementKind.ENUM_VALUE

    def test_enumeration_set_wrong_event_type_rejected(self) -> None:
        """ENUMERATION_SET kind is rejected on PropertyChanged."""
        with pytest.raises(ValidationError, match="PROPERTY or ENUM_VALUE"):
            PropertyChanged(
                label="SpeedUnit",
                parent_label="Root",
                kind=ElementKind.ENUMERATION_SET,
                change_type=ChangeType.ADDED,
            )

    def test_enum_value_wrong_event_type_rejected(self) -> None:
        """ENUM_VALUE kind is rejected on EntityChanged."""
        with pytest.raises(ValidationError, match="ENTITY or ENUMERATION_SET"):
            EntityChanged(
                label="SpeedUnit.KMH",
                kind=ElementKind.ENUM_VALUE,
                change_type=ChangeType.ADDED,
            )

    def test_enumeration_set_event_uses_enumeration_set_config(self) -> None:
        """ENUMERATION_SET MODIFIED events are checked against the enumeration_set config section."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "SpeedUnit",
                            "kind": "ENUMERATION_SET",
                            "change_type": "MODIFIED",
                            "aspects": {"definition": "updated"},
                        }
                    ]
                }
            )
        )
        cfg = _config(enumeration_set={"definition": False})  # declared in enumeration_set config
        assert validate_report_aspects(report, cfg) == []

    def test_enum_value_event_uses_enum_value_config(self) -> None:
        """ENUM_VALUE MODIFIED events are checked against the enum_value config section."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "SpeedUnit.KMH",
                            "parent_label": "SpeedUnit",
                            "kind": "ENUM_VALUE",
                            "change_type": "MODIFIED",
                            "aspects": {"symbol": "km/h"},
                        }
                    ]
                }
            )
        )
        cfg = _config(enum_value={"symbol": False})  # declared in enum_value config
        assert validate_report_aspects(report, cfg) == []


# ── New tests for implemented improvements ────────────────────────────────────


# Step 1: is_breaking translates directional instance keys to canonical config key
class TestIsBreakingDirectionalInstanceKeys:
    def test_instances_added_evaluated_against_instances_added_config(self) -> None:
        """instances_added key in aspect_ops is evaluated against instances.added config rule."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"instances.added": True}})
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances_added": "added"}) is True

    def test_instances_removed_evaluated_against_instances_removed_config(self) -> None:
        """instances_removed key in aspect_ops is evaluated against instances.removed config rule."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"instances.removed": True}})
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances_removed": "removed"}) is True

    def test_instances_added_not_breaking_when_removed_is_true_only(self) -> None:
        """instances_added is non-breaking when only instances.removed: true is configured."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"instances.removed": True}})
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances_added": "added"}) is False

    def test_both_keys_independent_breaking_check(self) -> None:
        """Both keys are evaluated independently; breaking if any matching rule fires."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"instances.added": True, "instances.removed": False}})
        # instances_added fires → breaking overall
        assert cfg.is_breaking(ElementKind.ENTITY, {"instances_added": "added", "instances_removed": "removed"}) is True

    def test_directional_keys_not_unknown(self) -> None:
        """instances_added and instances_removed are structural — unknown_keys returns []."""
        cfg = BreakingChangeConfig.model_validate({"entity": {"instances.added": False}})
        unknown = cfg.unknown_keys(ElementKind.ENTITY, {"instances_added": "added"})
        assert unknown == []


# Step 3: cross-kind label duplicate warning
class TestValidateStructureCrossKind:
    def test_same_label_entity_and_property_warns(self) -> None:
        """Same label appearing as both entity and property event produces a warning."""
        report = DiffReport(
            changes=[
                EntityChanged(label="Vehicle.Speed", change_type=ChangeType.ADDED),
                PropertyChanged(
                    label="Vehicle.Speed",
                    parent_label="Vehicle",
                    change_type=ChangeType.ADDED,
                    aspects={"output_type": "Float"},
                ),
            ]
        )
        warnings = report.validate_structure()
        assert any("globally unique" in w for w in warnings)

    def test_distinct_labels_no_cross_kind_warning(self) -> None:
        """Distinct labels never produce a cross-kind warning."""
        report = DiffReport(
            changes=[
                EntityChanged(label="Vehicle", change_type=ChangeType.ADDED),
                PropertyChanged(
                    label="Vehicle.Speed",
                    parent_label="Vehicle",
                    change_type=ChangeType.ADDED,
                    aspects={"output_type": "Float"},
                ),
            ]
        )
        assert report.validate_structure() == []


# Step 6: content cross-validation
class TestValidateStructureContent:
    def test_content_item_missing_standalone_event_warns(self) -> None:
        """Content entry with no corresponding standalone event produces a warning."""
        report = DiffReport(
            changes=[
                EntityChanged(
                    label="Vehicle",
                    change_type=ChangeType.MODIFIED,
                    content=[ContentItem(label="Vehicle.Mass", change_type=ChangeType.ADDED)],
                )
                # Vehicle.Mass has no standalone event
            ]
        )
        warnings = report.validate_structure()
        assert any("Vehicle.Mass" in w and "no corresponding standalone" in w for w in warnings)

    def test_content_item_with_standalone_event_no_warning(self) -> None:
        """Content entry with a matching standalone event produces no warning."""
        report = DiffReport(
            changes=[
                EntityChanged(
                    label="Vehicle",
                    change_type=ChangeType.MODIFIED,
                    content=[ContentItem(label="Vehicle.Mass", change_type=ChangeType.ADDED)],
                ),
                PropertyChanged(
                    label="Vehicle.Mass",
                    parent_label="Vehicle",
                    change_type=ChangeType.ADDED,
                    aspects={"output_type": "Float"},
                ),
            ]
        )
        assert report.validate_structure() == []

    def test_standalone_event_not_in_parent_content_warns(self) -> None:
        """Standalone property event not listed in parent entity content produces a warning."""
        report = DiffReport(
            changes=[
                EntityChanged(
                    label="Vehicle",
                    change_type=ChangeType.MODIFIED,
                    content=[ContentItem(label="Vehicle.Mass", change_type=ChangeType.ADDED)],
                ),
                PropertyChanged(
                    label="Vehicle.Mass",
                    parent_label="Vehicle",
                    change_type=ChangeType.ADDED,
                    aspects={},
                ),
                PropertyChanged(
                    label="Vehicle.Speed",
                    parent_label="Vehicle",
                    change_type=ChangeType.MODIFIED,
                    aspects={"unit": "mph"},
                ),
            ]
        )
        warnings = report.validate_structure()
        # Vehicle.Speed is MODIFIED but not listed in Vehicle's content
        assert any("Vehicle.Speed" in w and "not listed in" in w for w in warnings)

    def test_no_parent_modified_event_no_reverse_warning(self) -> None:
        """Standalone property event whose parent has no MODIFIED event in this report does not warn."""
        report = DiffReport(
            changes=[
                PropertyChanged(
                    label="Vehicle.Speed",
                    parent_label="Vehicle",
                    change_type=ChangeType.MODIFIED,
                    aspects={"unit": "mph"},
                ),
            ]
        )
        # No Vehicle MODIFIED event in this report — no expectation to check
        assert report.validate_structure() == []
