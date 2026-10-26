import pytest
from pydantic import ValidationError

from modl.models import (
    BindingRow,
    ConceptRow,
    ContractReferenceRow,
    ContractRow,
    ElementKind,
    ElementStatus,
    RevisionAspectRow,
    RevisionRow,
)


class TestElementStatus:
    def test_values(self) -> None:
        """All three status values are string-compatible."""
        assert ElementStatus.ACTIVE == "ACTIVE"
        assert ElementStatus.SUPERSEDED == "SUPERSEDED"
        assert ElementStatus.REMOVED == "REMOVED"

    def test_invalid_status_rejected(self) -> None:
        """Unknown status string fails pydantic validation."""
        with pytest.raises(ValidationError):
            ConceptRow(
                serial=0,
                concept_uri="ns-c:0",
                current_label="Vehicle",
                kind=ElementKind.ENTITY,
                status="UNKNOWN",  # ty: ignore[invalid-argument-type]
            )


class TestElementKind:
    def test_values(self) -> None:
        """All four kind values are string-compatible."""
        assert ElementKind.ENTITY == "ENTITY"
        assert ElementKind.PROPERTY == "PROPERTY"
        assert ElementKind.ENUMERATION_SET == "ENUMERATION_SET"
        assert ElementKind.ENUM_VALUE == "ENUM_VALUE"


class TestConceptRow:
    def test_valid(self) -> None:
        """Minimal concept row; previous_labels defaults to empty list."""
        row = ConceptRow(
            serial=0,
            concept_uri="ns-c:0",
            current_label="Vehicle",
            kind=ElementKind.ENTITY,
            status=ElementStatus.ACTIVE,
        )
        assert row.serial == 0
        assert row.previous_labels == []
        assert row.kind == ElementKind.ENTITY
        assert row.status == ElementStatus.ACTIVE

    def test_vocabulary_kinds(self) -> None:
        """ENUMERATION_SET and ENUM_VALUE are accepted on ConceptRow."""
        enum_set = ConceptRow(
            serial=5,
            concept_uri="ns-c:5",
            current_label="SpeedUnit",
            kind=ElementKind.ENUMERATION_SET,
            status=ElementStatus.ACTIVE,
        )
        enum_val = ConceptRow(
            serial=6,
            concept_uri="ns-c:6",
            current_label="SpeedUnit.KMH",
            kind=ElementKind.ENUM_VALUE,
            status=ElementStatus.ACTIVE,
        )
        assert enum_set.kind == ElementKind.ENUMERATION_SET
        assert enum_val.kind == ElementKind.ENUM_VALUE

    def test_previous_labels(self) -> None:
        """previous_labels list is preserved as-is."""
        row = ConceptRow(
            serial=1,
            concept_uri="ns-c:1",
            current_label="Vehicle.Speed",
            previous_labels=["Vehicle.Velocity"],
            kind=ElementKind.PROPERTY,
            status=ElementStatus.ACTIVE,
        )
        assert row.previous_labels == ["Vehicle.Velocity"]

    def test_parent_uri_defaults_to_none(self) -> None:
        """parent_uri defaults to None when not supplied."""
        row = ConceptRow(
            serial=0,
            concept_uri="ns-c:0",
            current_label="Vehicle",
            kind=ElementKind.ENTITY,
            status=ElementStatus.ACTIVE,
        )
        assert row.parent_uri is None

    def test_parent_uri_set_for_property(self) -> None:
        """parent_uri is accepted for PROPERTY concepts."""
        row = ConceptRow(
            serial=1,
            concept_uri="ns-c:1",
            current_label="Vehicle.Speed",
            kind=ElementKind.PROPERTY,
            status=ElementStatus.ACTIVE,
            parent_uri="ns-c:0",
        )
        assert row.parent_uri == "ns-c:0"

    def test_instances_defaults_to_none(self) -> None:
        """instances defaults to None when not supplied."""
        row = ConceptRow(
            serial=0,
            concept_uri="ns-c:0",
            current_label="Vehicle",
            kind=ElementKind.ENTITY,
            status=ElementStatus.ACTIVE,
        )
        assert row.instances is None

    def test_instances_accepted_as_dimension_dict(self) -> None:
        """instances is accepted as a dimension -> list-of-strings dictionary."""
        row = ConceptRow(
            serial=2,
            concept_uri="ns-c:2",
            current_label="Door",
            kind=ElementKind.ENTITY,
            status=ElementStatus.ACTIVE,
            instances={"side": ["Left", "Right"]},
        )
        assert row.instances == {"side": ["Left", "Right"]}

    def test_instances_list_rejected(self) -> None:
        """A flat list is no longer a valid instances value."""
        with pytest.raises(ValidationError):
            ConceptRow(
                serial=2,
                concept_uri="ns-c:2",
                current_label="Door",
                kind=ElementKind.ENTITY,
                status=ElementStatus.ACTIVE,
                instances=["Left", "Right"],  # ty: ignore[invalid-argument-type]
            )

    def test_negative_id_rejected(self) -> None:
        """ID must be non-negative."""
        with pytest.raises(ValidationError):
            ConceptRow(
                serial=-1,
                concept_uri="ns-c:0",
                current_label="Vehicle",
                kind=ElementKind.ENTITY,
                status=ElementStatus.ACTIVE,
            )

    def test_missing_required_field_rejected(self) -> None:
        """Omitting current_label fails validation."""
        with pytest.raises(ValidationError):
            ConceptRow(serial=0, concept_uri="ns-c:0", kind=ElementKind.ENTITY, status=ElementStatus.ACTIVE)  # ty: ignore[missing-argument]


class TestRevisionRow:
    def test_valid_no_previous(self) -> None:
        """First revision has no previous_revision_uri."""
        row = RevisionRow(serial=56, concept_uri="ns-c:0", revision_uri="ns-r:56", status=ElementStatus.ACTIVE)
        assert row.previous_revision_uri is None

    def test_valid_with_previous(self) -> None:
        """Subsequent revision links back to its predecessor."""
        row = RevisionRow(
            serial=103,
            concept_uri="ns-c:8",
            revision_uri="ns-r:103",
            previous_revision_uri="ns-r:57",
            status=ElementStatus.SUPERSEDED,
        )
        assert row.previous_revision_uri == "ns-r:57"


class TestContractRow:
    def test_valid(self) -> None:
        """Contract links a concept URI to a specific revision."""
        row = ContractRow(
            serial=40,
            concept_uri="ns-c:8",
            contract_uri="ns-ct:40",
            revision_uri="ns-r:103",
            status=ElementStatus.ACTIVE,
        )
        assert row.contract_uri == "ns-ct:40"


class TestBindingRow:
    def test_valid(self) -> None:
        """Binding attaches instance tags and a runtime path to a contract URI."""
        row = BindingRow(
            serial=24,
            contract_uri="ns-ct:40",
            binding_uri="ns-b:24",
            instance_tags={"ns-c:2": {"side": "Left"}},
            runtime_path="Door[Left].IsOpen",
            status=ElementStatus.ACTIVE,
        )
        assert row.instance_tags == {"ns-c:2": {"side": "Left"}}
        assert row.runtime_path == "Door[Left].IsOpen"

    def test_singleton_binding_empty_tags(self) -> None:
        """A singleton binding carries empty tags."""
        row = BindingRow(
            serial=42,
            contract_uri="ns-ct:40",
            binding_uri="ns-b:42",
            instance_tags={},
            runtime_path="Vehicle.Speed",
            status=ElementStatus.ACTIVE,
        )
        assert row.instance_tags == {}

    def test_runtime_path_required(self) -> None:
        """Omitting runtime_path fails validation."""
        with pytest.raises(ValidationError):
            BindingRow(  # ty: ignore[missing-argument]
                serial=42,
                contract_uri="ns-ct:40",
                binding_uri="ns-b:42",
                instance_tags={},
                status=ElementStatus.ACTIVE,
            )


class TestContractReferenceRow:
    def test_valid_row(self) -> None:
        """A row with role and target is accepted."""
        row = ContractReferenceRow(contract_uri="c/0", role="output_type", target_contract_uri="c/1")
        assert row.role == "output_type"

    def test_empty_role_rejected(self) -> None:
        """An empty role is rejected."""
        with pytest.raises(ValidationError):
            ContractReferenceRow(contract_uri="c/0", role="", target_contract_uri="c/1")


class TestRevisionAspectRowReason:
    def test_reason_defaults_to_none(self) -> None:
        """reason is optional."""
        row = RevisionAspectRow(revision_uri="r/0", aspect_key="unit", operation="added", newer_value="x")
        assert row.reason is None

    def test_reason_is_stored(self) -> None:
        """reason carries engine-derived provenance."""
        row = RevisionAspectRow(
            revision_uri="r/0",
            aspect_key="output_type",
            operation="modified",
            previous_value="a",
            newer_value="b",
            reason="referenced-contract-changed",
        )
        assert row.reason == "referenced-contract-changed"


class TestRevisionAspectRow:
    def test_valid_added_row(self) -> None:
        """operation='added' requires previous_value to be null."""
        row = RevisionAspectRow(
            revision_uri="ns-r:0",
            aspect_key="unit",
            operation="added",
            newer_value="km/h",
        )
        assert row.previous_value is None
        assert row.newer_value == "km/h"

    def test_valid_modified_row(self) -> None:
        """operation='modified' requires both previous_value and newer_value to be non-null."""
        row = RevisionAspectRow(
            revision_uri="ns-r:0",
            aspect_key="unit",
            operation="modified",
            previous_value="km/h",
            newer_value="mph",
        )
        assert row.previous_value == "km/h"
        assert row.newer_value == "mph"

    def test_valid_removed_row(self) -> None:
        """operation='removed' requires newer_value to be null."""
        row = RevisionAspectRow(
            revision_uri="ns-r:0",
            aspect_key="unit",
            operation="removed",
            previous_value="km/h",
        )
        assert row.newer_value is None

    def test_invalid_operation_rejected(self) -> None:
        """Operation outside {added, modified, removed} fails validation."""
        with pytest.raises(ValidationError, match="operation"):
            RevisionAspectRow(
                revision_uri="ns-r:0",
                aspect_key="unit",
                operation="renamed",
            )

    def test_modified_with_null_previous_value_rejected(self) -> None:
        """operation='modified' with null previous_value fails validation."""
        with pytest.raises(ValidationError, match="previous_value"):
            RevisionAspectRow(
                revision_uri="ns-r:0",
                aspect_key="unit",
                operation="modified",
                newer_value="mph",
            )

    def test_modified_with_null_newer_value_rejected(self) -> None:
        """operation='modified' with null newer_value fails validation."""
        with pytest.raises(ValidationError, match="newer_value"):
            RevisionAspectRow(
                revision_uri="ns-r:0",
                aspect_key="unit",
                operation="modified",
                previous_value="km/h",
            )

    def test_added_with_non_null_previous_value_rejected(self) -> None:
        """operation='added' with non-null previous_value fails validation."""
        with pytest.raises(ValidationError, match="previous_value"):
            RevisionAspectRow(
                revision_uri="ns-r:0",
                aspect_key="unit",
                operation="added",
                previous_value="km/h",
                newer_value="mph",
            )

    def test_removed_with_non_null_newer_value_rejected(self) -> None:
        """operation='removed' with non-null newer_value fails validation."""
        with pytest.raises(ValidationError, match="newer_value"):
            RevisionAspectRow(
                revision_uri="ns-r:0",
                aspect_key="unit",
                operation="removed",
                previous_value="km/h",
                newer_value="mph",
            )
