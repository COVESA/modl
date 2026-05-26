import json

import pytest
from pydantic import ValidationError

from modl.config import BreakingChangeConfig
from modl.ir import (
    CANONICAL_ASPECT_KEYS,
    CANONICAL_ENTITY_ASPECT_KEYS,
    ChangeType,
    ContentItem,
    DiffReport,
    DiffReportValidationError,
    EntityChanged,
    PropertyChanged,
    validate_report_aspects,
)
from modl.models import ElementKind

# ── Fixtures ──────────────────────────────────────────────────────────────────

NS = "http://example.org/myns/"


def _config(**kwargs) -> BreakingChangeConfig:
    """Return a minimal config, optionally overriding entity/property aspect dicts."""
    return BreakingChangeConfig.model_validate(
        {
            "namespace": {"namespace": NS},
            "entity": kwargs.get("entity", {}),
            "property": kwargs.get("property_", {}),
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
        """MODIFIED entity carries only changed keys in aspects."""
        event = EntityChanged(
            label="Vehicle.Door",
            change_type=ChangeType.MODIFIED,
            aspects={"instances": ["Left", "Right", "Center"]},
            content=[ContentItem(label="Vehicle.Door.IsLocked", change_type=ChangeType.ADDED)],
        )
        assert event.aspects == {"instances": ["Left", "Right", "Center"]}
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

    def test_removed_empty_payload(self) -> None:
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
        """All aspect keys declared in config → no warnings."""
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
        cfg = _config(property_={"unit": True})
        assert validate_report_aspects(report, cfg) == []

    def test_canonical_keys_always_known(self) -> None:
        """Canonical keys (output_type, is_list, is_required) never produce warnings."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Speed",
                            "parent_label": "Vehicle",
                            "kind": "PROPERTY",
                            "change_type": "MODIFIED",
                            "aspects": {"output_type": "Float", "is_list": False, "is_required": True},
                        }
                    ]
                }
            )
        )
        cfg = _config()  # no aspects configured
        assert validate_report_aspects(report, cfg) == []

    def test_unknown_key_produces_warning(self) -> None:
        """An aspect key not in config and not canonical produces a warning."""
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
        cfg = _config()  # unit not configured
        warnings = validate_report_aspects(report, cfg)
        assert len(warnings) == 1
        assert "unit" in warnings[0]
        assert "Vehicle.Speed" in warnings[0]

    def test_strict_mode_raises_on_unknown_key(self) -> None:
        """strict=True turns unknown-key warnings into DiffReportValidationError."""
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
        cfg = _config()
        with pytest.raises(DiffReportValidationError):
            validate_report_aspects(report, cfg, strict=True)

    def test_added_events_skipped(self) -> None:
        """ADDED events are not subject to aspect-key validation."""
        report = DiffReport.from_json(json.dumps(VALID_REPORT_DICT))
        cfg = _config()  # no aspects configured
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
        """Each unknown key produces its own warning entry."""
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
        cfg = _config()
        warnings = validate_report_aspects(report, cfg)
        assert len(warnings) == 3


# ── CANONICAL_ASPECT_KEYS ─────────────────────────────────────────────────────


class TestCanonicalAspectKeys:
    def test_canonical_set_contents(self) -> None:
        """Canonical aspect key set contains exactly the expected keys."""
        assert frozenset({"output_type", "is_list", "is_required"}) == CANONICAL_ASPECT_KEYS


# ── CANONICAL_ENTITY_ASPECT_KEYS ───────────────────────────────────────────────────────────────────


class TestCanonicalEntityAspectKeys:
    def test_canonical_entity_set_contents(self) -> None:
        """Entity canonical aspect key set contains only instances."""
        assert frozenset({"instances"}) == CANONICAL_ENTITY_ASPECT_KEYS

    def test_instances_key_on_entity_modified_no_warning(self) -> None:
        """The canonical 'instances' key on an entity MODIFIED event never produces a warning."""
        report = DiffReport.from_json(
            json.dumps(
                {
                    "changes": [
                        {
                            "label": "Vehicle.Door",
                            "kind": "ENTITY",
                            "change_type": "MODIFIED",
                            "aspects": {"instances": ["Left", "Right"]},
                            "content": [],
                        }
                    ]
                }
            )
        )
        cfg = _config()  # no entity aspects configured
        assert validate_report_aspects(report, cfg) == []


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

    def test_enumeration_set_event_uses_entity_config(self) -> None:
        """ENUMERATION_SET MODIFIED events are checked against the entity config."""
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
        cfg = _config(entity={"definition": False})  # declared in entity config
        assert validate_report_aspects(report, cfg) == []

    def test_enum_value_event_uses_property_config(self) -> None:
        """ENUM_VALUE MODIFIED events are checked against the property config."""
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
        cfg = _config(property_={"symbol": False})  # declared in property config
        assert validate_report_aspects(report, cfg) == []
