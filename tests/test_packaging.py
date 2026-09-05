"""T-25: Version und Containerbau haengen an den Paketmetadaten, nicht an Kopien."""

from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

import tender_ai
from tender_ai.cli import app

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_version_comes_from_package_metadata():
    assert tender_ai.__version__ == _pyproject()["project"]["version"]


def test_cli_reports_the_same_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert tender_ai.__version__ in result.output


def test_dockerfile_runs_as_non_root_and_installs_from_the_lockfile():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER tender" in dockerfile
    assert "requirements.txt" in dockerfile
    # Der Container darf nicht als root laufen; ein spaeteres "USER root"
    # wuerde das stillschweigend zuruecknehmen.
    assert dockerfile.rindex("USER tender") > dockerfile.rfind("USER root")


def test_dockerignore_excludes_secrets_and_local_state():
    ignored = set((ROOT / ".dockerignore").read_text(encoding="utf-8").split())
    for entry in (".env", ".git", ".venv", "data"):
        assert entry in ignored
