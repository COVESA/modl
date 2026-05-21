import json

import pytest
from pydantic import ValidationError

from modl.ir import ChangeType, DiffReport, EntityChanged, PropertyChanged
from modl.models import ElementKind

VALID_REPORT = {
    "changes": [
        {
            "label": "Vehicle",
            "kind": "ENTITY",
            "change_type": "ADDED",
            "changed_attributes": {},
        },
        {
            "label": "Vehicle.Speed",
            "parent_label": "Vehicle",
            "kind": "PROPERTY",
            "change_type": "MODIFIED",
            "changed_attributes": {"datatype": "Float"},
        },
    ]
}


class TestChangeType:
    def test_values(self) -> None:
        assert ChangeType.ADDED == "ADDED"
        assert ChangeType.REMOVED == "REMOVED"
        assert ChangeType.MODIFIED == "MODIFIED"


class TestEntityChanged:
    def test_defaults(self) -> None:
        event = EntityChanged(label="Vehicle", change_type=ChangeType.ADDED)
        assert event.kind == ElementKind.ENTITY
        assert event.changed_attributes == {}

    def test_with_attributes(self) -> None:
        event = EntityChanged(
            label="Vehicle.Door",
            change_type=ChangeType.MODIFIED,
            changed_attributes={"instances": ["Left", "Right", "Center"]},
        )
        assert event.changed_attributes["instances"] == ["Left", "Right", "Center"]

    def test_invalid_change_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EntityChanged(label="Vehicle", change_type="RENAMED")  # ty: ignore[invalid-argument-type]


class TestPropertyChanged:
    def test_valid(self) -> None:
        event = PropertyChanged(
            label="Vehicle.Speed",
            parent_label="Vehicle",
            change_type=ChangeType.MODIFIED,
            changed_attributes={"datatype": "Float"},
        )
        assert event.kind == ElementKind.PROPERTY
        assert event.parent_label == "Vehicle"

    def test_missing_parent_label_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PropertyChanged(label="Vehicle.Speed", change_type=ChangeType.ADDED)  # ty: ignore[missing-argument]


class TestDiffReport:
    def test_parse_valid_json(self) -> None:
        report = DiffReport.from_json(json.dumps(VALID_REPORT))
        assert len(report.changes) == 2
        assert isinstance(report.changes[0], EntityChanged)
        assert isinstance(report.changes[1], PropertyChanged)

    def test_entity_change_parsed(self) -> None:
        report = DiffReport.from_json(json.dumps(VALID_REPORT))
        entity = report.changes[0]
        assert isinstance(entity, EntityChanged)
        assert entity.label == "Vehicle"
        assert entity.change_type == ChangeType.ADDED

    def test_property_change_parsed(self) -> None:
        report = DiffReport.from_json(json.dumps(VALID_REPORT))
        prop = report.changes[1]
        assert isinstance(prop, PropertyChanged)
        assert prop.label == "Vehicle.Speed"
        assert prop.changed_attributes == {"datatype": "Float"}

    def test_empty_changes(self) -> None:
        report = DiffReport.from_json(json.dumps({"changes": []}))
        assert report.changes == []

    def test_invalid_json_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DiffReport.from_json("not valid json")

    def test_missing_changes_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DiffReport.from_json(json.dumps({"events": []}))
