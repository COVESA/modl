"""Tests for the compatibility analysis engine and CLI command (modl adapt)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from modl.adapt import (
    AdaptDirection,
    CompatibilityCategory,
    Lossiness,
    analyze,
    report_to_adaptation_plan,
    report_to_json,
    report_to_markdown,
)
from modl.cli import cli
from modl.config import AdaptationConfig, BreakingChangeConfig
from modl.ir import ChangeType, DiffReport, PropertyChanged
from modl.ledger import empty_ledger, write_ledger

# ── Fixtures and helpers ──────────────────────────────────────────────────────

NS = "http://test.example/model/"


def _cfg(**kwargs) -> BreakingChangeConfig:
    raw: dict = {}
    if "entity" in kwargs:
        raw["entity"] = kwargs["entity"]
    if "property_" in kwargs:
        raw["property"] = kwargs["property_"]
    return BreakingChangeConfig.model_validate(raw)


def _adapt_cfg(
    property_: dict | None = None,
    enum_value: dict | None = None,
) -> AdaptationConfig:
    raw: dict = {}
    if property_:
        raw["property"] = property_
    if enum_value:
        raw["enum_value"] = enum_value
    return AdaptationConfig.model_validate(raw)


def _minimal_ledger(ns: str = NS) -> dict:
    """Return an otherwise-empty ledger with one ENTITY concept and one PROPERTY concept."""
    ledger = empty_ledger()
    import pandas as pd

    ledger["concepts"] = pd.DataFrame(
        {
            "serial": [0, 1],
            "concept_uri": [f"{ns}concepts/0", f"{ns}concepts/1"],
            "current_label": ["ChargingSession", "ChargingSession.duration"],
            "previous_labels": [None, None],
            "kind": ["ENTITY", "PROPERTY"],
            "status": ["ACTIVE", "ACTIVE"],
            "parent_uri": [None, f"{ns}concepts/0"],
            "instances": [None, None],
        }
    )
    ledger["revisions"] = pd.DataFrame(
        {
            "serial": [0, 1],
            "concept_uri": [f"{ns}concepts/0", f"{ns}concepts/1"],
            "revision_uri": [f"{ns}revisions/0", f"{ns}revisions/1"],
            "previous_revision_uri": [None, None],
            "status": ["ACTIVE", "ACTIVE"],
        }
    )
    ledger["contracts"] = pd.DataFrame(
        {
            "serial": [0, 1],
            "concept_uri": [f"{ns}concepts/0", f"{ns}concepts/1"],
            "contract_uri": [f"{ns}contracts/0", f"{ns}contracts/1"],
            "revision_uri": [f"{ns}revisions/0", f"{ns}revisions/1"],
            "status": ["ACTIVE", "ACTIVE"],
        }
    )
    return ledger


def _report(*changes) -> DiffReport:
    return DiffReport(changes=list(changes))


# ── Engine unit tests ─────────────────────────────────────────────────────────


class TestAnalyzeFieldRename:
    def test_rename_classified_projection_compatible(self) -> None:
        """Field rename produces a projection_compatible entry."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        assert len(report.entries) == 1
        entry = report.entries[0]
        assert entry.category == CompatibilityCategory.PROJECTION_COMPATIBLE
        assert entry.lossiness == Lossiness.NONE
        assert entry.adapter_candidate is True

    def test_rename_produces_rename_step(self) -> None:
        """Field rename produces a rename step with correct source and target paths."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        entry = report.entries[0]
        assert len(entry.steps) == 1
        step = entry.steps[0]
        assert step["adaptation"]["kind"] == "rename"
        assert step["recipe"]["source_path"] == "ChargingSession.duration"
        assert step["recipe"]["target_path"] == "ChargingSession.oldDuration"

    def test_rename_change_kind(self) -> None:
        """Field rename is reported as change_kind 'field_renamed'."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        assert report.entries[0].change_kind == "field_renamed"


class TestAnalyzeUnitChange:
    def _unit_event(self) -> PropertyChanged:
        return PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            aspects={"unit": {"_op": "modified", "_value": "minute", "_previous": "second"}},
        )

    def test_unit_change_deterministic_transform(self) -> None:
        """Unit change with a matching recipe row is deterministic_transform."""
        recipe_row = {"source": "minute", "target": "second", "factor": 60}
        adapt = _adapt_cfg(
            property_={"unit.modified": {"steps": [{"adaptation": {"kind": "scale"}, "recipe": [recipe_row]}]}}
        )
        cfg = _cfg(property_={"unit.modified": True})
        report = analyze(_report(self._unit_event()), cfg, adapt, "v11", "v8")
        entry = report.entries[0]
        assert entry.category == CompatibilityCategory.DETERMINISTIC_TRANSFORM
        assert entry.lossiness == Lossiness.NONE
        assert entry.adapter_candidate is True

    def test_unit_change_produces_scale_step(self) -> None:
        """Unit change produces a scale step."""
        adapt = _adapt_cfg(property_={"unit.modified": {"steps": [{"adaptation": {"kind": "scale"}}]}})
        cfg = _cfg(property_={"unit.modified": True})
        report = analyze(_report(self._unit_event()), cfg, adapt, "v11", "v8")
        steps = report.entries[0].steps
        assert any(s["adaptation"]["kind"] == "scale" for s in steps)

    def test_unit_change_no_adapt_config_is_manual_mapping(self) -> None:
        """unit.modified with no adaptation config yields manual_mapping_required — no built-in steps."""
        cfg = _cfg(property_={"unit.modified": True})
        report = analyze(_report(self._unit_event()), cfg, _adapt_cfg(), "v11", "v8")
        entry = report.entries[0]
        assert entry.category == CompatibilityCategory.MANUAL_MAPPING_REQUIRED
        assert entry.steps == []

    def test_recipe_params_passed_through(self) -> None:
        """Arbitrary recipe params declared for a step appear in the plan step recipe dict."""
        adapt = _adapt_cfg(
            property_={
                "unit.modified": {
                    "steps": [
                        {
                            "adaptation": {"kind": "scale"},
                            "recipe": [
                                {"source": "minute", "target": "second", "factor": 60, "custom_hint": "my_vocab"}
                            ],
                        }
                    ]
                }
            }
        )
        cfg = _cfg(property_={"unit.modified": True})
        report = analyze(_report(self._unit_event()), cfg, adapt, "v11", "v8")
        scale_steps = [s for s in report.entries[0].steps if s["adaptation"]["kind"] == "scale"]
        assert scale_steps[0]["recipe"]["custom_hint"] == "my_vocab"


class TestAnalyzeUnitChangeWithTypeNarrowing:
    def _event(self) -> PropertyChanged:
        return PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            aspects={
                "unit": {"_op": "modified", "_value": "minute", "_previous": "second"},
                "output_type": {"_op": "modified", "_value": "Float", "_previous": "Int"},
            },
        )

    def test_policy_required_when_narrowing(self) -> None:
        """Declared round step in pipeline produces policy_required + possible lossiness."""
        unit_row = {"source": "minute", "target": "second", "factor": 60}
        adapt = _adapt_cfg(
            property_={
                "unit.modified": {"steps": [{"adaptation": {"kind": "scale"}, "recipe": [unit_row]}]},
                "output_type.modified": {
                    "steps": [
                        {"adaptation": {"kind": "cast"}},
                        {"adaptation": {"kind": "round"}, "recipe": {"policy": "floor"}},
                    ]
                },
            }
        )
        cfg = _cfg(property_={"unit.modified": True, "output_type": True})
        report = analyze(_report(self._event()), cfg, adapt, "v11", "v8")
        entry = report.entries[0]
        assert entry.category == CompatibilityCategory.POLICY_REQUIRED
        assert entry.lossiness == Lossiness.POSSIBLE

    def test_three_step_pipeline(self) -> None:
        """Declared scale + cast + round steps all appear in the plan."""
        unit_row = {"source": "minute", "target": "second", "factor": 60}
        adapt = _adapt_cfg(
            property_={
                "unit.modified": {"steps": [{"adaptation": {"kind": "scale"}, "recipe": [unit_row]}]},
                "output_type.modified": {
                    "steps": [
                        {"adaptation": {"kind": "cast"}},
                        {"adaptation": {"kind": "round"}, "recipe": {"policy": "floor"}},
                    ]
                },
            }
        )
        cfg = _cfg(property_={"unit.modified": True, "output_type": True})
        report = analyze(_report(self._event()), cfg, adapt, "v11", "v8")
        kinds = [s["adaptation"]["kind"] for s in report.entries[0].steps]
        assert "scale" in kinds
        assert "cast" in kinds
        assert "round" in kinds


class TestAnalyzeFieldAdded:
    def test_added_field_non_breaking_newer_to_older(self) -> None:
        """ADDED property event is non-breaking in newer_to_older — older consumers ignore extra fields."""
        event = PropertyChanged(
            label="ChargingSession.newField",
            parent_label="ChargingSession",
            change_type=ChangeType.ADDED,
            aspects={"output_type": "Float"},
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        entry = report.entries[0]
        assert entry.category == CompatibilityCategory.NON_BREAKING
        assert entry.lossiness == Lossiness.NONE

    def test_added_field_not_adapter_candidate(self) -> None:
        """ADDED field is non-breaking in newer_to_older; adapter_candidate must be False."""
        event = PropertyChanged(
            label="ChargingSession.newField",
            parent_label="ChargingSession",
            change_type=ChangeType.ADDED,
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        assert report.entries[0].adapter_candidate is False


class TestAnalyzeFieldRemoved:
    def test_removed_field_requires_previous_aspects(self) -> None:
        """REMOVED PropertyChanged event without previous_aspects fails validation."""
        with pytest.raises(ValidationError, match="previous_aspects"):
            PropertyChanged(
                label="ChargingSession.duration",
                parent_label="ChargingSession",
                change_type=ChangeType.REMOVED,
            )

    def test_removed_field_always_unsupported_newer_to_older(self) -> None:
        """REMOVED field in newer_to_older is always UNSUPPORTED — no auto-emission of default steps."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.REMOVED,
            previous_aspects={"output_type": "Int", "unit": "second"},
        )
        ledger = _minimal_ledger()
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8", newer_tables=ledger, older_tables=ledger)
        entry = report.entries[0]
        assert entry.category == CompatibilityCategory.UNSUPPORTED
        assert entry.steps == []
        assert entry.adapter_candidate is False


class TestAnalyzeNonBreakingChange:
    def test_non_breaking_not_adapter_candidate(self) -> None:
        """Non-breaking change has consumer_impact non_breaking and adapter_candidate False."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            aspects={
                "description": {"_op": "modified", "_value": "updated description", "_previous": "old description"}
            },
        )
        cfg = _cfg(property_={"description": False})
        report = analyze(_report(event), cfg, _adapt_cfg(), "v11", "v8")
        entry = report.entries[0]
        assert entry.consumer_impact == "non_breaking"
        assert entry.adapter_candidate is False


class TestAnalyzeConceptAbsentInTarget:
    def test_absent_concept_has_no_concept_uri_when_no_ledger(self) -> None:
        """concept_uri is None when no ledger snapshots are provided."""
        event = PropertyChanged(
            label="ChargingSession.unknownField",
            parent_label="ChargingSession",
            change_type=ChangeType.REMOVED,
            previous_aspects={"output_type": "Int"},
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        assert report.entries[0].concept_uri is None

    def test_absent_concept_has_no_concept_uri_from_empty_ledger(self) -> None:
        """concept_uri is None when the concept is absent from the provided ledger."""
        event = PropertyChanged(
            label="ChargingSession.unknownField",
            parent_label="ChargingSession",
            change_type=ChangeType.REMOVED,
            previous_aspects={"output_type": "Int"},
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8", newer_tables={}, older_tables={})
        assert report.entries[0].concept_uri is None


class TestSummary:
    def test_summary_counts_correct(self) -> None:
        """Summary aggregation counts match per-entry categories."""
        events = [
            PropertyChanged(
                label="ChargingSession.duration",
                parent_label="ChargingSession",
                change_type=ChangeType.MODIFIED,
                renamed_from="ChargingSession.oldDuration",
            ),
            PropertyChanged(
                label="ChargingSession.newField",
                parent_label="ChargingSession",
                change_type=ChangeType.ADDED,
            ),
        ]
        report = analyze(_report(*events), _cfg(), _adapt_cfg(), "v11", "v8")
        assert report.summary.total == 2
        assert report.summary.projection_compatible == 1  # rename
        assert report.summary.non_breaking == 1  # ADDED is NON_BREAKING in newer_to_older


# ── Output serializer tests ───────────────────────────────────────────────────


class TestReportToJson:
    def _simple_report(self):
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        return analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")

    def test_valid_json(self) -> None:
        """report_to_json produces parseable JSON."""
        compat_report = self._simple_report()
        parsed = json.loads(report_to_json(compat_report))
        assert "entries" in parsed
        assert "summary" in parsed

    def test_roundtrip_fields(self) -> None:
        """Core report metadata is preserved in JSON output."""
        compat_report = self._simple_report()
        parsed = json.loads(report_to_json(compat_report))
        assert parsed["newer_release"] == "v11"
        assert parsed["older_release"] == "v8"
        assert parsed["direction"] == "newer_to_older"

    def test_entry_structure(self) -> None:
        """Each entry contains the required fields."""
        compat_report = self._simple_report()
        parsed = json.loads(report_to_json(compat_report))
        entry = parsed["entries"][0]
        required_fields = (
            "change_id",
            "change",
            "consumer_impact",
            "category",
            "adapter_candidate",
            "lossiness",
            "steps",
        )
        for f in required_fields:
            assert f in entry
        assert "kind" in entry["change"]


class TestReportToAdaptationPlan:
    def test_valid_yaml(self) -> None:
        """report_to_adaptation_plan produces parseable YAML."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        compat_report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        plan = yaml.safe_load(report_to_adaptation_plan(compat_report))
        assert plan["newer_release"] == "v11"
        assert plan["older_release"] == "v8"
        assert isinstance(plan["rules"], list)

    def test_rule_has_steps(self) -> None:
        """Each adaptable rule contains a steps list and a change section."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        compat_report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        plan = yaml.safe_load(report_to_adaptation_plan(compat_report))
        assert len(plan["rules"]) == 1
        rule = plan["rules"][0]
        assert "steps" in rule
        assert "change" in rule
        assert rule["change"]["kind"] == "field_renamed"

    def test_non_candidates_excluded_from_plan(self) -> None:
        """Non-adapter-candidate entries do not appear in the adaptation plan."""
        event = PropertyChanged(
            label="ChargingSession.newField",
            parent_label="ChargingSession",
            change_type=ChangeType.ADDED,
        )
        compat_report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        plan = yaml.safe_load(report_to_adaptation_plan(compat_report))
        assert plan["rules"] == []


class TestReportToMarkdown:
    def test_contains_adapter_recipe(self) -> None:
        """Markdown report contains adapter recipe section for adaptable entries."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        compat_report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        md = report_to_markdown(compat_report)
        assert "Adapter recipes" in md
        assert "field_renamed" in md

    def test_summary_section_present(self) -> None:
        """Markdown report contains a Summary section."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        compat_report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8")
        md = report_to_markdown(compat_report)
        assert "## Summary" in md


# ── Bidirectional engine tests ────────────────────────────────────────────────────────────


class TestAnalyzeOlderToNewer:
    """Verify that the older_to_newer direction inverts the breaking/non-breaking semantics."""

    def test_added_field_is_breaking_older_to_newer(self) -> None:
        """ADDED field is breaking in older_to_newer — platform expects it, old client can't provide it."""
        event = PropertyChanged(
            label="ChargingSession.newField",
            parent_label="ChargingSession",
            change_type=ChangeType.ADDED,
            aspects={"output_type": "Float"},
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8", direction=AdaptDirection.OLDER_TO_NEWER)
        entry = report.entries[0]
        assert entry.consumer_impact == "breaking"
        assert entry.category == CompatibilityCategory.MANUAL_MAPPING_REQUIRED
        assert entry.adapter_candidate is False

    def test_removed_field_is_non_breaking_older_to_newer(self) -> None:
        """REMOVED field is non-breaking in older_to_newer — old client may send it, platform ignores."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.REMOVED,
            previous_aspects={"output_type": "Int"},
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8", direction=AdaptDirection.OLDER_TO_NEWER)
        entry = report.entries[0]
        assert entry.consumer_impact == "non_breaking"
        assert entry.category == CompatibilityCategory.NON_BREAKING
        assert entry.adapter_candidate is False

    def test_rename_step_paths_inverted_older_to_newer(self) -> None:
        """Rename step uses source=older name, target=newer name in older_to_newer."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8", direction=AdaptDirection.OLDER_TO_NEWER)
        entry = report.entries[0]
        assert entry.adapter_candidate is True
        step = entry.steps[0]
        assert step["adaptation"]["kind"] == "rename"
        assert step["recipe"]["source_path"] == "ChargingSession.oldDuration"  # older name (what you read)
        assert step["recipe"]["target_path"] == "ChargingSession.duration"  # newer name (what platform expects)

    def test_report_id_and_direction_older_to_newer(self) -> None:
        """Report ID and direction field reflect the write direction."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            renamed_from="ChargingSession.oldDuration",
        )
        report = analyze(_report(event), _cfg(), _adapt_cfg(), "v11", "v8", direction=AdaptDirection.OLDER_TO_NEWER)
        assert report.report_id == "compat-v8-to-v11"
        assert report.direction == AdaptDirection.OLDER_TO_NEWER

    def test_aspect_step_source_target_inverted_older_to_newer(self) -> None:
        """Steps use source_value=older, target_value=newer in older_to_newer direction."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            aspects={"unit": {"_op": "modified", "_value": "minute", "_previous": "second"}},
        )
        cfg = _cfg(property_={"unit.modified": True})
        # older_to_newer: source=older(second), target=newer(minute)
        adapt = _adapt_cfg(
            property_={
                "unit.modified": {
                    "steps": [
                        {
                            "adaptation": {"kind": "scale"},
                            "recipe": [{"source": "second", "target": "minute", "factor": 1 / 60}],
                        }
                    ]
                }
            }
        )
        report = analyze(_report(event), cfg, adapt, "v11", "v8", direction=AdaptDirection.OLDER_TO_NEWER)
        entry = report.entries[0]
        scale_steps = [s for s in entry.steps if s["adaptation"]["kind"] == "scale"]
        assert len(scale_steps) == 1
        # older_to_newer: source = older value (second), target = newer value (minute)
        assert scale_steps[0]["recipe"]["source_value"] == "second"
        assert scale_steps[0]["recipe"]["target_value"] == "minute"
        assert scale_steps[0]["recipe"]["status"] == "complete"

    def test_changed_aspects_are_direction_neutral(self) -> None:
        """changed_aspects always records newer_value/older_value regardless of direction."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            aspects={"unit": {"_op": "modified", "_value": "minute", "_previous": "second"}},
        )
        cfg = _cfg(property_={"unit.modified": True})
        report_nto = analyze(_report(event), cfg, _adapt_cfg(), "v11", "v8", direction=AdaptDirection.NEWER_TO_OLDER)
        report_otn = analyze(_report(event), cfg, _adapt_cfg(), "v11", "v8", direction=AdaptDirection.OLDER_TO_NEWER)
        assert report_nto.entries[0].changed_aspects == report_otn.entries[0].changed_aspects
        fact = report_nto.entries[0].changed_aspects[0]
        assert fact["newer_value"] == "minute"
        assert fact["older_value"] == "second"


# ── CLI integration tests ─────────────────────────────────────────────────────


class TestAdaptCli:
    def _write_ledger(self, tmp_path: Path, subdir: str) -> Path:
        ledger_dir = tmp_path / subdir / "ledger"
        write_ledger(_minimal_ledger(), ledger_dir)
        return ledger_dir

    def _write_diff(self, tmp_path: Path) -> Path:
        diff_path = tmp_path / "diff.json"
        diff = {
            "changes": [
                {
                    "label": "ChargingSession.duration",
                    "parent_label": "ChargingSession",
                    "change_type": "MODIFIED",
                    "renamed_from": "ChargingSession.oldDuration",
                }
            ]
        }
        diff_path.write_text(json.dumps(diff))
        return diff_path

    def _write_breaking_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "breaking.yaml"
        p.write_text("property:\n  name.modified: false\n  unit.modified: true\n")
        return p

    def _write_adaptation_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "adaptation.yaml"
        p.write_text(
            "property:\n"
            "  unit.modified:\n"
            "    steps:\n"
            "      - adaptation:\n"
            "          kind: scale\n"
            "        recipe:\n"
            "          - source: minute\n"
            "            target: second\n"
            "            factor: 60\n"
        )
        return p

    def test_cli_exits_zero(self, tmp_path: Path) -> None:
        """modl adapt with valid inputs exits 0."""
        newer_ledger = self._write_ledger(tmp_path, "release-v11")
        older_ledger = self._write_ledger(tmp_path, "release-v8")
        diff = self._write_diff(tmp_path)
        breaking = self._write_breaking_config(tmp_path)
        adaptation = self._write_adaptation_config(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "adapt",
                "--newer-ledger",
                str(newer_ledger),
                "--older-ledger",
                str(older_ledger),
                "--diff",
                str(diff),
                "--config",
                str(breaking),
                "--adaptation-config",
                str(adaptation),
                "--newer-release",
                "v11",
                "--older-release",
                "v8",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_cli_prints_compact_summary(self, tmp_path: Path) -> None:
        """modl adapt prints a compact plain-text summary when --output-dir is not specified."""
        newer_ledger = self._write_ledger(tmp_path, "release-v11")
        older_ledger = self._write_ledger(tmp_path, "release-v8")
        diff = self._write_diff(tmp_path)
        breaking = self._write_breaking_config(tmp_path)
        adaptation = self._write_adaptation_config(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "adapt",
                "--newer-ledger",
                str(newer_ledger),
                "--older-ledger",
                str(older_ledger),
                "--diff",
                str(diff),
                "--config",
                str(breaking),
                "--adaptation-config",
                str(adaptation),
            ],
        )
        assert result.exit_code == 0
        # compact summary starts with the release labels, not a JSON object
        assert "Compatibility:" in result.output
        assert "{" not in result.output

    def test_cli_writes_output_dir(self, tmp_path: Path) -> None:
        """--output-dir writes JSON, Markdown, and YAML plan files."""
        newer_ledger = self._write_ledger(tmp_path, "release-v11")
        older_ledger = self._write_ledger(tmp_path, "release-v8")
        diff = self._write_diff(tmp_path)
        breaking = self._write_breaking_config(tmp_path)
        adaptation = self._write_adaptation_config(tmp_path)
        out_dir = tmp_path / "out"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "adapt",
                "--newer-ledger",
                str(newer_ledger),
                "--older-ledger",
                str(older_ledger),
                "--diff",
                str(diff),
                "--config",
                str(breaking),
                "--adaptation-config",
                str(adaptation),
                "--newer-release",
                "v11",
                "--older-release",
                "v8",
                "--output-dir",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (out_dir / "compat-v11-to-v8.json").exists()
        assert (out_dir / "compat-v11-to-v8.md").exists()
        plan = yaml.safe_load((out_dir / "compat-v11-to-v8.yaml").read_text())
        assert "rules" in plan

    def test_cli_release_labels_from_dir_names(self, tmp_path: Path) -> None:
        """Release labels default to parent directory names and appear in the output files."""
        newer_ledger = self._write_ledger(tmp_path, "release-v11")
        older_ledger = self._write_ledger(tmp_path, "release-v8")
        diff = self._write_diff(tmp_path)
        breaking = self._write_breaking_config(tmp_path)
        adaptation = self._write_adaptation_config(tmp_path)
        out_dir = tmp_path / "out"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "adapt",
                "--newer-ledger",
                str(newer_ledger),
                "--older-ledger",
                str(older_ledger),
                "--diff",
                str(diff),
                "--config",
                str(breaking),
                "--adaptation-config",
                str(adaptation),
                "--output-dir",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        # ledger dirs are release-v11/ledger and release-v8/ledger;
        # parent names are "release-v11" and "release-v8"
        report_file = out_dir / "compat-release-v11-to-release-v8.json"
        assert report_file.exists()
        parsed = json.loads(report_file.read_text())
        assert parsed["newer_release"] == "release-v11"
        assert parsed["older_release"] == "release-v8"

    def test_cli_direction_newer_to_older_writes_one_file(self, tmp_path: Path) -> None:
        """--direction newer-to-older writes only the newer→older report file."""
        diff = self._write_diff(tmp_path)
        breaking = self._write_breaking_config(tmp_path)
        out_dir = tmp_path / "out"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "adapt",
                "--diff",
                str(diff),
                "--config",
                str(breaking),
                "--newer-release",
                "v11",
                "--older-release",
                "v8",
                "--direction",
                "newer-to-older",
                "--output-dir",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (out_dir / "compat-v11-to-v8.json").exists()
        assert not (out_dir / "compat-v8-to-v11.json").exists()

    def test_cli_exits_one_when_manual_mapping_required(self, tmp_path: Path) -> None:
        """Exit code 1 when a breaking change requires manual intervention."""
        # ADDED field + older_to_newer direction = manual_mapping_required (platform expects it)
        diff_path = tmp_path / "diff.json"
        diff = {
            "changes": [
                {
                    "label": "ChargingSession.newField",
                    "parent_label": "ChargingSession",
                    "change_type": "ADDED",
                }
            ]
        }
        diff_path.write_text(json.dumps(diff))
        breaking = self._write_breaking_config(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "adapt",
                "--diff",
                str(diff_path),
                "--config",
                str(breaking),
                "--newer-release",
                "v11",
                "--older-release",
                "v8",
                "--direction",
                "older-to-newer",
            ],
        )
        assert result.exit_code == 1

    def test_cli_no_ledger_required(self, tmp_path: Path) -> None:
        """modl adapt works without --newer-ledger and --older-ledger (concept URIs will be None)."""
        diff = self._write_diff(tmp_path)
        breaking = self._write_breaking_config(tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "adapt",
                "--diff",
                str(diff),
                "--config",
                str(breaking),
                "--newer-release",
                "v11",
                "--older-release",
                "v8",
                "--direction",
                "newer-to-older",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Compatibility:" in result.output

    def test_cli_adaptation_config_consistency_error(self, tmp_path: Path) -> None:
        """Adaptation config key not matching a breaking aspect exits 1 with an error message."""
        diff = self._write_diff(tmp_path)
        breaking = self._write_breaking_config(tmp_path)

        # Adaptation config references 'datatype.modified' which is NOT in the breaking config.
        bad_adapt = tmp_path / "bad_adaptation.yaml"
        bad_adapt.write_text("property:\n  datatype.modified:\n    steps:\n      - adaptation:\n          kind: cast\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "adapt",
                "--diff",
                str(diff),
                "--config",
                str(breaking),
                "--adaptation-config",
                str(bad_adapt),
                "--newer-release",
                "v11",
                "--older-release",
                "v8",
            ],
        )
        assert result.exit_code == 1


# ── Three-level separation: recipe resolver tests ──────────────────────────────


class TestAdaptationStrategy:
    """Tests for the ADAPTATION_STRATEGY category and recipe resolution."""

    def _unit_event(self) -> PropertyChanged:
        return PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            aspects={"unit": {"_op": "modified", "_value": "minute", "_previous": "second"}},
        )

    def test_adaptation_strategy_when_recipe_missing(self) -> None:
        """Step declared but recipe not provided → ADAPTATION_STRATEGY category."""
        adapt = _adapt_cfg(property_={"unit.modified": {"steps": [{"adaptation": {"kind": "scale"}}]}})
        cfg = _cfg(property_={"unit.modified": True})
        report = analyze(_report(self._unit_event()), cfg, adapt, "v11", "v8")
        entry = report.entries[0]
        assert entry.category == CompatibilityCategory.ADAPTATION_STRATEGY
        assert entry.adapter_candidate is True

    def test_recipe_status_complete_with_matching_row(self) -> None:
        """Scale step with matching source/target row produces status: complete."""
        adapt = _adapt_cfg(
            property_={
                "unit.modified": {
                    "steps": [
                        {
                            "adaptation": {"kind": "scale"},
                            "recipe": [{"source": "minute", "target": "second", "factor": 60}],
                        }
                    ]
                }
            }
        )
        cfg = _cfg(property_={"unit.modified": True})
        report = analyze(_report(self._unit_event()), cfg, adapt, "v11", "v8")
        scale_steps = [s for s in report.entries[0].steps if s["adaptation"]["kind"] == "scale"]
        assert scale_steps[0]["recipe"]["status"] == "complete"
        assert scale_steps[0]["recipe"]["factor"] == 60

    def test_recipe_status_incomplete_no_matching_row(self) -> None:
        """Scale step with no matching source/target row produces status: incomplete."""
        adapt = _adapt_cfg(
            property_={
                "unit.modified": {
                    "steps": [
                        {
                            "adaptation": {"kind": "scale"},
                            "recipe": [{"source": "other", "target": "other2", "factor": 1}],
                        }
                    ]
                }
            }
        )
        cfg = _cfg(property_={"unit.modified": True})
        report = analyze(_report(self._unit_event()), cfg, adapt, "v11", "v8")
        scale_steps = [s for s in report.entries[0].steps if s["adaptation"]["kind"] == "scale"]
        assert scale_steps[0]["recipe"]["status"] == "incomplete"

    def test_round_step_unconditional_recipe_complete(self) -> None:
        """Round step with dict recipe (unconditional) is always status: complete."""
        event = PropertyChanged(
            label="ChargingSession.value",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            aspects={"output_type": {"_op": "modified", "_value": "Float", "_previous": "Int"}},
        )
        adapt = _adapt_cfg(
            property_={
                "output_type.modified": {"steps": [{"adaptation": {"kind": "round"}, "recipe": {"policy": "floor"}}]}
            }
        )
        cfg = _cfg(property_={"output_type": True})
        report = analyze(_report(event), cfg, adapt, "v11", "v8")
        round_steps = [s for s in report.entries[0].steps if s["adaptation"]["kind"] == "round"]
        assert round_steps[0]["recipe"]["status"] == "complete"
        assert round_steps[0]["recipe"]["policy"] == "floor"

    def test_summary_adaptation_strategy_counted(self) -> None:
        """Summary.adaptation_strategy is incremented for each ADAPTATION_STRATEGY entry."""
        adapt = _adapt_cfg(property_={"unit.modified": {"steps": [{"adaptation": {"kind": "scale"}}]}})
        cfg = _cfg(property_={"unit.modified": True})
        report = analyze(_report(self._unit_event()), cfg, adapt, "v11", "v8")
        assert report.summary.adaptation_strategy == 1
        assert report.summary.deterministic_transform == 0

    def test_adaptation_plan_contains_change_section(self) -> None:
        """Adaptation plan rules include a change section with kind and aspects."""
        event = PropertyChanged(
            label="ChargingSession.duration",
            parent_label="ChargingSession",
            change_type=ChangeType.MODIFIED,
            aspects={"unit": {"_op": "modified", "_value": "minute", "_previous": "second"}},
        )
        unit_row = {"source": "minute", "target": "second", "factor": 60}
        adapt = _adapt_cfg(
            property_={"unit.modified": {"steps": [{"adaptation": {"kind": "scale"}, "recipe": [unit_row]}]}}
        )
        cfg = _cfg(property_={"unit.modified": True})
        report = analyze(_report(event), cfg, adapt, "v11", "v8")
        plan = yaml.safe_load(report_to_adaptation_plan(report))
        rule = plan["rules"][0]
        assert rule["change"]["kind"] == "aspect_changed"
        assert rule["change"]["aspects"][0]["key"] == "unit"
        step = rule["steps"][0]
        assert step["adaptation"]["kind"] == "scale"
        assert step["recipe"]["status"] == "complete"
