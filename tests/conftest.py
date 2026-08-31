"""Shared fixtures: a throwaway git repo for fingerprint/watch tests."""

import subprocess
from pathlib import Path

import pytest


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        check=True, timeout=30,
    ).stdout


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "fw"
    repo.mkdir()
    (repo / "App").mkdir()
    (repo / "App" / "main.c").write_text("int main(void) { return 0; }\n")
    (repo / "notes.md").write_text("not watched\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo
