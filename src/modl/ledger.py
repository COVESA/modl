"""Ledger I/O, schema validation, and ID minting for the five ledger CSV tables."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

import pandas as pd

from modl.models import ElementKind, ElementStatus

log = logging.getLogger(__name__)

# ── Schema constants ──────────────────────────────────────────────────────────

TABLES = ("concepts", "revisions", "contracts", "bindings", "revision_aspects", "contract_references")

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
    "bindings": ["serial", "binding_uri", "contract_uri", "instance_tags", "runtime_path", "status"],
    "revision_aspects": ["revision_uri", "aspect_key", "operation", "previous_value", "newer_value", "reason"],
    "contract_references": ["contract_uri", "role", "target_contract_uri"],
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
    "contract_references": ["contract_uri", "role"],
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
    ("contract_references", "contract_uri", "contracts", "contract_uri"),
    ("contract_references", "target_contract_uri", "contracts", "contract_uri"),
]

VALID_STATUSES = {s.value for s in ElementStatus}
VALID_KINDS = {k.value for k in ElementKind}
VALID_REVISION_ASPECT_OPERATIONS = {"added", "modified", "removed"}

# Required (non-nullable) columns per table — previous_revision_uri, parent_uri and instances are nullable
REQUIRED_COLUMNS: dict[str, list[str]] = {
    "concepts": ["serial", "concept_uri", "current_label", "kind", "status"],
    "revisions": ["serial", "concept_uri", "revision_uri", "status"],
    "contracts": ["serial", "concept_uri", "contract_uri", "revision_uri", "status"],
    "bindings": ["serial", "contract_uri", "binding_uri", "instance_tags", "runtime_path", "status"],
    "revision_aspects": ["revision_uri", "aspect_key", "operation"],
    "contract_references": ["contract_uri", "role", "target_contract_uri"],
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
    """Return empty DataFrames with the correct columns for each ledger table."""
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

        # Composite-key tables have no own serial/URI — identity is the composite key.
        if name in COMPOSITE_KEY_COLUMNS:
            key_cols = COMPOSITE_KEY_COLUMNS[name]
            if df.duplicated(subset=key_cols).any():
                bad = df[df.duplicated(subset=key_cols)][key_cols].values.tolist()
                raise LedgerValidationError(f"[{name}] Duplicate {tuple(key_cols)} pairs: {bad}")

            if name == "contract_references":
                if (df["role"].astype(str).str.strip() == "").any():
                    raise LedgerValidationError(f"[{name}] Column 'role' contains empty values")
                continue  # kind rules need the FK-checked tables; see the end of this function

            # revision_aspects: value nullability is governed by 'operation' rather than a
            # single required-columns check.
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

            # Only ENTITY concepts may have instances
            no_instances_kinds = {
                ElementKind.ENUMERATION_SET.value,
                ElementKind.ENUM_VALUE.value,
                ElementKind.PROPERTY.value,
            }
            bad_instances = df[df["kind"].isin(no_instances_kinds) & df["instances"].notna()]
            if not bad_instances.empty:
                bad = sorted(bad_instances["concept_uri"].tolist())
                raise LedgerValidationError(f"[concepts] Only ENTITY concepts may have instances: {bad}")

            # Non-null instances must be a JSON object of dimension -> list of strings
            for _, row in df[df["instances"].notna()].iterrows():
                raw = row["instances"]
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise LedgerValidationError(
                        f"[concepts] Column 'instances' contains invalid JSON at "
                        f"concept_uri '{row['concept_uri']}': {exc}"
                    ) from exc
                if not isinstance(parsed, dict) or not all(
                    isinstance(k, str) and isinstance(v, list) and all(isinstance(i, str) for i in v)
                    for k, v in parsed.items()
                ):
                    raise LedgerValidationError(
                        f"[concepts] Column 'instances' must be a JSON object of dimension -> list of strings at "
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

    # bindings: instance_tags must be a JSON object keyed by existing ENTITY concept_uris; the
    # runtime_path is unique among ACTIVE bindings, and tags are unique per contract among ACTIVE ones.
    if not bindings_df.empty:
        entity_uris = set(concepts_df[concepts_df["kind"] == ElementKind.ENTITY.value]["concept_uri"])
        for _, row in bindings_df.iterrows():
            try:
                tags = json.loads(row["instance_tags"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise LedgerValidationError(
                    f"[bindings] Column 'instance_tags' contains invalid JSON at binding_uri "
                    f"'{row['binding_uri']}': {exc}"
                ) from exc
            if not isinstance(tags, dict) or not all(
                isinstance(dims, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in dims.items())
                for dims in tags.values()
            ):
                raise LedgerValidationError(
                    f"[bindings] Column 'instance_tags' must map entity concept_uri -> {{dimension: instance}} at "
                    f"binding_uri '{row['binding_uri']}'"
                )
            unknown = set(tags) - entity_uris
            if unknown:
                raise LedgerValidationError(
                    f"[bindings] instance_tags of '{row['binding_uri']}' reference non-ENTITY concepts: "
                    f"{sorted(unknown)}"
                )
            if not str(row["runtime_path"]).strip():
                raise LedgerValidationError(f"[bindings] Empty runtime_path at binding_uri '{row['binding_uri']}'")
        active = bindings_df[bindings_df["status"] == ElementStatus.ACTIVE]
        dup_paths = active[active["runtime_path"].duplicated(keep=False)]
        if not dup_paths.empty:
            raise LedgerValidationError(
                f"[bindings] runtime_path must be unique among ACTIVE bindings: "
                f"{sorted(set(dup_paths['runtime_path']))}"
            )
        dup_tags = active[active.duplicated(subset=["contract_uri", "instance_tags"], keep=False)]
        if not dup_tags.empty:
            raise LedgerValidationError(
                f"[bindings] instance_tags must be unique per contract among ACTIVE bindings: "
                f"{sorted(dup_tags['binding_uri'])}"
            )

    # contract_references: the referencing contract must belong to a PROPERTY concept and the
    # referenced contract to an ENTITY or ENUMERATION_SET concept (container namespace).
    refs_df = tables["contract_references"]
    if not refs_df.empty:
        concept_by_contract = tables["contracts"].set_index("contract_uri")["concept_uri"]
        kind_by_concept = tables["concepts"].set_index("concept_uri")["kind"]
        source_kinds = refs_df["contract_uri"].map(concept_by_contract).map(kind_by_concept)
        bad_source = refs_df[source_kinds != ElementKind.PROPERTY.value]
        if not bad_source.empty:
            bad = sorted(bad_source["contract_uri"].tolist())
            raise LedgerValidationError(f"[contract_references] Only PROPERTY contracts may hold references: {bad}")
        container_kinds = {ElementKind.ENTITY.value, ElementKind.ENUMERATION_SET.value}
        target_kinds = refs_df["target_contract_uri"].map(concept_by_contract).map(kind_by_concept)
        bad_target = refs_df[~target_kinds.isin(container_kinds)]
        if not bad_target.empty:
            bad = sorted(bad_target["target_contract_uri"].tolist())
            raise LedgerValidationError(
                f"[contract_references] References must target ENTITY or ENUMERATION_SET contracts: {bad}"
            )


def next_serial(table: pd.DataFrame) -> int:
    """Return the next available serial integer for a ledger table."""
    if table.empty or table["serial"].isnull().all():
        return 0
    return int(table["serial"].max()) + 1


def validate_ledger_dir(ledger_dir: Path) -> None:
    """Validate that an existing directory contains exactly the expected ledger CSV files and nothing else."""
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
    """Read the ledger CSVs from a directory, validating both directory contents and table schemas."""
    validate_ledger_dir(ledger_dir)
    tables: dict[str, pd.DataFrame] = {}
    for name in TABLES:
        tables[name] = pd.read_csv(ledger_dir / f"{name}.csv")
    validate_ledger(tables)
    return tables


def write_ledger(tables: dict[str, pd.DataFrame], ledger_dir: Path) -> None:
    """Write the ledger DataFrames to CSV files in the given directory.

    All files are written to a temporary directory on the same filesystem first,
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


def _binding_export_records(tables: dict[str, pd.DataFrame]) -> list[dict]:
    """Return one record per ACTIVE binding, joined up to its bound property concept.

    Each record has keys: ``concept_uri``, ``current_label``, ``instance_tags`` (keyed by the
    current entity label), ``runtime_path`` and ``binding_uri``.
    """
    bindings_df = tables["bindings"]
    contracts_df = tables["contracts"]
    concepts_df = tables["concepts"]

    active_bindings = bindings_df[bindings_df["status"] == ElementStatus.ACTIVE]
    if active_bindings.empty:
        return []

    merged = active_bindings.merge(contracts_df[["contract_uri", "concept_uri"]], on="contract_uri", how="left").merge(
        concepts_df[["concept_uri", "current_label"]], on="concept_uri", how="left"
    )

    label_by_uri = concepts_df.set_index("concept_uri")["current_label"]

    records: list[dict] = []
    for _, row in merged.iterrows():
        tags = {label_by_uri[uri]: dims for uri, dims in json.loads(row["instance_tags"]).items()}
        records.append(
            {
                "concept_uri": row["concept_uri"],
                "current_label": row["current_label"],
                "instance_tags": tags,
                "runtime_path": row["runtime_path"],
                "binding_uri": row["binding_uri"],
            }
        )
    return records


def canonical_tags(tags: dict[str, dict[str, str]]) -> str:
    """Serialise instance tags to canonical JSON (sorted keys) for storage and comparison."""
    return json.dumps(tags, sort_keys=True, separators=(",", ":"))


def _binding_fields(binding_uri: str, complete: bool, key: str = "binding") -> dict[str, str]:
    """Return the leaf field dict for one binding: always ``key``, plus ``binding_uri`` if complete."""
    fields = {key: binding_uri.rsplit("/", 1)[-1]}
    if complete:
        fields["binding_uri"] = binding_uri
    return fields


def _export_bindings_json(records: list[dict], complete: bool, key: str) -> dict:
    """Group records by ``current_label`` — the abstract concept label from the concepts table.

    Each property maps to a list of its bindings; every item carries the binding fields plus
    ``instance_tags`` (entity label -> dimension -> instance, empty for a singleton) and ``runtime_path``.

    Note: ``current_label`` uniqueness is only guaranteed among siblings sharing the same
    ``parent_uri`` (see the label-uniqueness rules in :func:`validate_ledger`), not globally.
    Adapters that store fully-qualified paths as ``current_label`` (e.g. vspec-style
    ``"Door.IsOpen"``) are safe; adapters that store bare field names could collide here —
    caught below as a hard error rather than silently dropped.
    """
    grouped: dict[str, list[dict]] = {}
    for r in records:
        grouped.setdefault(r["current_label"], []).append(r)

    result: dict = {}
    for label, recs in grouped.items():
        concept_uris = {r["concept_uri"] for r in recs}
        if len(concept_uris) > 1:
            raise LedgerValidationError(
                f"Label collision while exporting: '{label}' is shared by multiple distinct "
                f"property concepts: {sorted(concept_uris)}"
            )

        result[label] = [
            {
                **_binding_fields(r["binding_uri"], complete, key),
                "instance_tags": r["instance_tags"],
                "runtime_path": r["runtime_path"],
            }
            for r in sorted(recs, key=lambda r: r["runtime_path"])
        ]

    return result


def _export_bindings_vspec(records: list[dict], complete: bool, key: str) -> dict:
    """Flatten records into a mapping keyed by each binding's literal ``runtime_path``."""
    result: dict = {}
    for r in records:
        path_key = r["runtime_path"]
        if path_key in result:
            raise LedgerValidationError(
                f"Binding key collision while exporting: '{path_key}' is produced by more than one binding"
            )
        result[path_key] = _binding_fields(r["binding_uri"], complete, key)

    return result


# Registry mapping a --format name to its (shape-builder) function. Adding a new export
# format is a matter of adding one entry here plus a serializer choice in the CLI layer.
_BINDING_EXPORT_BUILDERS: dict[str, Callable[[list[dict], bool, str], dict]] = {
    "json": _export_bindings_json,
    "vspec": _export_bindings_vspec,
}


def export_bindings(
    tables: dict[str, pd.DataFrame], format: str = "json", complete: bool = False, key: str = "binding"
) -> dict:
    """Export ACTIVE bindings as a lookup mapping for downstream tooling (e.g. vss-tools overlays).

    Only bindings with ``status == ACTIVE`` are included — superseded and removed bindings are
    omitted. Each leaf entry always carries ``key`` (the base-36 URI serial suffix, under the
    field name given by ``key``); pass ``complete=True`` to additionally include ``binding_uri``
    (the full binding URI, always under that fixed field name regardless of ``key``).

    ``format`` selects the mapping shape:

    - ``"json"`` (default): keyed by the property's ``current_label`` — the abstract concept
      label. Each property maps to a list of its bindings carrying ``instance_tags`` (entity label ->
      dimension -> instance) and ``runtime_path``. See
      :func:`_export_bindings_json` for a caveat on label uniqueness.
    - ``"vspec"``: a flat mapping keyed by each binding's literal ``runtime_path``.

    Raises LedgerValidationError if two bindings would resolve to the same key/label, since
    that would silently clobber a mapping entry.
    """
    builder = _BINDING_EXPORT_BUILDERS.get(format)
    if builder is None:
        raise ValueError(f"format must be one of {sorted(_BINDING_EXPORT_BUILDERS)}, got {format!r}")

    if not key or not key.strip():
        raise ValueError("key must be a non-empty string")
    if key == "binding_uri":
        raise ValueError("key must not be 'binding_uri' — that field name is reserved for the full binding URI")

    records = _binding_export_records(tables)
    if not records:
        return {}

    return builder(records, complete, key)
