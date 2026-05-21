from pathlib import Path

import pytest
from click.testing import CliRunner

from modl.cli import cli


class TestCli:
    def test_help(self) -> None:
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "sync" in result.output

    def test_sync_help(self) -> None:
        result = CliRunner().invoke(cli, ["sync", "--help"])
        assert result.exit_code == 0
        for option in ["DIFF_REPORT", "--ledger-dir", "--config", "--dry-run"]:
            assert option in result.output

    def test_sync_no_diff_report(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: myns\n")
        result = CliRunner().invoke(
            cli,
            ["sync", "--ledger-dir", str(tmp_path / "ledger"), "--config", str(config)],
        )
        assert result.exit_code == 0
        assert "initialising empty ledger" in caplog.text

    def test_sync_dry_run_flag(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: myns\n")
        result = CliRunner().invoke(
            cli,
            ["sync", "--ledger-dir", str(tmp_path / "ledger"), "--config", str(config), "--dry-run"],
        )
        assert result.exit_code == 0
        assert "Dry run" in caplog.text

    def test_sync_with_diff_report(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: myns\n")
        diff = tmp_path / "diff.json"
        diff.write_text('{"changes": []}')
        result = CliRunner().invoke(
            cli,
            ["sync", str(diff), "--ledger-dir", str(tmp_path / "ledger"), "--config", str(config)],
        )
        assert result.exit_code == 0
        assert "diff.json" in caplog.text

    def test_sync_missing_config_errors(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli,
            ["sync", "--ledger-dir", str(tmp_path), "--config", str(tmp_path / "missing.yaml")],
        )
        assert result.exit_code != 0

    def test_sync_missing_ledger_dir_option_errors(self, tmp_path: Path) -> None:
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: myns\n")
        result = CliRunner().invoke(cli, ["sync", "--config", str(config)])
        assert result.exit_code != 0
