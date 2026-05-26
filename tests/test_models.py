import pytest
from pydantic import ValidationError

from modl.models import BindingRow, ConceptRow, ElementKind, ElementStatus, RevisionRow, VariantRow


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


class TestVariantRow:
    def test_valid(self) -> None:
        """Variant links a concept URI to a specific revision."""
        row = VariantRow(
            serial=40,
            concept_uri="ns-c:8",
            variant_uri="ns-v:40",
            revision_uri="ns-r:103",
            status=ElementStatus.ACTIVE,
        )
        assert row.variant_uri == "ns-v:40"


class TestBindingRow:
    def test_valid(self) -> None:
        """Binding attaches an instance label to a variant URI."""
        row = BindingRow(
            serial=24,
            variant_uri="ns-v:40",
            binding_uri="ns-b:24",
            instance_label="Left",
            status=ElementStatus.ACTIVE,
        )
        assert row.instance_label == "Left"

    def test_singleton_binding_no_instance_label(self) -> None:
        """Binding with no instance label is valid (singleton — parent has no instances)."""
        row = BindingRow(
            serial=42,
            variant_uri="ns-v:40",
            binding_uri="ns-b:42",
            status=ElementStatus.ACTIVE,
        )
        assert row.instance_label is None
