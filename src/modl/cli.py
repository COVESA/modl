import logging
from pathlib import Path

import rich_click as click
from pydantic import ValidationError as PydanticValidationError
from rich.traceback import install

from . import __version__, log
from .config import BreakingChangeConfig
from .ledger import LedgerValidationError, empty_ledger, read_ledger, validate_ledger_dir, write_ledger


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
    "-c",
    "--config",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the breaking change config YAML file",
)
@click.option("-n", "--dry-run", is_flag=True, default=False, help="Preview changes without writing to disk")
def sync(diff_report: Path | None, ledger_dir: Path, config: Path, dry_run: bool) -> None:
    """Synchronise the ledger with a diff report, or initialise it if none exists."""
    try:
        cfg = BreakingChangeConfig.from_yaml(config)
    except PydanticValidationError as exc:
        for error in exc.errors():
            log.error("Invalid config — %s: %s", error["loc"][-1], error["msg"])
        raise SystemExit(1) from exc
    log.debug("Loaded config: namespace=%s prefix=%s", cfg.namespace.namespace, cfg.namespace.prefix)

    # Fail early: if the directory exists and is non-empty it must be a valid ledger
    dir_is_non_empty = ledger_dir.exists() and ledger_dir.is_dir() and any(ledger_dir.iterdir())
    if dir_is_non_empty:
        try:
            validate_ledger_dir(ledger_dir)
        except LedgerValidationError as exc:
            log.error("%s", exc)
            raise SystemExit(1) from exc

    ledger_exists = dir_is_non_empty

    if ledger_exists:
        tables = read_ledger(ledger_dir)
        log.info("Loaded existing ledger from %s", ledger_dir)
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

    if dry_run:
        log.info("Dry run — no changes will be written")
        return

    write_ledger(tables, ledger_dir)
    log.info("Ledger written to %s", ledger_dir)
