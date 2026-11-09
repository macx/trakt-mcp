"""The release gate (scripts/check_release.py) against throwaway git repositories."""

import importlib.util
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_release.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_release", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = _load()


def run_git(root: Path, *args: str) -> str:
    command = ["git", *args]
    result = subprocess.run(command, cwd=root, check=True, capture_output=True, text=True)  # noqa: S603
    return result.stdout.strip()


Repo = Callable[..., Path]


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """A minimal project at version 0.4.2; returns a function that commits files."""
    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "config", "user.email", "test@example.invalid")
    run_git(tmp_path, "config", "user.name", "Test")
    run_git(tmp_path, "config", "commit.gpgsign", "false")

    def commit(files: dict[str, str] | None = None) -> Path:
        defaults = {
            "pyproject.toml": '[project]\nname = "x"\nversion = "0.4.2"\n',
            "src/trakt_mcp/__init__.py": '"""x"""\n\n__version__ = "0.4.2"\n',
            "compose.example.yml": "services:\n  x:\n    image: ghcr.io/o/x:0.4\n",
            "docs/security-reviews.md": "| Version |\n|---|\n",
            "src/trakt_mcp/code.py": "x = 1\n",
        }
        for name, content in ({} if (tmp_path / "pyproject.toml").exists() else defaults).items():
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text(content)
        for name, content in (files or {}).items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        run_git(tmp_path, "add", "-A")
        run_git(tmp_path, "commit", "-q", "-m", "c")
        return tmp_path

    return commit


def cleared(sha: str, version: str = "0.4.2") -> str:
    return (
        "| Version | Date | Scope | Result |\n|---|---|---|---|\n"
        f"| {version} | 2026-11-09 | `a..b` | **cleared** (reviewed commit `{sha}`) |\n"
    )


REVIEWS = "docs/security-reviews.md"


def test_record_after_the_review_is_releasable(repo: Repo) -> None:
    root = repo()
    reviewed = run_git(root, "rev-parse", "HEAD")
    repo({REVIEWS: cleared(reviewed)})
    assert "cleared at" in release.check(root, "v0.4.2")
    assert release.main(["v0.4.2"], root) == 0


def test_tagging_the_reviewed_commit_itself_works(repo: Repo) -> None:
    root = repo()
    first = run_git(root, "rev-parse", "HEAD")
    repo({REVIEWS: cleared(first)})  # record commit
    # A later review that names the record commit as reviewed, tagged as is.
    second = run_git(root, "rev-parse", "HEAD")
    repo({REVIEWS: cleared(second)})
    assert release.main(["v0.4.2"], root) == 0


def test_code_changed_after_the_review_blocks(repo: Repo) -> None:
    root = repo()
    reviewed = run_git(root, "rev-parse", "HEAD")
    repo({"src/trakt_mcp/code.py": "x = 2\n", REVIEWS: cleared(reviewed)})
    with pytest.raises(release.ReleaseError, match="changed after the reviewed commit"):
        release.check(root, "v0.4.2")


def test_workflow_changed_after_the_review_blocks(repo: Repo) -> None:
    root = repo()
    reviewed = run_git(root, "rev-parse", "HEAD")
    repo({".github/workflows/x.yml": "on: push\n", REVIEWS: cleared(reviewed)})
    with pytest.raises(release.ReleaseError, match=r"\.github/workflows/x\.yml"):
        release.check(root, "v0.4.2")


def test_missing_review_blocks(repo: Repo) -> None:
    root = repo()
    with pytest.raises(release.ReleaseError, match=r"does not mark 0\.4\.2 as cleared"):
        release.check(root, "v0.4.2")


def test_pending_review_blocks(repo: Repo) -> None:
    root = repo()
    sha = run_git(root, "rev-parse", "HEAD")
    repo({REVIEWS: cleared(sha).replace("**cleared**", "pending")})
    with pytest.raises(release.ReleaseError, match="does not mark"):
        release.check(root, "v0.4.2")


def test_review_of_another_version_does_not_count(repo: Repo) -> None:
    root = repo()
    sha = run_git(root, "rev-parse", "HEAD")
    repo({REVIEWS: cleared(sha, version="0.4.1")})
    with pytest.raises(release.ReleaseError, match=r"does not mark 0\.4\.2"):
        release.check(root, "v0.4.2")


def test_reviewed_commit_must_be_an_ancestor(repo: Repo) -> None:
    root = repo()
    run_git(root, "checkout", "-q", "-b", "side")
    side = repo({"other.txt": "x"})
    side_sha = run_git(side, "rev-parse", "HEAD")
    run_git(root, "checkout", "-q", "main")
    repo({REVIEWS: cleared(side_sha)})
    with pytest.raises(release.ReleaseError, match="not an ancestor"):
        release.check(root, "v0.4.2")


def test_unknown_reviewed_commit_blocks(repo: Repo) -> None:
    root = repo({REVIEWS: cleared("deadbeef" * 5)})
    with pytest.raises(release.ReleaseError, match="rev-parse"):
        release.check(root, "v0.4.2")


def test_short_sha_is_not_accepted(repo: Repo) -> None:
    root = repo()
    sha = run_git(root, "rev-parse", "HEAD")
    repo({REVIEWS: cleared(sha).replace(sha, sha[:7])})
    with pytest.raises(release.ReleaseError, match="does not mark"):
        release.check(root, "v0.4.2")


def test_a_ref_named_like_the_reviewed_sha_cannot_redirect_the_check(repo: Repo) -> None:
    root = repo()
    reviewed = run_git(root, "rev-parse", "HEAD")
    run_git(root, "checkout", "-q", "-b", "side")
    unreviewed = run_git(repo({"src/trakt_mcp/code.py": "evil = 1\n"}), "rev-parse", "HEAD")
    run_git(root, "checkout", "-q", "main")
    # Refs literally named like the reviewed SHA, pointing at unreviewed code.
    run_git(root, "update-ref", f"refs/tags/{reviewed}", unreviewed)
    run_git(root, "update-ref", f"refs/heads/{reviewed}", unreviewed)
    repo({REVIEWS: cleared(reviewed)})
    # The gate still compares against the reviewed object itself, not the refs.
    assert release.check(root, "v0.4.2").endswith(f"cleared at {reviewed}.")
    assert (
        release.git(root, "rev-parse", "--verify", "--end-of-options", f"{reviewed}^{{commit}}")
        == reviewed
    )


def test_renaming_code_onto_another_path_is_still_seen(repo: Repo) -> None:
    root = repo({"src/trakt_mcp/guard.py": "def guard() -> None: ...\n"})
    reviewed = run_git(root, "rev-parse", "HEAD")
    run_git(root, "mv", "src/trakt_mcp/guard.py", "docs/other.md")
    run_git(root, "config", "diff.renames", "true")
    repo({REVIEWS: cleared(reviewed)})
    with pytest.raises(release.ReleaseError, match=r"src/trakt_mcp/guard\.py"):
        release.check(root, "v0.4.2")


@pytest.mark.parametrize("tag", ["v0.4.3", "0.4.2", "v0.4", "v0.4.2-rc1", "v0.4.2\n"])
def test_tag_must_match_the_version(repo: Repo, tag: str) -> None:
    root = repo()
    with pytest.raises(release.ReleaseError):
        release.check(root, tag)


def test_versions_must_agree(repo: Repo) -> None:
    root = repo({"src/trakt_mcp/__init__.py": '__version__ = "0.4.1"\n'})
    with pytest.raises(release.ReleaseError, match="does not match"):
        release.check(root, "v0.4.2")


@pytest.mark.parametrize(
    "compose",
    [
        "    image: ghcr.io/o/x:0.3\n",
        "    image: ghcr.io/o/x:latest\n",
        "    image: x:0.4\n",
        "    image: ghcr.io/o/x:0.4\n    image: ghcr.io/o/x:0.4\n",
    ],
)
def test_compose_example_must_use_this_minor(repo: Repo, compose: str) -> None:
    root = repo()
    repo({"compose.example.yml": compose})
    repo({REVIEWS: cleared(run_git(root, "rev-parse", "HEAD"))})
    with pytest.raises(release.ReleaseError, match=r"compose\.example\.yml"):
        release.check(root, "v0.4.2")


def test_main_reports_usage_and_errors(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    root = repo()
    assert release.main([], root) == 2
    assert release.main(["v0.4.2"], root) == 1
    assert "Release blocked" in capsys.readouterr().err
    assert release.main(["v0.4.2"], root / "missing") == 1
