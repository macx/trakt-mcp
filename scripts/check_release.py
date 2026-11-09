"""Release gate: may the checked-out commit be released under this tag?

Run by the release workflow on every pushed ``v*`` tag (and usable locally)::

    python3 scripts/check_release.py v0.4.2

It fails unless

1. the tag matches the version in ``pyproject.toml`` and ``src/trakt_mcp/__init__.py``,
2. ``docs/security-reviews.md`` marks this version **cleared** with the reviewed
   commit, that commit is an ancestor of the tagged one, and nothing but
   ``docs/security-reviews.md`` changed in between (so the record can be committed
   after the review, but no unreviewed code can slip in), and
3. ``compose.example.yml`` points at this version's ``major.minor`` image tag.

The gate runs from the tagged commit, so it cannot protect against a tag on a
commit that weakens the gate itself; that is what the GitHub tag rulesets are for
(only admins create ``v*`` tags, none can be moved or deleted). It prevents
mistakes, not a malicious maintainer.

Standard library only, and Python 3.11+ compatible, so it runs on a bare runner.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404
import sys
import tomllib
from pathlib import Path

REVIEWS = "docs/security-reviews.md"
# | 0.4.2 | 2026-11-09 | scope | **cleared** (reviewed commit `<full 40-char SHA>`) |
_ROW = re.compile(
    r"^\|\s*(?P<version>[0-9][^|\s]*)\s*\|[^\n]*?\|\s*\*\*cleared\*\*\s*"
    r"\(reviewed commit `(?P<sha>[0-9a-f]{40})`\)\s*\|\s*$",
    re.MULTILINE,
)
_IMAGE = re.compile(r"^\s*image:\s*ghcr\.io/[^\s:]+:(?P<tag>\S+)\s*$", re.MULTILINE)


class ReleaseError(Exception):
    pass


def git(root: Path, *args: str) -> str:
    """Run a fixed git subcommand (no shell); only commit IDs vary."""
    command = ["git", *args]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)  # noqa: S603  # nosec B603
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def project_versions(root: Path) -> tuple[str, str]:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    init = (root / "src/trakt_mcp/__init__.py").read_text()
    match = re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE)
    return pyproject, match.group(1) if match else ""


def cleared_commit(reviews: str, version: str) -> str | None:
    for match in _ROW.finditer(reviews):
        if match["version"] == version:
            return match["sha"]
    return None


def check(root: Path, tag: str) -> str:
    """Return a success message, or raise :class:`ReleaseError`."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ReleaseError(f"Tag {tag!r} is not of the form vX.Y.Z.")
    version = tag.removeprefix("v")
    pyproject, init = project_versions(root)
    if pyproject != version or init != version:
        raise ReleaseError(
            f"Tag {tag} does not match pyproject.toml ({pyproject}) "
            f"and src/trakt_mcp/__init__.py ({init})."
        )

    sha = cleared_commit((root / REVIEWS).read_text(), version)
    if sha is None:
        raise ReleaseError(
            f"{REVIEWS} does not mark {version} as cleared. Every release needs an "
            "independent security review first (CLAUDE.md, 'Releases')."
        )
    # A full SHA, resolved as an object (never as a ref: a tag named like a short
    # SHA would otherwise win) and checked to be exactly the recorded commit.
    reviewed = git(root, "rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}")
    if reviewed != sha:
        raise ReleaseError(f"Reviewed commit {sha} resolves to {reviewed}.")
    head = git(root, "rev-parse", "HEAD")
    try:
        git(root, "merge-base", "--is-ancestor", reviewed, head)
    except ReleaseError:
        raise ReleaseError(f"Reviewed commit {sha} is not an ancestor of {tag}.") from None
    diff = git(root, "diff", "--name-only", "--no-renames", reviewed, head)
    changed = set(filter(None, diff.splitlines()))
    if changed - {REVIEWS}:
        raise ReleaseError(
            f"Files changed after the reviewed commit {sha}: {', '.join(sorted(changed))}. "
            "Only the review record may change after the review."
        )

    minor = ".".join(version.split(".")[:2])
    images = _IMAGE.findall((root / "compose.example.yml").read_text())
    if images != [minor]:
        raise ReleaseError(f"compose.example.yml must use the image tag {minor!r}.")
    return f"{tag}: versions match, security review cleared at {sha}."


def main(argv: list[str], root: Path | None = None) -> int:
    if len(argv) != 1:
        print("usage: check_release.py vX.Y.Z", file=sys.stderr)
        return 2
    try:
        print(check(root or Path.cwd(), argv[0]))
    except (ReleaseError, OSError, KeyError) as exc:
        print(f"Release blocked: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
