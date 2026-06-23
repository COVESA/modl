import logging
from pathlib import Path

import rich_click as click
from pydantic import ValidationError as PydanticValidationError
from rich.traceback import install

from . import __version__, log
from .config import BreakingChangeConfig, ModelMetadata
from .ir import DiffReport, validate_report_aspects
from .ledger import LedgerValidationError, empty_ledger, read_ledger, validate_ledger_dir, write_ledger
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
