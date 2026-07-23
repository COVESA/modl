"""Compatibility analysis engine for the ``modl adapt`` command.

Compares two release ledger snapshots against a direct diff report and produces
a runtime-agnostic compatibility report, Markdown adapter recipes, and a YAML
adaptation plan.

The public entry point is :func:`analyze`.  All other functions in this module
are pure (no I/O) and are therefore directly testable without fixtures.

Architecture
------------
::

    newer_tables   \
    older_tables    |---> analyze() ---> CompatibilityReport
    diff_report      |
    cfg              |         |---> report_to_json()
    adapt_cfg       /          |---> report_to_markdown()
    newer/older release         `--> report_to_adaptation_plan()

Three-level separation
-----------------------
Each adaptation rule in the plan is built from three explicit levels:

- **Level 1 — change**: observed facts from the diff (automatic; recorded in the
  ``change`` section of each plan rule: ``kind`` and, for aspect changes, ``aspects``).
- **Level 2 — adaptation**: which transformation class addresses this change
  (the ``adaptation.kind`` block inside each step, declared in the adaptation config).
- **Level 3 — recipe**: execution parameters for the transformation (the ``recipe``
  block inside each step).  May be absent (``status: incomplete``) when the user
  has declared the strategy but not yet filled in value-specific parameters.

Terminology
-----------
- *newer release*: the producer release whose data we have (e.g. v11).
- *older release*: the consumer release whose contract must be satisfied (e.g. v8).
- *diff*: a :class:`~modl.ir.DiffReport` describing changes **from** the older release **to** the newer release.
  ADDED events mean the element is new in the newer release (absent from the older release).
  REMOVED events mean the element existed in the older release but is gone from the newer release.

Change kinds
-------------
``change_kind`` values are derived from event structure:

- ``field_renamed``    — ``renamed_from`` set on the event
- ``field_added``      — ADDED event (property / enum value)
- ``field_removed``    — REMOVED event (property / enum value)
- ``entity_added``     — ADDED event (entity / enumeration set)
- ``entity_removed``   — REMOVED event (entity / enumeration set)
- ``aspect_changed``   — MODIFIED event with aspect changes but no rename

Step kinds (Level 2)
---------------------
Steps are structured dicts with ``adaptation`` and ``recipe`` sub-blocks.
``recipe.source_value`` / ``recipe.target_value`` are direction-relative:
in ``newer_to_older`` mode source = newer value; in ``older_to_newer`` mode source = older value.
``changed_aspects`` always records ``newer_value`` / ``older_value`` as direction-neutral facts.

- ``rename``   — rename/move a field path (structural; auto-emitted for renames)
- ``scale``    — multiply or divide a numeric value; recipe: list of ``{source, target, factor}`` rows
- ``cast``     — coerce from one type to another; recipe: optional passthrough dict
- ``round``    — apply a rounding policy; recipe: ``{policy: floor|ceil|round|trunc}``
- ``lookup``   — map a discrete value; recipe: list of ``{source, target}`` rows
- ``default``  — inject a constant; recipe: ``{default_value: <value>}``
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import yaml

from modl.config import _RECIPE_REQUIRED_KINDS, AdaptationConfig, AdaptationStep, BreakingChangeConfig, StepKind
from modl.ir import ChangeType, DiffReport, EntityChanged, PropertyChanged, extract_op_full
from modl.models import ElementKind

# ── Controlled vocabularies ───────────────────────────────────────────────────


class CompatibilityCategory(StrEnum):
    """Consumer-facing compatibility classification for a single contract change."""

    PROJECTION_COMPATIBLE = "projection_compatible"
    DETERMINISTIC_TRANSFORM = "deterministic_transform"
    ADAPTATION_STRATEGY = "adaptation_strategy"
    POLICY_REQUIRED = "policy_required"
    MANUAL_MAPPING_REQUIRED = "manual_mapping_required"
    UNSUPPORTED = "unsupported"
    NON_BREAKING = "non_breaking"


class Lossiness(StrEnum):
    """Data loss characterisation for an adaptation step pipeline."""

    NONE = "none"
    POSSIBLE = "possible"
    GUARANTEED = "guaranteed"


class AdaptDirection(StrEnum):
    """Perspective of the adaptation analysis.

    ``newer_to_older`` — reading: transform platform (newer-release) data so that consumers
    still on an older-release contract can understand it.  ADDED fields are non-breaking;
    REMOVED fields are breaking.

    ``older_to_newer`` — writing: transform older-release client data so that the platform
    (newer-release) contract is satisfied.  ADDED fields are breaking (platform expects them);
    REMOVED fields are non-breaking (platform ignores extra fields).
    """

    NEWER_TO_OLDER = "newer_to_older"
    OLDER_TO_NEWER = "older_to_newer"


# ── Report data models ────────────────────────────────────────────────────────


@dataclass
class ContractRepresentation:
    """Attribute snapshot of a contract in a specific release."""

    release: str
    label: str
    aspects: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompatibilityEntry:
    """Compatibility classification for a single changed model element."""

    change_id: str
    concept_uri: str | None
    change_kind: str
    consumer_impact: str  # "breaking" | "non_breaking"
    category: CompatibilityCategory
    adapter_candidate: bool
    lossiness: Lossiness
    newer_representation: ContractRepresentation
    older_representation: ContractRepresentation
    steps: list[dict[str, Any]] = field(default_factory=list)
    changed_aspects: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""


@dataclass
class ReportSummary:
    """Aggregated counts across all compatibility entries."""

    total: int = 0
    projection_compatible: int = 0
    deterministic_transform: int = 0
    adaptation_strategy: int = 0
    policy_required: int = 0
    manual_mapping_required: int = 0
    unsupported: int = 0
    non_breaking: int = 0
    adapter_candidates: int = 0


@dataclass
class CompatibilityReport:
    """Full compatibility report produced by :func:`analyze`."""

    report_id: str
    newer_release: str
    older_release: str
    direction: AdaptDirection
    entries: list[CompatibilityEntry] = field(default_factory=list)
    summary: ReportSummary = field(default_factory=ReportSummary)


# ── Core analysis engine ──────────────────────────────────────────────────────


def analyze(
    diff_report: DiffReport,
    cfg: BreakingChangeConfig,
    adapt_cfg: AdaptationConfig,
    newer_release: str,
    older_release: str,
    *,
    direction: AdaptDirection = AdaptDirection.NEWER_TO_OLDER,
    newer_tables: dict | None = None,
    older_tables: dict | None = None,
) -> CompatibilityReport:
    """Analyse contract compatibility between two release ledger snapshots.

    *diff_report* describes changes **from** *older_release* **to** *newer_release*:
    ADDED events are new in the newer release; REMOVED events were present in the older release only.

    *direction* controls the adaptation perspective:

    - ``newer_to_older`` — reading: transform platform (newer) values to satisfy an older-release
      consumer.  ADDED fields are non-breaking; REMOVED fields are breaking.
    - ``older_to_newer`` — writing: transform older-release client values to satisfy the platform
      (newer) contract.  ADDED fields are breaking; REMOVED fields are non-breaking.

    *newer_tables* and *older_tables* are optional ledger snapshots used to resolve stable concept
    URIs.  When omitted, ``concept_uri`` is ``None`` in all entries.

    Returns a :class:`CompatibilityReport` with one :class:`CompatibilityEntry` per changed element.
    Never mutates any ledger table.
    """
    if direction == AdaptDirection.NEWER_TO_OLDER:
        report_id = f"compat-{newer_release}-to-{older_release}"
    else:
        report_id = f"compat-{older_release}-to-{newer_release}"

    report = CompatibilityReport(
        report_id=report_id,
        newer_release=newer_release,
        older_release=older_release,
        direction=direction,
    )

    for change_counter, event in enumerate(diff_report.changes, start=1):
        change_id = f"change-{change_counter:04d}"
        entry = _process_event(
            event, change_id, newer_tables, older_tables, cfg, adapt_cfg, newer_release, older_release, direction
        )
        report.entries.append(entry)

    report.summary = _compute_summary(report.entries)
    return report


def _process_event(
    event: EntityChanged | PropertyChanged,
    change_id: str,
    newer_tables: dict | None,
    older_tables: dict | None,
    cfg: BreakingChangeConfig,
    adapt_cfg: AdaptationConfig,
    newer_release: str,
    older_release: str,
    direction: AdaptDirection,
) -> CompatibilityEntry:
    """Produce a :class:`CompatibilityEntry` for a single diff event."""
    # Resolve concept URI from the newer ledger (preferred) then older ledger
    lookup_label = event.renamed_from if event.renamed_from is not None else event.label
    concept_uri = _find_concept_uri(newer_tables, lookup_label) or _find_concept_uri(older_tables, lookup_label)

    # Determine element kind for config lookups
    kind = event.kind

    # Build aspect snapshots (direction-neutral facts)
    newer_aspects = _extract_newer_aspects(event)
    older_aspects = _extract_older_aspects(event)

    newer_repr = ContractRepresentation(release=newer_release, label=event.label, aspects=newer_aspects)
    older_repr = ContractRepresentation(
        release=older_release,
        label=event.renamed_from if event.renamed_from is not None else event.label,
        aspects=older_aspects,
    )

    # Consumer impact depends on direction.
    # newer_to_older: REMOVED=breaking (old client expected it), ADDED=non_breaking (ignored).
    # older_to_newer: ADDED=breaking (platform expects it, old client can't provide it), REMOVED=non_breaking.
    from modl.ir import _aspect_ops_for_event

    is_added = event.change_type == ChangeType.ADDED
    is_removed = event.change_type == ChangeType.REMOVED
    is_modified = event.change_type == ChangeType.MODIFIED

    if is_modified:
        # Rename is always breaking in both directions — consumers reference the field by name.
        if event.renamed_from is not None:
            consumer_impact = "breaking"
        else:
            aspect_ops = _aspect_ops_for_event(event)
            is_brk = cfg.is_breaking(kind, aspect_ops, renamed_from=None)
            consumer_impact = "breaking" if is_brk else "non_breaking"
    elif direction == AdaptDirection.NEWER_TO_OLDER:
        consumer_impact = "breaking" if is_removed else "non_breaking"
    else:  # older_to_newer
        consumer_impact = "breaking" if is_added else "non_breaking"

    # Determine change_kind, steps, changed_aspects, category, lossiness
    change_kind, steps, changed_aspects, category, lossiness = _classify(
        event, kind, adapt_cfg, newer_aspects, older_aspects, concept_uri is not None, direction
    )

    # Adapter candidate: only if breaking + category is actionable
    adapter_candidate = consumer_impact == "breaking" and category not in (
        CompatibilityCategory.UNSUPPORTED,
        CompatibilityCategory.NON_BREAKING,
        CompatibilityCategory.MANUAL_MAPPING_REQUIRED,
    )

    rationale = _rationale(change_kind, category, event, direction)

    return CompatibilityEntry(
        change_id=change_id,
        concept_uri=concept_uri,
        change_kind=change_kind,
        consumer_impact=consumer_impact,
        category=category,
        adapter_candidate=adapter_candidate,
        lossiness=lossiness,
        newer_representation=newer_repr,
        older_representation=older_repr,
        steps=steps,
        changed_aspects=changed_aspects,
        rationale=rationale,
    )


def _classify(
    event: EntityChanged | PropertyChanged,
    kind: ElementKind,
    adapt_cfg: AdaptationConfig,
    newer_aspects: dict[str, Any],
    older_aspects: dict[str, Any],
    concept_known: bool,
    direction: AdaptDirection,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], CompatibilityCategory, Lossiness]:
    """Return ``(change_kind, steps, changed_aspects, category, lossiness)`` for an event.

    ``steps`` are structured dicts with ``adaptation`` (kind) and ``recipe`` (params) sub-blocks.
    ``recipe.source_value`` / ``recipe.target_value`` are direction-relative:
    - ``newer_to_older``: source = newer value (what you have), target = older value (what you produce).
    - ``older_to_newer``: source = older value (what you have), target = newer value (what you produce).
    ``changed_aspects`` always records ``newer_value`` / ``older_value`` as direction-neutral facts.
    ``concept_known`` is kept for signature compatibility but no longer affects REMOVED logic.
    """
    ct = event.change_type
    changed_aspects: list[dict[str, Any]] = []

    # ── ADDED ─────────────────────────────────────────────────────────────────
    if ct == ChangeType.ADDED:
        change_kind = "entity_added" if isinstance(event, EntityChanged) else "field_added"
        if direction == AdaptDirection.NEWER_TO_OLDER:
            # New field in platform — older consumers ignore extra fields.
            return change_kind, [], changed_aspects, CompatibilityCategory.NON_BREAKING, Lossiness.NONE
        else:
            # New field in platform — older clients cannot provide it.
            return change_kind, [], changed_aspects, CompatibilityCategory.MANUAL_MAPPING_REQUIRED, Lossiness.NONE

    # ── REMOVED ───────────────────────────────────────────────────────────────
    if ct == ChangeType.REMOVED:
        change_kind = "entity_removed" if isinstance(event, EntityChanged) else "field_removed"
        if direction == AdaptDirection.OLDER_TO_NEWER:
            # Old client may send this field; platform ignores extra fields.
            return change_kind, [], changed_aspects, CompatibilityCategory.NON_BREAKING, Lossiness.NONE
        # newer_to_older: field existed in older, gone from newer.
        # No auto-emission of default steps — always UNSUPPORTED.
        # Users who need default injection must configure it explicitly in the adaptation config.
        return change_kind, [], changed_aspects, CompatibilityCategory.UNSUPPORTED, Lossiness.GUARANTEED

    # ── MODIFIED ──────────────────────────────────────────────────────────────
    # Step path orientation is direction-relative.
    if direction == AdaptDirection.NEWER_TO_OLDER:
        source_path, target_path = event.label, event.renamed_from
    else:
        source_path, target_path = event.renamed_from, event.label

    # Rename only — no aspect changes
    if event.renamed_from is not None and not newer_aspects:
        rename_step: dict[str, Any] = {
            "adaptation": {"kind": "rename"},
            "recipe": {"status": "complete", "source_path": source_path, "target_path": target_path},
        }
        return (
            "field_renamed",
            [rename_step],
            changed_aspects,
            CompatibilityCategory.PROJECTION_COMPATIBLE,
            Lossiness.NONE,
        )

    steps: list[dict[str, Any]] = []
    if event.renamed_from is not None:
        steps.append(
            {
                "adaptation": {"kind": "rename"},
                "recipe": {"status": "complete", "source_path": source_path, "target_path": target_path},
            }
        )

    change_kind = "field_renamed" if event.renamed_from else "aspect_changed"
    category: CompatibilityCategory = CompatibilityCategory.NON_BREAKING
    lossiness: Lossiness = Lossiness.NONE

    for aspect_key, new_val in newer_aspects.items():
        prev_val = older_aspects.get(aspect_key)
        # changed_aspects are direction-neutral facts.
        changed_aspects.append({"key": aspect_key, "newer_value": new_val, "older_value": prev_val})

        # source/target values depend on direction.
        if direction == AdaptDirection.NEWER_TO_OLDER:
            source_val, target_val = new_val, prev_val
        else:
            source_val, target_val = prev_val, new_val

        # Build the dotted key (e.g. "unit.modified") for config lookup.
        raw_val = event.aspects.get(aspect_key, {})
        op, _, _ = extract_op_full(raw_val)
        lookup_key = f"{aspect_key}.{op}" if op else aspect_key

        step_specs = adapt_cfg.steps_for(kind, lookup_key)
        if step_specs:
            for spec in step_specs:
                recipe_dict = _resolve_recipe(spec, source_val, target_val)
                steps.append({"adaptation": {"kind": spec.adaptation.kind.value}, "recipe": recipe_dict})
                if spec.adaptation.kind == StepKind.ROUND:
                    lossiness = Lossiness.POSSIBLE
                    category = _higher_category(category, CompatibilityCategory.POLICY_REQUIRED)
                elif recipe_dict["status"] == "incomplete":
                    category = _higher_category(category, CompatibilityCategory.ADAPTATION_STRATEGY)
                else:
                    category = _higher_category(category, CompatibilityCategory.DETERMINISTIC_TRANSFORM)
        else:
            category = _higher_category(category, CompatibilityCategory.MANUAL_MAPPING_REQUIRED)

    if event.renamed_from is not None and category == CompatibilityCategory.NON_BREAKING:
        category = CompatibilityCategory.PROJECTION_COMPATIBLE

    return change_kind, steps, changed_aspects, category, lossiness


# ── Recipe resolver ───────────────────────────────────────────────────────────


def _resolve_recipe(spec: AdaptationStep, source_val: Any, target_val: Any) -> dict[str, Any]:
    """Resolve Level-3 recipe parameters for a step against the actual diff values.

    For kinds that never need recipe params (``rename``, ``cast``, ``nest``, ``extract``,
    ``map``), any provided recipe dict is passed through and the result is always
    ``status: complete``.

    For recipe-required kinds (``scale``, ``lookup``, ``round``, ``default``):

    - If no recipe was declared → ``status: incomplete``.
    - If recipe is a dict (unconditional params, e.g. ``round``) → ``status: complete``.
    - If recipe is a list of value-conditional rows → look for a row matching
      ``source == source_val`` and ``target == target_val``; ``status: complete`` if found,
      ``status: incomplete`` otherwise.
    """
    recipe = spec.recipe
    kind = spec.adaptation.kind

    if kind not in _RECIPE_REQUIRED_KINDS:
        # Structural / passthrough kinds — recipe is always complete.
        if recipe is None:
            return {"status": "complete", "source_value": source_val, "target_value": target_val}
        result = dict(recipe) if isinstance(recipe, dict) else {}
        result["status"] = "complete"
        result.setdefault("source_value", source_val)
        result.setdefault("target_value", target_val)
        return result

    # Recipe-required kinds.
    if recipe is None:
        return {"status": "incomplete", "source_value": source_val, "target_value": target_val}

    if isinstance(recipe, dict):
        # Unconditional params (e.g. round policy, default value).
        result = dict(recipe)
        result["status"] = "complete"
        result.setdefault("source_value", source_val)
        result.setdefault("target_value", target_val)
        return result

    # List of value-conditional rows — find one matching source/target.
    for row in recipe:
        if row.get("source") == source_val and row.get("target") == target_val:
            result = {k: v for k, v in row.items() if k not in ("source", "target")}
            result["status"] = "complete"
            result["source_value"] = source_val
            result["target_value"] = target_val
            return result

    return {"status": "incomplete", "source_value": source_val, "target_value": target_val}


# ── Category ordering ─────────────────────────────────────────────────────────

_CATEGORY_ORDER = [
    CompatibilityCategory.NON_BREAKING,
    CompatibilityCategory.PROJECTION_COMPATIBLE,
    CompatibilityCategory.DETERMINISTIC_TRANSFORM,
    CompatibilityCategory.ADAPTATION_STRATEGY,
    CompatibilityCategory.POLICY_REQUIRED,
    CompatibilityCategory.MANUAL_MAPPING_REQUIRED,
    CompatibilityCategory.UNSUPPORTED,
]


def _higher_category(a: CompatibilityCategory, b: CompatibilityCategory) -> CompatibilityCategory:
    """Return the more severe of two categories."""
    return a if _CATEGORY_ORDER.index(a) >= _CATEGORY_ORDER.index(b) else b


# ── Aspect extraction helpers ─────────────────────────────────────────────────


def _extract_newer_aspects(event: EntityChanged | PropertyChanged) -> dict[str, Any]:
    """Return new values from a diff event (the newer representation)."""
    if event.change_type == ChangeType.REMOVED:
        return {}
    result: dict[str, Any] = {}
    for key, val in event.aspects.items():
        _, new_val, _ = extract_op_full(val)
        if new_val is not None:
            result[key] = new_val
    return result


def _extract_older_aspects(event: EntityChanged | PropertyChanged) -> dict[str, Any]:
    """Return old values from a diff event (the older representation).

    Gathers ``_previous`` values from annotated MODIFIED aspects and the full
    ``previous_aspects`` dict from REMOVED events.
    """
    if event.change_type == ChangeType.REMOVED:
        return dict(event.previous_aspects)
    result: dict[str, Any] = {}
    for key, val in event.aspects.items():
        _, _, prev_val = extract_op_full(val)
        if prev_val is not None:
            result[key] = prev_val
    return result


def _find_concept_uri(tables: dict | None, label: str) -> str | None:
    """Look up the concept_uri for a label in a ledger snapshot; return None if absent or ledger not given."""
    if tables is None:
        return None
    concepts = tables.get("concepts")
    if concepts is None or concepts.empty:
        return None
    match = concepts[concepts["current_label"] == label]
    if not match.empty:
        return str(match.iloc[0]["concept_uri"])
    # Fall back to previous_labels search
    for _, row in concepts.iterrows():
        prev_raw = row.get("previous_labels")
        if not prev_raw or (isinstance(prev_raw, float)):
            continue
        try:
            prev_labels: list[str] = json.loads(prev_raw) if isinstance(prev_raw, str) else prev_raw
            if label in prev_labels:
                return str(row["concept_uri"])
        except (json.JSONDecodeError, TypeError):
            continue
    return None


# ── Rationale builder ─────────────────────────────────────────────────────────


def _rationale(
    change_kind: str,
    category: CompatibilityCategory,
    event: EntityChanged | PropertyChanged,
    direction: AdaptDirection,
) -> str:
    label = event.label
    if change_kind == "field_renamed":
        if direction == AdaptDirection.NEWER_TO_OLDER:
            return (
                f"The concept identity is preserved but the label changed from '{event.renamed_from}' to '{label}'. "
                f"Older consumers expecting '{event.renamed_from}' can be served by reading from '{label}'."
            )
        else:
            return (
                f"The concept identity is preserved but the label changed from '{event.renamed_from}' to '{label}'. "
                f"The platform expecting '{label}' can be served from '{event.renamed_from}' in the older client."
            )
    if change_kind == "aspect_changed":
        return (
            f"The concept identity is preserved but one or more aspects changed for '{label}'. "
            "An adaptation pipeline is required."
        )
    if change_kind == "field_added":
        if direction == AdaptDirection.NEWER_TO_OLDER:
            return (
                f"'{label}' is new in the newer release and absent from the older release. "
                "Older consumers can safely ignore this field — no adaptation needed."
            )
        else:
            return (
                f"'{label}' is new in the newer release. "
                "Older clients cannot provide it — a manual default or mapping is required."
            )
    if change_kind == "field_removed":
        if direction == AdaptDirection.NEWER_TO_OLDER:
            return (
                f"'{label}' existed in the older release but is absent from the newer release. "
                "A default injection is needed for older consumers."
            )
        else:
            return (
                f"'{label}' was removed in the newer release. "
                "Older clients may send it; the platform ignores extra fields — no adaptation needed."
            )
    if change_kind in ("entity_added", "entity_removed"):
        return f"Entity '{label}' was {change_kind.split('_')[1]}."
    if category == CompatibilityCategory.NON_BREAKING:
        return f"Change to '{label}' is non-breaking for this direction."
    return f"Change to '{label}' requires review ({change_kind})."


# ── Summary computation ───────────────────────────────────────────────────────


def _compute_summary(entries: list[CompatibilityEntry]) -> ReportSummary:
    s = ReportSummary(total=len(entries))
    for e in entries:
        match e.category:
            case CompatibilityCategory.PROJECTION_COMPATIBLE:
                s.projection_compatible += 1
            case CompatibilityCategory.DETERMINISTIC_TRANSFORM:
                s.deterministic_transform += 1
            case CompatibilityCategory.ADAPTATION_STRATEGY:
                s.adaptation_strategy += 1
            case CompatibilityCategory.POLICY_REQUIRED:
                s.policy_required += 1
            case CompatibilityCategory.MANUAL_MAPPING_REQUIRED:
                s.manual_mapping_required += 1
            case CompatibilityCategory.UNSUPPORTED:
                s.unsupported += 1
            case CompatibilityCategory.NON_BREAKING:
                s.non_breaking += 1
        if e.adapter_candidate:
            s.adapter_candidates += 1
    return s


# ── Output serializers ────────────────────────────────────────────────────────


def _entry_to_dict(entry: CompatibilityEntry) -> dict[str, Any]:
    change: dict[str, Any] = {"kind": entry.change_kind}
    if entry.changed_aspects:
        change["aspects"] = entry.changed_aspects
    return {
        "change_id": entry.change_id,
        "concept_uri": entry.concept_uri,
        "consumer_impact": entry.consumer_impact,
        "category": entry.category.value,
        "adapter_candidate": entry.adapter_candidate,
        "lossiness": entry.lossiness.value,
        "change": change,
        "newer_representation": {
            "release": entry.newer_representation.release,
            "label": entry.newer_representation.label,
            "aspects": entry.newer_representation.aspects,
        },
        "older_representation": {
            "release": entry.older_representation.release,
            "label": entry.older_representation.label,
            "aspects": entry.older_representation.aspects,
        },
        "steps": entry.steps,
        "rationale": entry.rationale,
    }


def report_to_json(report: CompatibilityReport) -> str:
    """Serialise a :class:`CompatibilityReport` to a JSON string."""
    doc: dict[str, Any] = {
        "report_id": report.report_id,
        "newer_release": report.newer_release,
        "older_release": report.older_release,
        "direction": report.direction.value,
        "summary": {
            "total": report.summary.total,
            "projection_compatible": report.summary.projection_compatible,
            "deterministic_transform": report.summary.deterministic_transform,
            "adaptation_strategy": report.summary.adaptation_strategy,
            "policy_required": report.summary.policy_required,
            "manual_mapping_required": report.summary.manual_mapping_required,
            "unsupported": report.summary.unsupported,
            "non_breaking": report.summary.non_breaking,
            "adapter_candidates": report.summary.adapter_candidates,
        },
        "entries": [_entry_to_dict(e) for e in report.entries],
    }
    return json.dumps(doc, indent=2)


def report_to_markdown(report: CompatibilityReport) -> str:
    """Render a human-readable Markdown compatibility report."""
    lines: list[str] = []
    if report.direction == AdaptDirection.NEWER_TO_OLDER:
        direction_str = f"{report.newer_release} \u2192 {report.older_release} (newer\u2192older)"
    else:
        direction_str = f"{report.older_release} \u2192 {report.newer_release} (older\u2192newer)"
    lines.append(f"# Compatibility report: {direction_str}\n")
    s = report.summary
    lines.append("## Summary\n")
    lines.append(f"- **Total changes**: {s.total}")
    lines.append(f"- **Projection-compatible**: {s.projection_compatible}")
    lines.append(f"- **Deterministic transform**: {s.deterministic_transform}")
    lines.append(f"- **Adaptation strategy (recipe incomplete)**: {s.adaptation_strategy}")
    lines.append(f"- **Policy required**: {s.policy_required}")
    lines.append(f"- **Manual mapping required**: {s.manual_mapping_required}")
    lines.append(f"- **Unsupported**: {s.unsupported}")
    lines.append(f"- **Non-breaking**: {s.non_breaking}")
    lines.append(f"- **Adapter candidates**: {s.adapter_candidates}\n")

    adaptable = [e for e in report.entries if e.adapter_candidate]
    if adaptable:
        lines.append("## Adapter recipes\n")
        for entry in adaptable:
            if report.direction == AdaptDirection.NEWER_TO_OLDER:
                src, tgt = entry.newer_representation, entry.older_representation
            else:
                src, tgt = entry.older_representation, entry.newer_representation
            lines.append(f"### {entry.change_id}: {entry.change_kind}\n")
            lines.append(f"**Concept**: `{entry.concept_uri or 'unknown'}`\n")
            lines.append(f"**From** ({src.release}): `{src.label}`")
            if src.aspects:
                lines.append("  " + ", ".join(f"{k}: {v}" for k, v in src.aspects.items()))
            lines.append(f"\n**To** ({tgt.release}): `{tgt.label}`")
            if tgt.aspects:
                lines.append("  " + ", ".join(f"{k}: {v}" for k, v in tgt.aspects.items()))
            lines.append(f"\n**Category**: {entry.category.value}")
            lines.append(f"**Lossiness**: {entry.lossiness.value}\n")
            if entry.steps:
                lines.append("**Steps**:\n")
                for step in entry.steps:
                    adapt_kind = step.get("adaptation", {}).get("kind", "?")
                    recipe = step.get("recipe", {})
                    recipe_str = ", ".join(f"{k}: {v}" for k, v in recipe.items() if v is not None)
                    lines.append(f"- kind: {adapt_kind}" + (f" | {recipe_str}" if recipe_str else ""))
            lines.append(f"\n**Rationale**: {entry.rationale}\n")
            lines.append("---\n")

    non_adaptable = [e for e in report.entries if not e.adapter_candidate and e.consumer_impact == "breaking"]
    if non_adaptable:
        lines.append("## Non-adaptable breaking changes\n")
        for entry in non_adaptable:
            lines.append(
                f"- `{entry.newer_representation.label}` ({entry.change_kind}): "
                f"{entry.category.value} — {entry.rationale}"
            )
        lines.append("")

    return "\n".join(lines)


def report_to_adaptation_plan(report: CompatibilityReport) -> str:
    """Serialise the adaptation rules to a runtime-agnostic YAML plan."""
    rules: list[dict[str, Any]] = []
    rule_counter = 0
    for entry in report.entries:
        if not entry.adapter_candidate:
            continue
        rule_counter += 1
        change: dict[str, Any] = {"kind": entry.change_kind}
        if entry.changed_aspects:
            change["aspects"] = entry.changed_aspects
        rule: dict[str, Any] = {
            "rule_id": f"rule-{rule_counter:04d}",
            "concept_uri": entry.concept_uri,
            "lossiness": entry.lossiness.value,
            "requires_policy": entry.lossiness in (Lossiness.POSSIBLE, Lossiness.GUARANTEED),
            "change": change,
            "steps": entry.steps,
        }
        rules.append(rule)

    plan: dict[str, Any] = {
        "adapter_id": report.report_id,
        "newer_release": report.newer_release,
        "older_release": report.older_release,
        "direction": report.direction.value,
        "rules": rules,
    }
    return yaml.dump(plan, sort_keys=False, allow_unicode=True)


def report_to_compact_summary(report: CompatibilityReport) -> str:
    """Render a compact plain-text summary for terminal output."""
    s = report.summary
    plural = "s" if s.total != 1 else ""
    if report.direction == AdaptDirection.NEWER_TO_OLDER:
        direction_label = f"{report.newer_release} \u2192 {report.older_release}  [newer\u2192older]"
    else:
        direction_label = f"{report.older_release} \u2192 {report.newer_release}  [older\u2192newer]"
    lines: list[str] = [
        f"Compatibility: {direction_label}  ({s.total} change{plural})",
        "",
    ]

    breaking_no_adapter = [e for e in report.entries if e.consumer_impact == "breaking" and not e.adapter_candidate]
    if breaking_no_adapter:
        lines.append(f"  breaking \u2014 no adapter ({len(breaking_no_adapter)}):")
        for e in breaking_no_adapter:
            lines.append(f"    - {e.newer_representation.label}  [{e.change_kind}]")
        lines.append("")

    candidates = [e for e in report.entries if e.adapter_candidate]
    if candidates:
        lines.append(f"  adapter candidates ({len(candidates)}):")
        for e in candidates:
            loss_tag = f"  (lossy: {e.lossiness.value})" if e.lossiness != Lossiness.NONE else ""
            incomplete_tag = "  [recipe incomplete]" if e.category == CompatibilityCategory.ADAPTATION_STRATEGY else ""
            lines.append(
                f"    - {e.newer_representation.label}  [{e.change_kind}]  {e.category.value}{loss_tag}{incomplete_tag}"
            )
        lines.append("")

    if s.adaptation_strategy:
        lines.append(f"  adaptation strategy (recipe incomplete): {s.adaptation_strategy}")
        lines.append("")

    if s.non_breaking:
        lines.append(f"  non-breaking: {s.non_breaking}")
        lines.append("")

    return "\n".join(lines)
