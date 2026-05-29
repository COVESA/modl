from pathlib import Path

import pytest
from click.testing import CliRunner

from modl.cli import cli


class TestCli:
    def test_help(self) -> None:
        """Top-level --help exits cleanly and lists the sync subcommand."""
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "sync" in result.output

    def test_sync_help(self) -> None:
        """sync --help exposes all expected short and long option flags."""
        result = CliRunner().invoke(cli, ["sync", "--help"])
        assert result.exit_code == 0
        for option in ["--diff-report", "-d", "--ledger-dir", "-o", "--config", "-c", "--dry-run", "-n"]:
            assert option in result.output

    def test_sync_no_diff_report(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """No diff report → initialises empty ledger and logs it."""
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: http://example.org/myns/\n")
        result = CliRunner().invoke(
            cli,
            ["sync", "--ledger-dir", str(tmp_path / "ledger"), "--config", str(config)],
        )
        assert result.exit_code == 0
        assert "empty ledger" in caplog.text

    def test_sync_dry_run_flag(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """--dry-run prevents writes and logs a dry-run notice."""
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: http://example.org/myns/\n")
        result = CliRunner().invoke(
            cli,
            ["sync", "--ledger-dir", str(tmp_path / "ledger"), "--config", str(config), "--dry-run"],
        )
        assert result.exit_code == 0
        assert "Dry run" in caplog.text

    def test_sync_with_diff_report(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Provided diff report path appears in log output."""
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: http://example.org/myns/\n")
        diff = tmp_path / "diff.json"
        diff.write_text('{"changes": []}')
        result = CliRunner().invoke(
            cli,
            ["sync", "--diff-report", str(diff), "--ledger-dir", str(tmp_path / "ledger"), "--config", str(config)],
        )
        assert result.exit_code == 0
        assert "diff.json" in caplog.text

    def test_sync_invalid_diff_report_errors(self, tmp_path: Path) -> None:
        """Malformed diff report JSON causes non-zero exit."""
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: http://example.org/myns/\n")
        diff = tmp_path / "diff.json"
        diff.write_text('{"changes": [{"label": "X", "kind": "INVALID_KIND", "change_type": "ADDED"}]}')
        result = CliRunner().invoke(
            cli,
            ["sync", "--diff-report", str(diff), "--ledger-dir", str(tmp_path / "ledger"), "--config", str(config)],
        )
        assert result.exit_code != 0

    def test_sync_strict_flag_fails_on_unknown_aspects(self, tmp_path: Path) -> None:
        """--strict causes non-zero exit when diff report has unconfigured aspect keys."""
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: http://example.org/myns/\n")
        diff = tmp_path / "diff.json"
        diff.write_text(
            '{"changes": [{"label": "X.speed", "parent_label": "X", "kind": "PROPERTY",'
            ' "change_type": "MODIFIED", "aspects": {"unit": "mph"}}]}'
        )
        result = CliRunner().invoke(
            cli,
            [
                "sync",
                "--diff-report",
                str(diff),
                "--ledger-dir",
                str(tmp_path / "ledger"),
                "--config",
                str(config),
                "--strict",
            ],
        )
        assert result.exit_code != 0

    def test_sync_missing_config_errors(self, tmp_path: Path) -> None:
        """Non-existent config file causes non-zero exit."""
        result = CliRunner().invoke(
            cli,
            ["sync", "--ledger-dir", str(tmp_path), "--config", str(tmp_path / "missing.yaml")],
        )
        assert result.exit_code != 0

    def test_sync_missing_ledger_dir_option_errors(self, tmp_path: Path) -> None:
        """Omitting required --ledger-dir causes non-zero exit."""
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: http://example.org/myns/\n")
        result = CliRunner().invoke(cli, ["sync", "--config", str(config)])
        assert result.exit_code != 0

    def test_sync_dirty_ledger_dir_errors(self, tmp_path: Path) -> None:
        """Dir with unrecognised files causes non-zero exit before any write."""
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: http://example.org/myns/\n")
        ledger_dir = tmp_path / "ledger"
        ledger_dir.mkdir()
        (ledger_dir / "unrelated.txt").write_text("oops")
        result = CliRunner().invoke(
            cli,
            ["sync", "--ledger-dir", str(ledger_dir), "--config", str(config)],
        )
        assert result.exit_code != 0

    def test_sync_engine_sync_error_surfaces_cleanly(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """SyncError from the engine exits non-zero with a single logged error line and no traceback."""
        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: http://example.org/myns/\n")
        diff = tmp_path / "diff.json"
        # nested list in instances triggers the _validate_instances guard
        diff.write_text(
            '{"changes": [{"label": "Door", "kind": "ENTITY", "change_type": "ADDED",'
            ' "aspects": {"instances": [["Front", "Rear"]]}}]}'
        )
        result = CliRunner().invoke(
            cli,
            ["sync", "--diff-report", str(diff), "--ledger-dir", str(tmp_path / "ledger"), "--config", str(config)],
        )
        assert result.exit_code != 0
        assert "Sync error" in caplog.text
        assert "Traceback" not in result.output

    def test_sync_corrupt_ledger_surfaces_cleanly(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Corrupt ledger (invalid status value) exits non-zero with a single log line and no traceback."""
        import pandas as pd

        from modl.ledger import empty_ledger, write_ledger

        config = tmp_path / "modl.yaml"
        config.write_text("namespace:\n  namespace: http://example.org/myns/\n")
        ledger_dir = tmp_path / "ledger"
        tables = empty_ledger()
        tables["concepts"] = pd.DataFrame(
            {
                "serial": [0],
                "concept_uri": ["http://example.org/myns/concepts/0"],
                "current_label": ["Vehicle"],
                "previous_labels": [None],
                "kind": ["ENTITY"],
                "status": ["PENDING"],  # invalid status — will fail validate_ledger
                "parent_uri": [None],
                "instances": [None],
            }
        )
        write_ledger(tables, ledger_dir)
        result = CliRunner().invoke(
            cli,
            ["sync", "--ledger-dir", str(ledger_dir), "--config", str(config)],
        )
        assert result.exit_code != 0
        assert "Ledger validation error" in caplog.text
        assert "Traceback" not in result.output

    def test_sync_invalid_config_yaml_structure_errors(self, tmp_path: Path) -> None:
        """Config YAML whose structure fails Pydantic validation causes non-zero exit."""
        config = tmp_path / "modl.yaml"
        config.write_text("namespace: not_a_mapping\n")
        result = CliRunner().invoke(
            cli,
            ["sync", "--ledger-dir", str(tmp_path / "ledger"), "--config", str(config)],
        )
        assert result.exit_code != 0
