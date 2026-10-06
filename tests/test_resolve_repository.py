import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from ephy_worker.coding_executor import resolve_repository, CodingExecutionError

# Helper to run git commands

def run_git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return result


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path
    # init repo
    run_git(repo, "init", "-q")
    # create a file
    (repo / "README.md").write_text("# test repo")
    run_git(repo, "add", "README.md")
    run_git(repo, "commit", "-m", "initial", "-q")
    # get commit SHA
    commit_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, commit_sha


def test_resolve_repository_valid(git_repo):
    repo, commit_sha = git_repo
    root, revision = resolve_repository(repo, "HEAD")
    assert root == repo
    assert revision == commit_sha


def test_resolve_repository_non_directory(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("data")
    with pytest.raises(CodingExecutionError) as exc:
        resolve_repository(file_path, "HEAD")
    assert exc.value.code == "repository_unavailable"
    assert exc.value.stage == "repository"
    assert "repository is not a directory" in exc.value.detail


def test_resolve_repository_not_git(tmp_path):
    # create a directory without git
    dir_path = tmp_path / "notgit"
    dir_path.mkdir()
    with pytest.raises(CodingExecutionError) as exc:
        resolve_repository(dir_path, "HEAD")
    assert exc.value.code == "repository_unavailable"
    assert exc.value.stage == "repository"
    assert "path is not a Git worktree" in exc.value.detail


def test_resolve_repository_root_mismatch(tmp_path, git_repo):
    repo, _ = git_repo
    subdir = repo / "sub"
    subdir.mkdir()
    with pytest.raises(CodingExecutionError) as exc:
        resolve_repository(subdir, "HEAD")
    assert exc.value.code == "repository_unavailable"
    assert exc.value.stage == "repository"
    assert "repository_path must name the Git worktree root" in exc.value.detail


def test_resolve_repository_invalid_revision(git_repo):
    repo, _ = git_repo
    with pytest.raises(CodingExecutionError) as exc:
        resolve_repository(repo, "invalid-rev-123")
    assert exc.value.code == "invalid_revision"
    assert exc.value.stage == "repository"
    assert "base revision does not resolve" in exc.value.detail

