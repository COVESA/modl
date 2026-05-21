from pathlib import Path

import pandas as pd
import pytest

from modl.ledger import (
    LedgerValidationError,
    empty_ledger,
    next_id,
    read_ledger,
    validate_ledger,
    validate_ledger_dir,
    write_ledger,
)


class TestEmptyLedger:
    def test_returns_four_tables(self) -> None:
        """Returns exactly the four expected table keys."""
        ledger = empty_ledger()
        assert set(ledger.keys()) == {"concepts", "revisions", "variants", "bindings"}

    def test_tables_are_empty(self) -> None:
        """All tables start with zero rows."""
        for df in empty_ledger().values():
            assert len(df) == 0

    def test_correct_columns(self) -> None:
        """Each table has exactly the expected column schema."""
        ledger = empty_ledger()
        assert list(ledger["concepts"].columns) == ["id", "concept_uri", "current_label", "previous_labels", "status"]
        assert list(ledger["revisions"].columns) == [
            "id",
            "concept_uri",
            "revision_uri",
            "previous_revision_uri",
            "status",
        ]
        assert list(ledger["variants"].columns) == ["id", "concept_uri", "variant_uri", "revision_uri", "status"]
        assert list(ledger["bindings"].columns) == ["id", "variant_uri", "binding_uri", "instance_label", "status"]

    def test_empty_ledger_passes_validation(self) -> None:
        """Empty ledger satisfies all schema constraints."""
        validate_ledger(empty_ledger())  # must not raise


class TestValidateLedger:
    def test_missing_table_raises(self) -> None:
        """Absent table name triggers validation failure."""
        ledger = empty_ledger()
        del ledger["concepts"]
        with pytest.raises(LedgerValidationError, match="Missing table"):
            validate_ledger(ledger)

    def test_missing_column_raises(self) -> None:
        """Incomplete column set triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(columns=["id", "concept_uri"])
        with pytest.raises(LedgerValidationError, match="Missing columns"):
            validate_ledger(ledger)

    def test_extra_column_raises(self) -> None:
        """Unexpected extra column triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            columns=["id", "concept_uri", "current_label", "previous_labels", "status", "extra"]
        )
        with pytest.raises(LedgerValidationError, match="Unexpected columns"):
            validate_ledger(ledger)

    def test_duplicate_id_raises(self) -> None:
        """Duplicate id within a table triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "id": [0, 0],
                "concept_uri": ["ns-c:0", "ns-c:1"],
                "current_label": ["Vehicle", "Door"],
                "previous_labels": [None, None],
                "status": ["ACTIVE", "ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="duplicate"):
            validate_ledger(ledger)

    def test_duplicate_uri_raises(self) -> None:
        """Duplicate URI value within a table triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "id": [0, 1],
                "concept_uri": ["ns-c:0", "ns-c:0"],
                "current_label": ["Vehicle", "Door"],
                "previous_labels": [None, None],
                "status": ["ACTIVE", "ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="duplicate"):
            validate_ledger(ledger)

    def test_null_required_field_raises(self) -> None:
        """Null in a required column triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "id": [0],
                "concept_uri": [None],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="null"):
            validate_ledger(ledger)

    def test_invalid_status_raises(self) -> None:
        """Unrecognised status string triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "id": [0],
                "concept_uri": ["ns-c:0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "status": ["PENDING"],
            }
        )
        with pytest.raises(LedgerValidationError, match="Invalid status"):
            validate_ledger(ledger)

    def test_fk_violation_raises(self) -> None:
        """Revision referencing a non-existent concept_uri triggers validation failure."""
        ledger = empty_ledger()
        ledger["revisions"] = pd.DataFrame(
            {
                "id": [0],
                "concept_uri": ["ns-c:99"],  # does not exist in concepts
                "revision_uri": ["ns-r:0"],
                "previous_revision_uri": [None],
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="References missing"):
            validate_ledger(ledger)

    def test_valid_populated_ledger_passes(self) -> None:
        """Fully cross-linked four-table ledger passes all constraints."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "id": [0],
                "concept_uri": ["ns-c:0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "status": ["ACTIVE"],
            }
        )
        ledger["revisions"] = pd.DataFrame(
            {
                "id": [0],
                "concept_uri": ["ns-c:0"],
                "revision_uri": ["ns-r:0"],
                "previous_revision_uri": [None],
                "status": ["ACTIVE"],
            }
        )
        ledger["variants"] = pd.DataFrame(
            {
                "id": [0],
                "concept_uri": ["ns-c:0"],
                "variant_uri": ["ns-v:0"],
                "revision_uri": ["ns-r:0"],
                "status": ["ACTIVE"],
            }
        )
        ledger["bindings"] = pd.DataFrame(
            {
                "id": [0],
                "variant_uri": ["ns-v:0"],
                "binding_uri": ["ns-b:0"],
                "instance_label": ["Left"],
                "status": ["ACTIVE"],
            }
        )
        validate_ledger(ledger)  # must not raise


class TestNextId:
    def test_empty_table_returns_zero(self) -> None:
        """No rows → next id is 0."""
        df = pd.DataFrame(columns=["id"])
        assert next_id(df) == 0

    def test_returns_max_plus_one(self) -> None:
        """Next id is one above the current maximum, regardless of row order."""
        df = pd.DataFrame({"id": [0, 5, 3]})
        assert next_id(df) == 6

    def test_single_row(self) -> None:
        """Single-row table gives max+1 correctly."""
        df = pd.DataFrame({"id": [42]})
        assert next_id(df) == 43


class TestReadWriteLedger:
    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        """Written CSVs survive a read+validate cycle with data intact."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "id": [0],
                "concept_uri": ["ns-c:0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "status": ["ACTIVE"],
            }
        )
        write_ledger(ledger, tmp_path)
        for name in ("concepts", "revisions", "variants", "bindings"):
            assert (tmp_path / f"{name}.csv").exists()
        restored = read_ledger(tmp_path)
        assert restored["concepts"]["current_label"].iloc[0] == "Vehicle"

    def test_read_missing_file_raises(self, tmp_path: Path) -> None:
        """Empty dir (no CSVs) is treated as missing files by validate_ledger_dir."""
        with pytest.raises(LedgerValidationError, match="missing files"):
            read_ledger(tmp_path)

    def test_write_creates_directory(self, tmp_path: Path) -> None:
        """write_ledger creates nested parent directories if absent."""
        ledger_dir = tmp_path / "ledger" / "nested"
        write_ledger(empty_ledger(), ledger_dir)
        assert ledger_dir.exists()


class TestValidateLedgerDir:
    def test_valid_dir_passes(self, tmp_path: Path) -> None:
        """All four CSVs present and nothing else → no error."""
        write_ledger(empty_ledger(), tmp_path)
        validate_ledger_dir(tmp_path)  # must not raise

    def test_path_is_file_raises(self, tmp_path: Path) -> None:
        """File path (not a directory) is rejected."""
        f = tmp_path / "notadir"
        f.write_text("x")
        with pytest.raises(LedgerValidationError, match="not a directory"):
            validate_ledger_dir(f)

    def test_missing_csv_raises(self, tmp_path: Path) -> None:
        """Removing one CSV triggers missing-files error."""
        write_ledger(empty_ledger(), tmp_path)
        (tmp_path / "concepts.csv").unlink()
        with pytest.raises(LedgerValidationError, match="missing files"):
            validate_ledger_dir(tmp_path)

    def test_extra_file_raises(self, tmp_path: Path) -> None:
        """Any file beyond the four CSVs is rejected."""
        write_ledger(empty_ledger(), tmp_path)
        (tmp_path / "extra.csv").write_text("id\n0\n")
        with pytest.raises(LedgerValidationError, match="unexpected files"):
            validate_ledger_dir(tmp_path)

    def test_read_ledger_calls_dir_validation(self, tmp_path: Path) -> None:
        """read_ledger rejects dirs that contain unexpected files."""
        write_ledger(empty_ledger(), tmp_path)
        (tmp_path / "stray.txt").write_text("oops")
        with pytest.raises(LedgerValidationError, match="unexpected files"):
            read_ledger(tmp_path)
