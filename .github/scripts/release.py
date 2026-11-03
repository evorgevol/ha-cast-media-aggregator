"""Work out the next release version and its release notes from git history.

The version bump follows Conventional Commits (https://www.conventionalcommits.org):
a breaking change (``feat!:`` or a ``BREAKING CHANGE:`` footer) bumps the major
version, ``feat:`` bumps the minor version and anything else bumps the patch version.
Only commits touching files that ship to users (see ``RELEASE_PATHS``) count.

Writes ``release``, ``version`` and ``tag`` to ``$GITHUB_OUTPUT`` (or stdout) and the
release notes to the file given by ``--notes-file``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys

RELEASE_PATHS = ("custom_components", "hacs.json")
INITIAL_VERSION = (0, 1, 0)
SKIP_MARKER = "[skip release]"

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
HEADER_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s*(?P<desc>.+)$"
)
BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)

SECTIONS = (
    ("breaking", "⚠️ Breaking changes"),
    ("feat", "Features"),
    ("fix", "Fixes"),
    ("perf", "Performance"),
    ("other", "Other changes"),
)

Version = tuple[int, int, int]


@dataclass(frozen=True)
class Commit:
    """A commit included in a release."""

    sha: str
    subject: str
    body: str = ""


@dataclass(frozen=True)
class ParsedCommit:
    """A commit classified by its Conventional Commit header."""

    sha: str
    section: str
    scope: str | None
    description: str
    breaking: bool


def parse_version(value: str, pattern: re.Pattern[str] = VERSION_RE) -> Version | None:
    """Parse ``X.Y.Z`` (optionally ``v``-prefixed) into a version tuple."""
    if not (match := pattern.match(value.strip())):
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def format_version(version: Version) -> str:
    """Format a version tuple as ``X.Y.Z``."""
    return ".".join(str(part) for part in version)


def parse_commit(commit: Commit) -> ParsedCommit:
    """Classify a commit into a release notes section."""
    subject = commit.subject.replace(SKIP_MARKER, "").strip()
    if not (match := HEADER_RE.match(subject)):
        return ParsedCommit(commit.sha, "other", None, subject, False)
    breaking = bool(match["breaking"]) or bool(BREAKING_FOOTER_RE.search(commit.body))
    commit_type = match["type"].lower()
    if breaking:
        section = "breaking"
    elif commit_type in ("feat", "fix", "perf"):
        section = commit_type
    else:
        section = "other"
    return ParsedCommit(
        commit.sha, section, match["scope"] or None, match["desc"].strip(), breaking
    )


def next_version(previous: Version | None, commits: list[ParsedCommit]) -> Version:
    """Return the version that follows ``previous`` given the released commits."""
    if previous is None:
        return INITIAL_VERSION
    major, minor, patch = previous
    if any(commit.breaking for commit in commits):
        return (major + 1, 0, 0)
    if any(commit.section == "feat" for commit in commits):
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def release_notes(
    commits: list[ParsedCommit],
    *,
    previous_tag: str | None,
    tag: str,
    repository: str | None,
) -> str:
    """Build markdown release notes grouped by section."""
    if previous_tag is None:
        return "Initial release.\n"
    lines: list[str] = []
    for section, title in SECTIONS:
        entries = [commit for commit in commits if commit.section == section]
        if not entries:
            continue
        lines.append(f"## {title}\n")
        lines.extend(
            f"- {f'**{entry.scope}:** ' if entry.scope else ''}"
            f"{entry.description} ({entry.sha[:7]})"
            for entry in entries
        )
        lines.append("")
    if not lines:
        lines.append("Maintenance release.\n")
    if repository:
        lines.append(
            f"**Full changelog:** https://github.com/{repository}/compare/{previous_tag}...{tag}"
        )
    return "\n".join(lines).rstrip() + "\n"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def latest_tag() -> tuple[str, Version] | None:
    """Return the highest ``vX.Y.Z`` tag and its version."""
    tags = [
        (version, tag)
        for tag in _git("tag", "--list", "v*").split()
        if (version := parse_version(tag, TAG_RE)) is not None
    ]
    if not tags:
        return None
    version, tag = max(tags)
    return tag, version


def commits_since(tag: str | None) -> list[Commit]:
    """Return non-merge commits since ``tag`` that touch released files, newest first."""
    revision = f"{tag}..HEAD" if tag else "HEAD"
    output = _git(
        "log",
        "--no-merges",
        "--format=%H%x1f%s%x1f%b%x1e",
        revision,
        "--",
        *RELEASE_PATHS,
    )
    commits = []
    for record in output.split("\x1e"):
        if not record.strip():
            continue
        sha, subject, body = record.strip("\n").split("\x1f", 2)
        commits.append(Commit(sha, subject, body))
    return commits


def main(argv: list[str] | None = None) -> int:
    """Compute the release and write outputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes-file", type=Path, required=True)
    parser.add_argument(
        "--version",
        default="",
        help="Release this X.Y.Z version instead of computing it.",
    )
    args = parser.parse_args(argv)

    previous_tag, previous_version = latest_tag() or (None, None)
    commits = [parse_commit(commit) for commit in commits_since(previous_tag)]

    release = True
    if args.version.strip():
        forced = parse_version(args.version)
        if forced is None:
            parser.error(f"Invalid version {args.version!r}, expected X.Y.Z")
        if previous_version is not None and forced <= previous_version:
            parser.error(f"Version {args.version} must be newer than {previous_tag}")
        version = forced
    else:
        version = next_version(previous_version, commits)
        if not commits:
            print("No released files changed since the last release.", file=sys.stderr)
            release = False
        elif SKIP_MARKER in _git("log", "-1", "--format=%B", "HEAD"):
            print(f"Latest commit contains {SKIP_MARKER}.", file=sys.stderr)
            release = False

    tag = f"v{format_version(version)}"
    if release:
        args.notes_file.write_text(
            release_notes(
                commits,
                previous_tag=previous_tag,
                tag=tag,
                repository=os.environ.get("GITHUB_REPOSITORY"),
            ),
            encoding="utf-8",
        )

    outputs = f"release={str(release).lower()}\nversion={format_version(version)}\ntag={tag}\n"
    if github_output := os.environ.get("GITHUB_OUTPUT"):
        with open(github_output, "a", encoding="utf-8") as file:
            file.write(outputs)
    else:
        sys.stdout.write(outputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
