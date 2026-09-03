"""Ledger I/O, schema validation, and ID minting for the five ledger CSV tables."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from modl.models import ElementKind, ElementStatus

# ── Schema constants ──────────────────────────────────────────────────────────

TABLES = ("concepts", "revisions", "contracts", "bindings", "revision_aspects")

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
    "revision_aspects": ["revision_uri", "aspect_key", "operation", "previous_value", "newer_value"],
}

UNIQUE_COLUMNS: dict[str, list[str]] = {
    "concepts": ["serial", "concept_uri"],
    "revisions": ["serial", "revision_uri"],
    "contracts": ["serial", "contract_uri"],
    "bindings": ["serial", "binding_uri"],
}

# Tables with no own serial/URI — identity is the composite key below instead.
COMPOSITE_KEY_COLUMNS: dict[str, list[str]] = {
    "revision_aspects": ["revision_uri", "aspect_key"],
}

# (child_table, child_column, parent_table, parent_column)
FK_CONSTRAINTS: list[tuple[str, str, str, str]] = [
    ("concepts", "parent_uri", "concepts", "concept_uri"),
    ("revisions", "concept_uri", "concepts", "concept_uri"),
    ("revisions", "previous_revision_uri", "revisions", "revision_uri"),
    ("contracts", "concept_uri", "concepts", "concept_uri"),
    ("contracts", "revision_uri", "revisions", "revision_uri"),
    ("bindings", "contract_uri", "contracts", "contract_uri"),
    ("revision_aspects", "revision_uri", "revisions", "revision_uri"),
]

VALID_STATUSES = {s.value for s in ElementStatus}
VALID_KINDS = {k.value for k in ElementKind}
VALID_REVISION_ASPECT_OPERATIONS = {"added", "modified", "removed"}

# Required (non-nullable) columns per table — previous_revision_uri and instance_label are nullable
REQUIRED_COLUMNS: dict[str, list[str]] = {
    "concepts": ["serial", "concept_uri", "current_label", "kind", "status"],
    "revisions": ["serial", "concept_uri", "revision_uri", "status"],
    "contracts": ["serial", "concept_uri", "contract_uri", "revision_uri", "status"],
    "bindings": ["serial", "contract_uri", "binding_uri", "status"],
    "revision_aspects": ["revision_uri", "aspect_key", "operation"],
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
    """Return five empty DataFrames with the correct columns for each ledger table."""
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

        # revision_aspects has no own serial/URI — identity is the composite key, and value
        # nullability is governed by 'operation' rather than a single required-columns check.
        if name in COMPOSITE_KEY_COLUMNS:
            key_cols = COMPOSITE_KEY_COLUMNS[name]
            if df.duplicated(subset=key_cols).any():
                bad = df[df.duplicated(subset=key_cols)][key_cols].values.tolist()
                raise LedgerValidationError(f"[{name}] Duplicate {tuple(key_cols)} pairs: {bad}")

            invalid_ops = set(df["operation"].dropna().unique()) - VALID_REVISION_ASPECT_OPERATIONS
            if invalid_ops:
                raise LedgerValidationError(f"[{name}] Invalid operation values: {sorted(invalid_ops)}")

            bad_modified = df[
                (df["operation"] == "modified") & (df["previous_value"].isnull() | df["newer_value"].isnull())
            ]
            if not bad_modified.empty:
                bad = bad_modified[key_cols].values.tolist()
                raise LedgerValidationError(
                    f"[{name}] operation='modified' rows must have non-null previous_value and newer_value: {bad}"
                )

            bad_added = df[(df["operation"] == "added") & df["previous_value"].notna()]
            if not bad_added.empty:
                bad = bad_added[key_cols].values.tolist()
                raise LedgerValidationError(f"[{name}] operation='added' rows must have null previous_value: {bad}")

            bad_removed = df[(df["operation"] == "removed") & df["newer_value"].notna()]
            if not bad_removed.empty:
                bad = bad_removed[key_cols].values.tolist()
                raise LedgerValidationError(f"[{name}] operation='removed' rows must have null newer_value: {bad}")

            continue  # no serial/URI/status columns to validate below

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

            # current_label uniqueness is scoped to two independent namespaces, mirroring GraphQL
            # SDL: named-type names (ENTITY + ENUMERATION_SET) are globally unique against each
            # other, while field/enum-value names (PROPERTY + ENUM_VALUE) are unique only among
            # siblings sharing the same parent_uri — never compared against the container namespace.
            container_kinds = {ElementKind.ENTITY.value, ElementKind.ENUMERATION_SET.value}
            member_kinds = {ElementKind.PROPERTY.value, ElementKind.ENUM_VALUE.value}

            # Group A: ENTITY + ENUMERATION_SET — current_label globally unique across the group
            container_df = df[df["kind"].isin(container_kinds)]
            dup_mask = container_df["current_label"].duplicated(keep=False)
            if dup_mask.any():
                msgs: list[str] = []
                for label, group in container_df[dup_mask].groupby("current_label"):
                    details = ", ".join(
                        f"concept_uri='{row['concept_uri']}' kind={row['kind']} parent_uri={row['parent_uri']!r}"
                        for _, row in group.iterrows()
                    )
                    msgs.append(f"  '{label}': {details}")
                raise LedgerValidationError(
                    "[concepts] Duplicate current_label values among ENTITY/ENUMERATION_SET concepts:\n"
                    + "\n".join(msgs)
                )

            # Group B: PROPERTY + ENUM_VALUE — current_label unique only within (parent_uri, current_label)
            member_df = df[df["kind"].isin(member_kinds)]
            dup_mask = member_df.duplicated(subset=["parent_uri", "current_label"], keep=False)
            if dup_mask.any():
                msgs = []
                for (_parent_uri, label), group in member_df[dup_mask].groupby(["parent_uri", "current_label"]):
                    details = ", ".join(
                        f"concept_uri='{row['concept_uri']}' kind={row['kind']} parent_uri={row['parent_uri']!r}"
                        for _, row in group.iterrows()
                    )
                    msgs.append(f"  '{label}': {details}")
                raise LedgerValidationError(
                    "[concepts] Duplicate current_label values among sibling PROPERTY/ENUM_VALUE concepts "
                    "(same parent_uri):\n" + "\n".join(msgs)
                )

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

    # Parent-kind consistency: a PROPERTY's parent_uri must resolve to an ENTITY concept, and an
    # ENUM_VALUE's parent_uri must resolve to an ENUMERATION_SET concept. This is the safety net
    # that makes label uniqueness safe to scope by parent_uri (Group B above) rather than globally:
    # without it, a PROPERTY could silently attach to a same-named non-ENTITY concept.
    concepts_df = tables["concepts"]
    if not concepts_df.empty:
        kind_by_uri = concepts_df.set_index("concept_uri")["kind"]
        expected_parent_kind = {
            ElementKind.PROPERTY.value: ElementKind.ENTITY.value,
            ElementKind.ENUM_VALUE.value: ElementKind.ENUMERATION_SET.value,
        }
        for child_kind, expected_kind in expected_parent_kind.items():
            children = concepts_df[(concepts_df["kind"] == child_kind) & concepts_df["parent_uri"].notna()]
            if children.empty:
                continue
            actual_parent_kinds = children["parent_uri"].map(kind_by_uri)
            bad = children[actual_parent_kinds != expected_kind]
            if not bad.empty:
                details = sorted(
                    f"concept_uri='{row['concept_uri']}' parent_uri='{row['parent_uri']}' "
                    f"(parent kind={kind_by_uri.get(row['parent_uri'], 'MISSING')!r})"
                    for _, row in bad.iterrows()
                )
                raise LedgerValidationError(
                    f"[concepts] {child_kind} concepts must have a parent_uri resolving to "
                    f"an {expected_kind} concept: {details}"
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
    """Validate that an existing directory contains exactly the five expected ledger CSV files and nothing else."""
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
    """Read the five ledger CSVs from a directory, validating both directory contents and table schemas."""
    validate_ledger_dir(ledger_dir)
    tables: dict[str, pd.DataFrame] = {}
    for name in TABLES:
        tables[name] = pd.read_csv(ledger_dir / f"{name}.csv")
    validate_ledger(tables)
    return tables


def write_ledger(tables: dict[str, pd.DataFrame], ledger_dir: Path) -> None:
    """Write the five ledger DataFrames to CSV files in the given directory.

    All five files are written to a temporary directory on the same filesystem first,
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


def validate_model_labels(
    elements: Sequence[tuple[str, str, str | None]],
    ledger_dir: Path,
) -> None:
    """Check that ``elements`` exactly matches the active concepts in the ledger at ``ledger_dir``.

    Reads and fully validates the four CSV files from ``ledger_dir`` before checking.
    Each element is a ``(label, kind, parent_label)`` triple drawn from the composed model.
    ``parent_label`` must be ``None`` for ``ENTITY``/``ENUMERATION_SET`` elements (globally unique
    labels) and the label of the immediate parent for ``PROPERTY``/``ENUM_VALUE`` elements (labels
    scoped to their parent) — see the two label namespaces documented on
    :class:`~modl.models.ElementKind`.

    Raises LedgerValidationError if:

    - ``elements`` contains a duplicate ``(label, parent_label)`` pair (indicates a corrupt or
      mismatched snapshot).
    - Any element's ``parent_label`` does not resolve to an active ledger concept.
    - Any element is absent from the active ledger concepts, or vice versa.
    - Any ``kind`` does not match the ledger record for that ``(label, parent_label)`` pair.
    """
    tables = read_ledger(ledger_dir)
    concepts = tables["concepts"]
    active = concepts[concepts["status"] == ElementStatus.ACTIVE]

    # --- 1. Reject duplicate (label, parent_label) pairs in input ---
    seen: set[tuple[str, str | None]] = set()
    dupes: list[str] = []
    for label, _, parent_label in elements:
        key = (label, parent_label)
        if key in seen:
            dupes.append(label)
        seen.add(key)
    if dupes:
        raise LedgerValidationError(f"Duplicate labels in input (expected unique model elements): {sorted(set(dupes))}")

    # --- 2. Resolve each input parent_label to a parent_uri, so entries can be compared
    # against the ledger's parent_uri-keyed concepts. ---
    label_to_uri: dict[str, str] = dict(zip(active["current_label"], active["concept_uri"], strict=True))

    input_index: dict[tuple[str, str | None], str] = {}
    unresolved_parents: list[str] = []
    for label, kind, parent_label in elements:
        if parent_label is None:
            input_index[(label, None)] = kind
            continue
        parent_uri = label_to_uri.get(parent_label)
        if parent_uri is None:
            unresolved_parents.append(f"'{label}' (parent_label='{parent_label}')")
            continue
        input_index[(label, parent_uri)] = kind
    if unresolved_parents:
        raise LedgerValidationError(
            "Elements reference a parent_label not found among active ledger concepts: "
            + ", ".join(sorted(unresolved_parents))
        )

    # --- 3. One-to-one census, scoped by (label, parent_uri) ---
    active_index: dict[tuple[str, str | None], str] = {}
    for _, row in active.iterrows():
        parent_uri = row["parent_uri"]
        parent_key = None if pd.isna(parent_uri) else parent_uri
        active_index[(row["current_label"], parent_key)] = row["kind"]

    input_keys = set(input_index)
    active_keys = set(active_index)
    only_in_input = input_keys - active_keys
    only_in_ledger = active_keys - input_keys

    if only_in_input or only_in_ledger:
        parts: list[str] = []
        if only_in_input:
            parts.append(f"labels not in ledger: {sorted(label for label, _ in only_in_input)}")
        if only_in_ledger:
            parts.append(f"active ledger labels not in input: {sorted(label for label, _ in only_in_ledger)}")
        raise LedgerValidationError("Model/ledger label mismatch — " + "; ".join(parts))

    # --- 4. Kind attestation for every matched (label, parent_uri) ---
    mismatches: list[str] = []
    for key, kind in input_index.items():
        label, _ = key
        ledger_kind = active_index[key]
        if kind != ledger_kind:
            mismatches.append(f"'{label}': kind {kind!r} != ledger {ledger_kind!r}")
    if mismatches:
        raise LedgerValidationError("Model/ledger kind mismatch:\n" + "\n".join(f"  {m}" for m in mismatches))
