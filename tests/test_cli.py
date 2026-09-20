"""Tests for the CLI entry points (bookserver / buku)."""

import re
from pathlib import Path

from click.testing import CliRunner

from buku.cli import cli


def test_cli_help() -> None:
    """Verify root CLI help outputs available commands."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "scan" in result.output
    assert "migrate" in result.output


def test_cli_version() -> None:
    """Verify version option works."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_cli_serve_help() -> None:
    """Verify serve command options."""
    runner = CliRunner()
    result = runner.invoke(cli, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.output
    assert "--port" in result.output
    assert "--reload" in result.output
    assert "--config" in result.output


def test_cli_scan_valid_directory(tmp_path: Path) -> None:
    """Verify scan command executes successfully when directory exists."""
    runner = CliRunner()
    books_dir = tmp_path / "books"
    books_dir.mkdir()

    result = runner.invoke(cli, ["scan", "--books-dir", str(books_dir)])
    assert result.exit_code == 0
    assert "Scanning library directory" in result.output
    assert "read-only mode" in result.output


def test_cli_scan_missing_directory(tmp_path: Path) -> None:
    """Verify scan command fails gracefully when directory does not exist."""
    runner = CliRunner()
    books_dir = tmp_path / "non_existent_books"

    result = runner.invoke(cli, ["scan", "--books-dir", str(books_dir)])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_cli_migrate(tmp_path: Path) -> None:
    """Verify migrate command creates config directory and prints status."""
    runner = CliRunner()
    cfg_dir = tmp_path / "cfg"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[paths]\nconfig_dir = "{cfg_dir}"\n')

    result = runner.invoke(cli, ["migrate", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "Target database" in result.output
    assert cfg_dir.is_dir()


def test_no_architecture_specific_checks() -> None:
    """Verify acceptance criterion: codebase does not contain platform.machine checks."""
    src_dir = Path(__file__).resolve().parent.parent / "src"
    py_files = list(src_dir.rglob("*.py"))
    assert len(py_files) > 0

    arch_pattern = re.compile(r"platform\.(machine|processor|architecture)", re.IGNORECASE)
    for py_file in py_files:
        content = py_file.read_text()
        match = arch_pattern.search(content)
        assert match is None, f"Found architecture-specific check in {py_file}: {match.group()}"
