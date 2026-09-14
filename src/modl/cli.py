import json
import logging
from pathlib import Path

import rich_click as click
import yaml
from pydantic import ValidationError as PydanticValidationError
from rich.traceback import install

from . import __version__, log
from .adapt import (
    AdaptDirection,
    CompatibilityCategory,
    analyze,
    report_to_adaptation_plan,
    report_to_compact_summary,
    report_to_json,
    report_to_markdown,
)
from .config import AdaptationConfig, BreakingChangeConfig, ModelMetadata
from .ir import DiffReport, validate_report_aspects
from .ledger import LedgerValidationError, empty_ledger, export_bindings, read_ledger, validate_ledger_dir, write_ledger
from .sync import SyncError
from .sync import sync as run_sync


@click.group(context_settings={"auto_envvar_prefix": "modl"})
@click.option(
    "-l",
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    default="INFO",
    help="Log level",
    show_default=True,
)
@click.option("-L", "--log-file", type=click.Path(dir_okay=False, writable=True, path_type=Path), help="Log file")
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context, log_level: str, log_file: Path | None) -> None:
    ctx.ensure_object(dict)
    if log_file:
        file_handler = logging.FileHandler(log_file, mode="w")
        file_handler.setFormatter(logging.Formatter("%(asctime)s:%(levelname)s:%(message)s"))
        log.addHandler(file_handler)

    log.setLevel(log_level)
    if log_level == "DEBUG":
        install(show_locals=True)


@cli.command()
@click.option(
    "-d",
    "--diff-report",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the diff report JSON file. Omit to initialise an empty ledger.",
)
@click.option(
    "-o",
    "--ledger-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing (or to create) the ledger CSV files",
)
@click.option(
    "-m",
    "--model-metadata",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the model metadata YAML file (name, id, preferred_prefix)",
)
@click.option(
    "-b",
    "--breaking-aspects",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the breaking aspects config YAML file",
)
@click.option("-n", "--dry-run", is_flag=True, default=False, help="Preview changes without writing to disk")
@click.option(
    "-s",
    "--strict",
    is_flag=True,
    default=False,
    help="Treat unconfigured aspect keys in the diff report as errors instead of warnings",
)
def sync(
    diff_report: Path | None,
    ledger_dir: Path,
    model_metadata: Path,
    breaking_aspects: Path,
    dry_run: bool,
    strict: bool,
) -> None:
    """Synchronise the ledger with a diff report, or initialise it if none exists."""
    try:
        metadata = ModelMetadata.from_yaml(model_metadata)
    except PydanticValidationError as exc:
        for error in exc.errors():
            loc = " → ".join(str(p) for p in error["loc"])
            prefix = f"[{loc}] " if loc else ""
            log.error("Invalid model metadata — %s%s", prefix, error["msg"])
        raise SystemExit(1) from exc
    try:
        cfg = BreakingChangeConfig.from_yaml(breaking_aspects)
    except PydanticValidationError as exc:
        for error in exc.errors():
            loc = " → ".join(str(p) for p in error["loc"])
            prefix = f"[{loc}] " if loc else ""
            log.error("Invalid breaking aspects config — %s%s", prefix, error["msg"])
        raise SystemExit(1) from exc
    log.debug("Loaded metadata: id=%s preferred_prefix=%s", metadata.id, metadata.preferred_prefix)

    # Fail early: if the directory exists and is non-empty it must be a valid ledger
    dir_is_non_empty = ledger_dir.exists() and ledger_dir.is_dir() and any(ledger_dir.iterdir())
    if dir_is_non_empty:
        try:
            validate_ledger_dir(ledger_dir)
        except LedgerValidationError as exc:
            log.error("%s", exc)
            raise SystemExit(1) from None

    ledger_exists = dir_is_non_empty

    if ledger_exists:
        try:
            tables = read_ledger(ledger_dir)
        except LedgerValidationError as exc:
            log.error("Ledger validation error — %s", exc)
            raise SystemExit(1) from None
        log.info("Loaded existing ledger from %s", ledger_dir)

        # Guard: all concept URIs in the existing ledger must use the current metadata namespace.
        concepts_df = tables["concepts"]
        if not concepts_df.empty:
            existing_uri = str(concepts_df.iloc[0]["concept_uri"])
            expected_prefix = f"{metadata.id}concepts/"
            if not existing_uri.startswith(expected_prefix):
                found_ns = existing_uri.split("concepts/", 1)[0] if "concepts/" in existing_uri else existing_uri
                log.error(
                    "Namespace mismatch: the existing ledger uses namespace '%s' "
                    "(detected from concept_uri '%s') but the current metadata id is '%s'. "
                    "Use the same metadata id that was used to create this ledger.",
                    found_ns,
                    existing_uri,
                    metadata.id,
                )
                raise SystemExit(1)
    else:
        tables = empty_ledger()
        log.info("No existing ledger found — starting with empty tables")

    if diff_report is None:
        if not ledger_exists:
            log.info("No diff report provided — initialised empty ledger")
        else:
            log.info("No diff report provided — existing ledger unchanged")
    else:
        log.info("Diff report: %s", diff_report)
        try:
            report = DiffReport.from_json(diff_report.read_text())
        except PydanticValidationError as exc:
            for error in exc.errors():
                log.error("Invalid diff report — %s: %s", " → ".join(str(loc) for loc in error["loc"]), error["msg"])
            raise SystemExit(1) from exc

        structural_warnings = report.validate_structure(strict=strict)
        for w in structural_warnings:
            log.warning("Diff report: %s", w)

        aspect_warnings = validate_report_aspects(report, cfg, strict=strict)
        for w in aspect_warnings:
            log.warning("Diff report: %s", w)

        if strict and (structural_warnings or aspect_warnings):
            raise SystemExit(1)

        try:
            tables = run_sync(tables, report, metadata, cfg)
        except SyncError as exc:
            log.error("Sync error — %s", exc)
            raise SystemExit(1) from None
        except Exception as exc:
            log.error("Unexpected error — %s: %s", type(exc).__name__, exc)
            raise SystemExit(1) from None

    if dry_run:
        log.info("Dry run — no changes will be written")
        return

    write_ledger(tables, ledger_dir)
    log.info("Ledger written to %s", ledger_dir)


@cli.group("export")
@click.option(
    "-o",
    "--ledger-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory containing the ledger CSV files",
)
@click.option(
    "--output",
    required=True,
    type=click.Path(writable=True, path_type=Path),
    help="Path to write the exported file to (a directory when --struct-prefix is used)",
)
@click.pass_context
def export_group(ctx: click.Context, ledger_dir: Path, output: Path) -> None:
    """Export ledger tables into lookup mappings for downstream tooling."""
    ctx.ensure_object(dict)
    ctx.obj["ledger_dir"] = ledger_dir
    ctx.obj["output"] = output


@export_group.command("bindings")
@click.option(
    "-f",
    "--format",
    "export_format",
    type=click.Choice(["json", "vspec"]),
    default="json",
    show_default=True,
    help="Shape and serialization of the exported mapping",
)
@click.option(
    "-c",
    "--complete",
    is_flag=True,
    default=False,
    help="Include binding_uri (the full binding URI) alongside binding in every entry",
)
@click.option(
    "-p",
    "--struct-prefix",
    default=None,
    help=(
        "Top-level label prefix (e.g. 'Structs') marking struct/type entries in vspec output. "
        "When set, --output is treated as a directory; writes overlay_tree.vspec (domain tree) "
        "and types_tree.vspec (prefix-matched entries) inside it. Only valid with --format vspec."
    ),
)
@click.pass_context
def export_bindings_cmd(ctx: click.Context, export_format: str, complete: bool, struct_prefix: str | None) -> None:
    """Export active bindings as a lookup mapping (JSON or vspec-style YAML)."""
    ledger_dir: Path = ctx.obj["ledger_dir"]
    output: Path = ctx.obj["output"]

    if struct_prefix is not None and export_format != "vspec":
        log.error("--struct-prefix is only valid with --format vspec")
        raise SystemExit(1)

    try:
        tables = read_ledger(ledger_dir)
    except LedgerValidationError as exc:
        log.error("Ledger validation error — %s", exc)
        raise SystemExit(1) from None

    try:
        mapping = export_bindings(tables, format=export_format, complete=complete)
    except LedgerValidationError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from None

    if struct_prefix is not None:
        types_tree = {
            key: value for key, value in mapping.items() if key == struct_prefix or key.startswith(f"{struct_prefix}.")
        }
        overlay_tree = {key: value for key, value in mapping.items() if key not in types_tree}

        output.mkdir(parents=True, exist_ok=True)
        (output / "overlay_tree.vspec").write_text(yaml.safe_dump(overlay_tree, sort_keys=True))
        (output / "types_tree.vspec").write_text(yaml.safe_dump(types_tree, sort_keys=True))
        log.info(
            "Exported %d overlay binding(s) and %d type binding(s) to %s",
            len(overlay_tree),
            len(types_tree),
            output,
        )
        return

    if export_format == "vspec":
        output.write_text(yaml.safe_dump(mapping, sort_keys=True))
    else:
        output.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n")
    log.info("Exported %d active binding(s) to %s", len(mapping), output)


@cli.command()
@click.option(
    "--newer-ledger",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing the newer release ledger snapshot (optional; used to resolve concept URIs)",
)
@click.option(
    "--older-ledger",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing the older release ledger snapshot (optional; used to resolve concept URIs)",
)
@click.option(
    "-d",
    "--diff",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the diff report JSON file (changes from the older release to the newer release)",
)
@click.option(
    "--config",
    "breaking_config",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the breaking-change rules YAML file",
)
@click.option(
    "--adaptation-config",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the adaptation-rules YAML file. Omit to use built-in defaults only.",
)
@click.option(
    "--newer-release",
    default=None,
    help="Newer release label (default: newer ledger parent directory name, or 'newer')",
)
@click.option(
    "--older-release",
    default=None,
    help="Older release label (default: older ledger parent directory name, or 'older')",
)
@click.option(
    "--direction",
    type=click.Choice(["both", "newer-to-older", "older-to-newer"]),
    default="both",
    show_default=True,
    help=(
        "Direction(s) to analyse. "
        "'newer-to-older': reading — transform platform data for older consumers. "
        "'older-to-newer': writing — transform older client data for the platform. "
        "'both': run both directions."
    ),
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(file_okay=False, writable=True, path_type=Path),
    help="Write JSON report, Markdown summary, and YAML adaptation plan to this directory. Omit for compact stdout.",
)
def adapt(
    newer_ledger: Path | None,
    older_ledger: Path | None,
    diff: Path,
    breaking_config: Path,
    adaptation_config: Path | None,
    newer_release: str | None,
    older_release: str | None,
    direction: str,
    output_dir: Path | None,
) -> None:
    """Analyse contract compatibility between two release ledger snapshots."""
    # Resolve release labels
    if newer_release:
        newer_label = newer_release
    elif newer_ledger is not None:
        newer_label = newer_ledger.parent.name
    else:
        newer_label = "newer"

    if older_release:
        older_label = older_release
    elif older_ledger is not None:
        older_label = older_ledger.parent.name
    else:
        older_label = "older"

    # Load breaking-change config
    try:
        cfg = BreakingChangeConfig.from_yaml(breaking_config)
    except PydanticValidationError as exc:
        for error in exc.errors():
            loc = " → ".join(str(p) for p in error["loc"])
            prefix = f"[{loc}] " if loc else ""
            log.error("Invalid breaking-change config — %s%s", prefix, error["msg"])
        raise SystemExit(1) from exc

    # Load adaptation config (optional — built-in defaults apply when absent)
    if adaptation_config is not None:
        try:
            adapt_cfg = AdaptationConfig.from_yaml(adaptation_config)
        except PydanticValidationError as exc:
            for error in exc.errors():
                loc = " → ".join(str(p) for p in error["loc"])
                prefix = f"[{loc}] " if loc else ""
                log.error("Invalid adaptation config — %s%s", prefix, error["msg"])
            raise SystemExit(1) from exc
    else:
        adapt_cfg = AdaptationConfig.model_validate({})

    # Validate adaptation config consistency against breaking-change config
    if adaptation_config is not None:
        consistency_errors = adapt_cfg.validate_against_breaking_config(cfg)
        if consistency_errors:
            for err in consistency_errors:
                log.error("Adaptation config consistency error — %s", err)
            raise SystemExit(1) from None

    # Load ledger snapshots (optional — concept URIs will be None when absent)
    newer_tables: dict | None = None
    if newer_ledger is not None:
        try:
            newer_tables = read_ledger(newer_ledger)
        except LedgerValidationError as exc:
            log.error("Newer-release ledger error — %s", exc)
            raise SystemExit(1) from None

    older_tables: dict | None = None
    if older_ledger is not None:
        try:
            older_tables = read_ledger(older_ledger)
        except LedgerValidationError as exc:
            log.error("Older-release ledger error — %s", exc)
            raise SystemExit(1) from None

    # Parse diff report
    try:
        report = DiffReport.from_json(diff.read_text())
    except PydanticValidationError as exc:
        for error in exc.errors():
            log.error("Invalid diff report — %s: %s", " → ".join(str(loc) for loc in error["loc"]), error["msg"])
        raise SystemExit(1) from exc

    # Determine which directions to run
    dirs_to_run: list[AdaptDirection] = []
    if direction == "both":
        dirs_to_run = [AdaptDirection.NEWER_TO_OLDER, AdaptDirection.OLDER_TO_NEWER]
    elif direction == "newer-to-older":
        dirs_to_run = [AdaptDirection.NEWER_TO_OLDER]
    else:
        dirs_to_run = [AdaptDirection.OLDER_TO_NEWER]

    # Run compatibility analysis for each direction
    compat_reports = []
    for d in dirs_to_run:
        compat_report = analyze(
            report,
            cfg,
            adapt_cfg,
            newer_label,
            older_label,
            direction=d,
            newer_tables=newer_tables,
            older_tables=older_tables,
        )
        compat_reports.append(compat_report)

        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            base = compat_report.report_id
            (output_dir / f"{base}.json").write_text(report_to_json(compat_report))
            (output_dir / f"{base}.md").write_text(report_to_markdown(compat_report))
            (output_dir / f"{base}.yaml").write_text(report_to_adaptation_plan(compat_report))
        else:
            click.echo(report_to_compact_summary(compat_report))

    if output_dir:
        log.info("Compatibility report(s) written to %s/", output_dir)

    # Exit with code 1 if any breaking change requires manual intervention
    needs_manual = any(
        e.category in (CompatibilityCategory.MANUAL_MAPPING_REQUIRED, CompatibilityCategory.UNSUPPORTED)
        for r in compat_reports
        for e in r.entries
        if e.consumer_impact == "breaking"
    )
    if needs_manual:
        raise SystemExit(1)
