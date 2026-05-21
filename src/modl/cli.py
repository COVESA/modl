import logging
from pathlib import Path

import rich_click as click
from rich.traceback import install

from . import __version__, log


@click.group(context_settings={"auto_envvar_prefix": "modl"})
@click.option(
    "-l",
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False),
    default="INFO",
    help="Log level",
    show_default=True,
)
@click.option("--log-file", type=click.Path(dir_okay=False, writable=True, path_type=Path), help="Log file")
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
@click.argument("diff_report", required=False, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--ledger-dir",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory containing (or to create) the ledger CSV files",
)
@click.option(
    "--config",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the breaking change config YAML file",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview changes without writing to disk")
def sync(diff_report: Path | None, ledger_dir: Path, config: Path, dry_run: bool) -> None:
    """Synchronise the ledger with a diff report, or initialise it if none exists."""
    if dry_run:
        log.info("Dry run — no changes will be written")

    if diff_report is None:
        log.info("No diff report provided — initialising empty ledger")
    else:
        log.info("Diff report: %s", diff_report)

    log.info("Ledger dir: %s", ledger_dir)
    log.info("Config:     %s", config)
    log.debug("Dry run: %s", dry_run)
