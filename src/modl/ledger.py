"""Ledger I/O, schema validation, and ID minting for the four ledger CSV tables."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pandas as pd

from modl.models import ElementKind, ElementStatus

# ── Schema constants ──────────────────────────────────────────────────────────

TABLES = ("concepts", "revisions", "contracts", "bindings")

EXPECTED_COLUMNS: dict[str, list[str]] = {
    "concepts": [
        "serial",
        "concept_uri",
        "current_label",
        "previous_labels",
        "kind",
        "status",
        "parent_uri",
        "instances",
    ],
    "revisions": ["serial", "revision_uri", "concept_uri", "previous_revision_uri", "status"],
    "contracts": ["serial", "contract_uri", "concept_uri", "revision_uri", "status"],
    "bindings": ["serial", "binding_uri", "contract_uri", "instance_label", "status"],
}

UNIQUE_COLUMNS: dict[str, list[str]] = {
    "concepts": ["serial", "concept_uri"],
    "revisions": ["serial", "revision_uri"],
    "contracts": ["serial", "contract_uri"],
    "bindings": ["serial", "binding_uri"],
}

# (child_table, child_column, parent_table, parent_column)
FK_CONSTRAINTS: list[tuple[str, str, str, str]] = [
    ("concepts", "parent_uri", "concepts", "concept_uri"),
    ("revisions", "concept_uri", "concepts", "concept_uri"),
    ("revisions", "previous_revision_uri", "revisions", "revision_uri"),
    ("contracts", "concept_uri", "concepts", "concept_uri"),
    ("contracts", "revision_uri", "revisions", "revision_uri"),
    ("bindings", "contract_uri", "contracts", "contract_uri"),
]

VALID_STATUSES = {s.value for s in ElementStatus}
VALID_KINDS = {k.value for k in ElementKind}

# Required (non-nullable) columns per table — previous_revision_uri and instance_label are nullable
REQUIRED_COLUMNS: dict[str, list[str]] = {
    "concepts": ["serial", "concept_uri", "current_label", "kind", "status"],
    "revisions": ["serial", "concept_uri", "revision_uri", "status"],
    "contracts": ["serial", "concept_uri", "contract_uri", "revision_uri", "status"],
    "bindings": ["serial", "contract_uri", "binding_uri", "status"],
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

        # Valid kind values and label uniqueness (concepts table only)
        if name == "concepts":
            invalid_kinds = set(df["kind"].dropna().unique()) - VALID_KINDS
            if invalid_kinds:
                raise LedgerValidationError(f"[{name}] Invalid kind values: {sorted(invalid_kinds)}")

            # current_label must be globally unique across all concepts
            dup_mask = df["current_label"].duplicated(keep=False)
            if dup_mask.any():
                msgs: list[str] = []
                for label, group in df[dup_mask].groupby("current_label"):
                    details = ", ".join(
                        f"concept_uri='{row['concept_uri']}' kind={row['kind']} parent_uri={row['parent_uri']!r}"
                        for _, row in group.iterrows()
                    )
                    msgs.append(f"  '{label}': {details}")
                raise LedgerValidationError("[concepts] Duplicate current_label values:\n" + "\n".join(msgs))

            # ENTITY and ENUMERATION_SET must not have a parent_uri
            no_parent_kinds = {ElementKind.ENTITY.value, ElementKind.ENUMERATION_SET.value}
            bad_parent = df[df["kind"].isin(no_parent_kinds) & df["parent_uri"].notna()]
            if not bad_parent.empty:
                bad = sorted(bad_parent["concept_uri"].tolist())
                raise LedgerValidationError(
                    f"[concepts] ENTITY and ENUMERATION_SET concepts must have null parent_uri: {bad}"
                )

            # ENUMERATION_SET and ENUM_VALUE must not have instances
            no_instances_kinds = {ElementKind.ENUMERATION_SET.value, ElementKind.ENUM_VALUE.value}
            bad_instances = df[df["kind"].isin(no_instances_kinds) & df["instances"].notna()]
            if not bad_instances.empty:
                bad = sorted(bad_instances["concept_uri"].tolist())
                raise LedgerValidationError(
                    f"[concepts] ENUMERATION_SET and ENUM_VALUE concepts must have null instances: {bad}"
                )

            # Non-null instances must be a valid JSON array of strings
            for _, row in df[df["instances"].notna()].iterrows():
                raw = row["instances"]
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise LedgerValidationError(
                        f"[concepts] Column 'instances' contains invalid JSON at"
                        f"concept_uri '{row['concept_uri']}': {exc}"
                    ) from exc
                if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
                    raise LedgerValidationError(
                        f"[concepts] Column 'instances' must be a JSON array of strings at"
                        f"concept_uri '{row['concept_uri']}'. Got: {raw}"
                    )

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

    # Cross-concept consistency: each contract's revision must belong to the same concept
    contracts_df = tables["contracts"]
    revisions_df = tables["revisions"]
    if not contracts_df.empty and not revisions_df.empty:
        merged = contracts_df[["concept_uri", "revision_uri"]].merge(
            revisions_df[["revision_uri", "concept_uri"]].rename(columns={"concept_uri": "rev_concept_uri"}),
            on="revision_uri",
            how="left",
        )
        mismatch = merged[merged["concept_uri"] != merged["rev_concept_uri"]]
        if not mismatch.empty:
            bad = sorted(mismatch["revision_uri"].dropna().tolist())
            raise LedgerValidationError(
                f"[contracts.revision_uri] References a revision belonging to a different concept: {bad}"
            )

    # Only PROPERTY concepts may have bindings; ENTITY, ENUMERATION_SET, and ENUM_VALUE must not
    non_binding_kinds = {ElementKind.ENTITY.value, ElementKind.ENUMERATION_SET.value, ElementKind.ENUM_VALUE.value}
    concepts_df = tables["concepts"]
    bindings_df = tables["bindings"]
    if not bindings_df.empty and not concepts_df.empty:
        non_binding_uris = set(concepts_df[concepts_df["kind"].isin(non_binding_kinds)]["concept_uri"])
        if non_binding_uris:
            non_binding_contract_uris = set(
                tables["contracts"][tables["contracts"]["concept_uri"].isin(non_binding_uris)]["contract_uri"]
            )
            if non_binding_contract_uris:
                bad_bindings = bindings_df[bindings_df["contract_uri"].isin(non_binding_contract_uris)]
                if not bad_bindings.empty:
                    bad = sorted(bad_bindings["binding_uri"].tolist())
                    raise LedgerValidationError(
                        f"[bindings] Only PROPERTY concepts may have bindings; "
                        f"ENTITY, ENUMERATION_SET, and ENUM_VALUE must not: {bad}"
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
    """Write the four ledger DataFrames to CSV files in the given directory.

    All four files are written to a temporary directory on the same filesystem first,
    then atomically renamed into place.  A crash or disk-full error during writing
    leaves any pre-existing ledger intact.
    """
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ledger_dir.parent, prefix=".modl-tmp-") as tmp:
        tmp_path = Path(tmp)
        for name in TABLES:
            tables[name].to_csv(tmp_path / f"{name}.csv", index=False)
        for name in TABLES:
            os.replace(tmp_path / f"{name}.csv", ledger_dir / f"{name}.csv")
