"""Ledger I/O, schema validation, and ID minting for the four ledger CSV tables."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from modl.models import ElementStatus

# ── Schema constants ──────────────────────────────────────────────────────────

TABLES = ("concepts", "revisions", "variants", "bindings")

EXPECTED_COLUMNS: dict[str, list[str]] = {
    "concepts": ["serial", "concept_uri", "current_label", "previous_labels", "status"],
    "revisions": ["serial", "concept_uri", "revision_uri", "previous_revision_uri", "status"],
    "variants": ["serial", "concept_uri", "variant_uri", "revision_uri", "status"],
    "bindings": ["serial", "variant_uri", "binding_uri", "instance_label", "status"],
}

UNIQUE_COLUMNS: dict[str, list[str]] = {
    "concepts": ["serial", "concept_uri"],
    "revisions": ["serial", "revision_uri"],
    "variants": ["serial", "variant_uri"],
    "bindings": ["serial", "binding_uri"],
}

# (child_table, child_column, parent_table, parent_column)
FK_CONSTRAINTS: list[tuple[str, str, str, str]] = [
    ("revisions", "concept_uri", "concepts", "concept_uri"),
    ("revisions", "previous_revision_uri", "revisions", "revision_uri"),
    ("variants", "concept_uri", "concepts", "concept_uri"),
    ("variants", "revision_uri", "revisions", "revision_uri"),
    ("bindings", "variant_uri", "variants", "variant_uri"),
]

VALID_STATUSES = {s.value for s in ElementStatus}

# Required (non-nullable) columns per table — previous_revision_uri is nullable
REQUIRED_COLUMNS: dict[str, list[str]] = {
    "concepts": ["serial", "concept_uri", "current_label", "status"],
    "revisions": ["serial", "concept_uri", "revision_uri", "status"],
    "variants": ["serial", "concept_uri", "variant_uri", "revision_uri", "status"],
    "bindings": ["serial", "variant_uri", "binding_uri", "instance_label", "status"],
}

# ── Exception ─────────────────────────────────────────────────────────────────


class LedgerValidationError(Exception):
    """Raised when a ledger table violates a schema, uniqueness, or referential integrity constraint."""


# ── Base-36 URI serial encoding ───────────────────────────────────────────────

_B36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def b36encode(n: int) -> str:
    """Encode a non-negative integer as a lowercase base-36 string (alphabet 0-9a-z)."""
    if n < 0:
        raise ValueError(f"serial must be non-negative, got {n}")
    if n == 0:
        return "0"
    digits: list[str] = []
    while n:
        digits.append(_B36_ALPHABET[n % 36])
        n //= 36
    return "".join(reversed(digits))


def b36decode(s: str) -> int:
    """Decode a lowercase base-36 string to a non-negative integer."""
    return int(s, 36)


# ── Core functions ────────────────────────────────────────────────────────────


def empty_ledger() -> dict[str, pd.DataFrame]:
    """Return four empty DataFrames with the correct columns for each ledger table."""
    return {name: pd.DataFrame(columns=cols) for name, cols in EXPECTED_COLUMNS.items()}


def validate_ledger(tables: dict[str, pd.DataFrame]) -> None:
    """Validate structural and referential integrity of the ledger tables.

    Raises LedgerValidationError on the first violation found.
    """
    for name in TABLES:
        if name not in tables:
            raise LedgerValidationError(f"Missing table: '{name}'")
        df = tables[name]

        # Expected columns
        expected = set(EXPECTED_COLUMNS[name])
        actual = set(df.columns)
        missing = expected - actual
        extra = actual - expected
        if missing:
            raise LedgerValidationError(f"[{name}] Missing columns: {sorted(missing)}")
        if extra:
            raise LedgerValidationError(f"[{name}] Unexpected columns: {sorted(extra)}")

        if df.empty:
            continue

        # Required (non-null) columns
        for col in REQUIRED_COLUMNS[name]:
            if df[col].isnull().any():
                raise LedgerValidationError(f"[{name}] Column '{col}' contains null values")

        # Serial must be non-negative
        if (df["serial"] < 0).any():
            raise LedgerValidationError(f"[{name}] Column 'serial' contains negative values")

        # Uniqueness constraints (PK: serial; UK: URI column)
        for col in UNIQUE_COLUMNS[name]:
            if df[col].duplicated().any():
                raise LedgerValidationError(f"[{name}] Column '{col}' contains duplicate values")

        # URI suffix must equal b36encode(serial): decode suffix and compare to serial
        uri_col = UNIQUE_COLUMNS[name][1]
        suffixes = df[uri_col].str.rsplit("/", n=1).str[-1]
        try:
            decoded_serials = suffixes.apply(b36decode)
        except ValueError as exc:
            raise LedgerValidationError(
                f"[{name}] Column '{uri_col}' contains a URI with an invalid base-36 suffix: {exc}"
            ) from exc
        mismatch_mask = decoded_serials.values != df["serial"].values
        if mismatch_mask.any():
            bad = df[mismatch_mask][["serial", uri_col]].values.tolist()
            raise LedgerValidationError(f"[{name}] URI suffix does not match base-36 encoding of serial: {bad}")

        # Valid status values
        invalid = set(df["status"].dropna().unique()) - VALID_STATUSES
        if invalid:
            raise LedgerValidationError(f"[{name}] Invalid status values: {sorted(invalid)}")

    # Referential integrity
    for child_table, child_col, parent_table, parent_col in FK_CONSTRAINTS:
        child_df = tables[child_table]
        parent_df = tables[parent_table]
        if child_df.empty:
            continue
        orphans = set(child_df[child_col].dropna()) - set(parent_df[parent_col])
        if orphans:
            raise LedgerValidationError(
                f"[{child_table}.{child_col}] References missing from [{parent_table}.{parent_col}]: {sorted(orphans)}"
            )

    # Cross-concept consistency: each variant's revision must belong to the same concept
    variants_df = tables["variants"]
    revisions_df = tables["revisions"]
    if not variants_df.empty and not revisions_df.empty:
        merged = variants_df[["concept_uri", "revision_uri"]].merge(
            revisions_df[["revision_uri", "concept_uri"]].rename(columns={"concept_uri": "rev_concept_uri"}),
            on="revision_uri",
            how="left",
        )
        mismatch = merged[merged["concept_uri"] != merged["rev_concept_uri"]]
        if not mismatch.empty:
            bad = sorted(mismatch["revision_uri"].dropna().tolist())
            raise LedgerValidationError(
                f"[variants.revision_uri] References a revision belonging to a different concept: {bad}"
            )


def next_serial(table: pd.DataFrame) -> int:
    """Return the next available serial integer for a ledger table."""
    if table.empty or table["serial"].isnull().all():
        return 0
    return int(table["serial"].max()) + 1


def validate_ledger_dir(ledger_dir: Path) -> None:
    """Validate that an existing directory contains exactly the four expected ledger CSV files and nothing else."""
    if not ledger_dir.is_dir():
        raise LedgerValidationError(f"Ledger path is not a directory: {ledger_dir}")
    expected = {f"{name}.csv" for name in TABLES}
    actual = {f.name for f in ledger_dir.iterdir()}
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise LedgerValidationError(f"Ledger directory is missing files: {sorted(missing)}")
    if extra:
        raise LedgerValidationError(f"Ledger directory contains unexpected files: {sorted(extra)}")


def read_ledger(ledger_dir: Path) -> dict[str, pd.DataFrame]:
    """Read the four ledger CSVs from a directory, validating both directory contents and table schemas."""
    validate_ledger_dir(ledger_dir)
    tables: dict[str, pd.DataFrame] = {}
    for name in TABLES:
        tables[name] = pd.read_csv(ledger_dir / f"{name}.csv")
    validate_ledger(tables)
    return tables


def write_ledger(tables: dict[str, pd.DataFrame], ledger_dir: Path) -> None:
    """Write the four ledger DataFrames to CSV files in the given directory."""
    ledger_dir.mkdir(parents=True, exist_ok=True)
    for name in TABLES:
        tables[name].to_csv(ledger_dir / f"{name}.csv", index=False)
