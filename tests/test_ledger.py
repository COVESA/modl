from pathlib import Path

import pandas as pd
import pytest

from modl.ledger import (
    LedgerValidationError,
    b36decode,
    b36encode,
    empty_ledger,
    next_serial,
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
        assert list(ledger["concepts"].columns) == [
            "serial",
            "concept_uri",
            "current_label",
            "previous_labels",
            "kind",
            "status",
        ]
        assert list(ledger["revisions"].columns) == [
            "serial",
            "concept_uri",
            "revision_uri",
            "previous_revision_uri",
            "status",
        ]
        assert list(ledger["variants"].columns) == ["serial", "concept_uri", "variant_uri", "revision_uri", "status"]
        assert list(ledger["bindings"].columns) == ["serial", "variant_uri", "binding_uri", "instance_label", "status"]

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
        ledger["concepts"] = pd.DataFrame(columns=["serial", "concept_uri"])
        with pytest.raises(LedgerValidationError, match="Missing columns"):
            validate_ledger(ledger)

    def test_extra_column_raises(self) -> None:
        """Unexpected extra column triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            columns=["serial", "concept_uri", "current_label", "previous_labels", "kind", "status", "extra"]
        )
        with pytest.raises(LedgerValidationError, match="Unexpected columns"):
            validate_ledger(ledger)

    def test_duplicate_serial_raises(self) -> None:
        """Duplicate serial within a table triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0, 0],
                "concept_uri": ["ns-c:0", "ns-c:1"],
                "current_label": ["Vehicle", "Door"],
                "previous_labels": [None, None],
                "kind": ["ENTITY", "ENTITY"],
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
                "serial": [0, 1],
                "concept_uri": ["ns-c:0", "ns-c:0"],
                "current_label": ["Vehicle", "Door"],
                "previous_labels": [None, None],
                "kind": ["ENTITY", "ENTITY"],
                "status": ["ACTIVE", "ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="duplicate"):
            validate_ledger(ledger)

    def test_null_instance_label_in_bindings_is_valid(self) -> None:
        """Null instance_label in bindings is permitted (singleton binding)."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "current_label": ["Battery.StateOfCharge"],
                "previous_labels": [None],
                "kind": ["PROPERTY"],
                "status": ["ACTIVE"],
            }
        )
        ledger["revisions"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "revision_uri": ["http://ns.example/revisions/0"],
                "previous_revision_uri": [None],
                "status": ["ACTIVE"],
            }
        )
        ledger["variants"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "variant_uri": ["http://ns.example/variants/0"],
                "revision_uri": ["http://ns.example/revisions/0"],
                "status": ["ACTIVE"],
            }
        )
        ledger["bindings"] = pd.DataFrame(
            {
                "serial": [0],
                "variant_uri": ["http://ns.example/variants/0"],
                "binding_uri": ["http://ns.example/bindings/0"],
                "instance_label": [None],  # singleton — no instance label
                "status": ["ACTIVE"],
            }
        )
        validate_ledger(ledger)  # must not raise

    def test_null_required_field_raises(self) -> None:
        """Null in a required column triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": [None],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["ENTITY"],
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
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["ENTITY"],
                "status": ["PENDING"],
            }
        )
        with pytest.raises(LedgerValidationError, match="Invalid status"):
            validate_ledger(ledger)

    def test_invalid_kind_raises(self) -> None:
        """Unrecognised kind string in concepts triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["BRANCH"],  # not a valid ElementKind
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="Invalid kind"):
            validate_ledger(ledger)

    def test_fk_violation_raises(self) -> None:
        """Revision referencing a non-existent concept_uri triggers validation failure."""
        ledger = empty_ledger()
        ledger["revisions"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/2v"],  # does not exist in concepts
                "revision_uri": ["http://ns.example/revisions/0"],
                "previous_revision_uri": [None],
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="References missing"):
            validate_ledger(ledger)

    def test_negative_serial_raises(self) -> None:
        """Negative serial triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [-1],
                "concept_uri": ["ns-c:0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["ENTITY"],
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="negative"):
            validate_ledger(ledger)

    def test_uri_suffix_mismatch_raises(self) -> None:
        """URI suffix that does not match base-36 encoding of serial triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [1],
                "concept_uri": ["http://ns.example/concepts/z"],  # z = b36(35), not b36(1)
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["ENTITY"],
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="URI suffix does not match"):
            validate_ledger(ledger)

    def test_uri_suffix_invalid_b36_raises(self) -> None:
        """URI suffix that is not valid base-36 triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/NOT_B36!"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["ENTITY"],
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="invalid base-36 suffix"):
            validate_ledger(ledger)

    def test_self_fk_previous_revision_raises(self) -> None:
        """previous_revision_uri referencing a non-existent revision_uri triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["ENTITY"],
                "status": ["ACTIVE"],
            }
        )
        ledger["revisions"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "revision_uri": ["http://ns.example/revisions/0"],
                "previous_revision_uri": ["http://ns.example/revisions/2v"],  # does not exist
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="References missing"):
            validate_ledger(ledger)

    def test_variant_revision_concept_mismatch_raises(self) -> None:
        """Variant whose revision_uri belongs to a different concept triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0, 1],
                "concept_uri": ["http://ns.example/concepts/0", "http://ns.example/concepts/1"],
                "current_label": ["Vehicle", "Door"],
                "previous_labels": [None, None],
                "kind": ["ENTITY", "ENTITY"],
                "status": ["ACTIVE", "ACTIVE"],
            }
        )
        ledger["revisions"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],  # revision belongs to concept 0
                "revision_uri": ["http://ns.example/revisions/0"],
                "previous_revision_uri": [None],
                "status": ["ACTIVE"],
            }
        )
        ledger["variants"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/1"],  # variant claims concept 1
                "variant_uri": ["http://ns.example/variants/0"],
                "revision_uri": ["http://ns.example/revisions/0"],  # but revision belongs to concept 0
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="different concept"):
            validate_ledger(ledger)

    def test_valid_populated_ledger_passes(self) -> None:
        """Fully cross-linked four-table ledger passes all constraints."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["PROPERTY"],
                "status": ["ACTIVE"],
            }
        )
        ledger["revisions"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "revision_uri": ["http://ns.example/revisions/0"],
                "previous_revision_uri": [None],
                "status": ["ACTIVE"],
            }
        )
        ledger["variants"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "variant_uri": ["http://ns.example/variants/0"],
                "revision_uri": ["http://ns.example/revisions/0"],
                "status": ["ACTIVE"],
            }
        )
        ledger["bindings"] = pd.DataFrame(
            {
                "serial": [0],
                "variant_uri": ["http://ns.example/variants/0"],
                "binding_uri": ["http://ns.example/bindings/0"],
                "instance_label": ["Left"],
                "status": ["ACTIVE"],
            }
        )
        validate_ledger(ledger)  # must not raise

    def test_vocab_concept_binding_raises(self) -> None:
        """ENUMERATION_SET or ENUM_VALUE concept with a binding triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "current_label": ["SpeedUnit.KMH"],
                "previous_labels": [None],
                "kind": ["ENUM_VALUE"],
                "status": ["ACTIVE"],
            }
        )
        ledger["revisions"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "revision_uri": ["http://ns.example/revisions/0"],
                "previous_revision_uri": [None],
                "status": ["ACTIVE"],
            }
        )
        ledger["variants"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "variant_uri": ["http://ns.example/variants/0"],
                "revision_uri": ["http://ns.example/revisions/0"],
                "status": ["ACTIVE"],
            }
        )
        ledger["bindings"] = pd.DataFrame(
            {
                "serial": [0],
                "variant_uri": ["http://ns.example/variants/0"],
                "binding_uri": ["http://ns.example/bindings/0"],
                "instance_label": [None],
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="Only PROPERTY concepts"):
            validate_ledger(ledger)

    def test_entity_concept_binding_raises(self) -> None:
        """ENTITY concept with a binding triggers validation failure."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "current_label": ["Vehicle.Door"],
                "previous_labels": [None],
                "kind": ["ENTITY"],
                "status": ["ACTIVE"],
            }
        )
        ledger["revisions"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "revision_uri": ["http://ns.example/revisions/0"],
                "previous_revision_uri": [None],
                "status": ["ACTIVE"],
            }
        )
        ledger["variants"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "variant_uri": ["http://ns.example/variants/0"],
                "revision_uri": ["http://ns.example/revisions/0"],
                "status": ["ACTIVE"],
            }
        )
        ledger["bindings"] = pd.DataFrame(
            {
                "serial": [0],
                "variant_uri": ["http://ns.example/variants/0"],
                "binding_uri": ["http://ns.example/bindings/0"],
                "instance_label": [None],
                "status": ["ACTIVE"],
            }
        )
        with pytest.raises(LedgerValidationError, match="Only PROPERTY concepts"):
            validate_ledger(ledger)


class TestNextSerial:
    def test_empty_table_returns_zero(self) -> None:
        """No rows → next serial is 0."""
        df = pd.DataFrame(columns=["serial"])
        assert next_serial(df) == 0

    def test_returns_max_plus_one(self) -> None:
        """Next serial is one above the current maximum, regardless of row order."""
        df = pd.DataFrame({"serial": [0, 5, 3]})
        assert next_serial(df) == 6

    def test_single_row(self) -> None:
        """Single-row table gives max+1 correctly."""
        df = pd.DataFrame({"serial": [42]})
        assert next_serial(df) == 43


class TestReadWriteLedger:
    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        """Written CSVs survive a read+validate cycle with data intact."""
        ledger = empty_ledger()
        ledger["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://ns.example/concepts/0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["ENTITY"],
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


class TestB36:
    def test_zero(self) -> None:
        """Zero encodes to '0'."""
        assert b36encode(0) == "0"

    def test_single_digits(self) -> None:
        """Values 1–9 encode as single decimal digits."""
        assert b36encode(1) == "1"
        assert b36encode(9) == "9"

    def test_letter_range(self) -> None:
        """Values 10–35 encode as a single lowercase letter (a–z)."""
        assert b36encode(10) == "a"
        assert b36encode(35) == "z"

    def test_two_digit_boundary(self) -> None:
        """36 is the first two-character value ('10' in base 36)."""
        assert b36encode(36) == "10"

    def test_known_values(self) -> None:
        """Spot-check values used in README examples."""
        assert b36encode(24) == "o"
        assert b36encode(25) == "p"
        assert b36encode(40) == "14"
        assert b36encode(56) == "1k"
        assert b36encode(57) == "1l"
        assert b36encode(103) == "2v"

    def test_roundtrip(self) -> None:
        """b36decode(b36encode(n)) == n for a range of values."""
        for n in (0, 1, 35, 36, 100, 999, 123456):
            assert b36decode(b36encode(n)) == n

    def test_negative_raises(self) -> None:
        """Negative input raises ValueError."""
        with pytest.raises(ValueError, match="non-negative"):
            b36encode(-1)
