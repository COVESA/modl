import pytest
from pydantic import ValidationError

from modl.models import BindingRow, ConceptRow, ElementKind, ElementStatus, RevisionRow, VariantRow


class TestElementStatus:
    def test_values(self) -> None:
        assert ElementStatus.ACTIVE == "ACTIVE"
        assert ElementStatus.SUPERSEDED == "SUPERSEDED"
        assert ElementStatus.REMOVED == "REMOVED"

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ConceptRow(id=0, concept_uri="ns-c:0", current_label="Vehicle", status="UNKNOWN")  # ty: ignore[invalid-argument-type]


class TestElementKind:
    def test_values(self) -> None:
        assert ElementKind.ENTITY == "ENTITY"
        assert ElementKind.PROPERTY == "PROPERTY"


class TestConceptRow:
    def test_valid(self) -> None:
        row = ConceptRow(id=0, concept_uri="ns-c:0", current_label="Vehicle", status=ElementStatus.ACTIVE)
        assert row.id == 0
        assert row.previous_labels == []
        assert row.status == ElementStatus.ACTIVE

    def test_previous_labels(self) -> None:
        row = ConceptRow(
            id=1,
            concept_uri="ns-c:1",
            current_label="Vehicle.Speed",
            previous_labels=["Vehicle.Velocity"],
            status=ElementStatus.ACTIVE,
        )
        assert row.previous_labels == ["Vehicle.Velocity"]

    def test_negative_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ConceptRow(id=-1, concept_uri="ns-c:0", current_label="Vehicle", status=ElementStatus.ACTIVE)

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ConceptRow(id=0, concept_uri="ns-c:0", status=ElementStatus.ACTIVE)  # ty: ignore[missing-argument]


class TestRevisionRow:
    def test_valid_no_previous(self) -> None:
        row = RevisionRow(id=56, concept_uri="ns-c:0", revision_uri="ns-r:56", status=ElementStatus.ACTIVE)
        assert row.previous_revision_uri is None

    def test_valid_with_previous(self) -> None:
        row = RevisionRow(
            id=103,
            concept_uri="ns-c:8",
            revision_uri="ns-r:103",
            previous_revision_uri="ns-r:57",
            status=ElementStatus.SUPERSEDED,
        )
        assert row.previous_revision_uri == "ns-r:57"


class TestVariantRow:
    def test_valid(self) -> None:
        row = VariantRow(
            id=40,
            concept_uri="ns-c:8",
            variant_uri="ns-v:40",
            revision_uri="ns-r:103",
            status=ElementStatus.ACTIVE,
        )
        assert row.variant_uri == "ns-v:40"


class TestBindingRow:
    def test_valid(self) -> None:
        row = BindingRow(
            id=24,
            variant_uri="ns-v:40",
            binding_uri="ns-b:24",
            instance_label="Left",
            status=ElementStatus.ACTIVE,
        )
        assert row.instance_label == "Left"
