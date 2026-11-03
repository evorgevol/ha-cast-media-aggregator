"""Tests for the release script that versions HACS releases."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "release.py"
_spec = importlib.util.spec_from_file_location("release_script", SCRIPT)
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
sys.modules["release_script"] = release
_spec.loader.exec_module(release)


def parsed(subject: str, body: str = "", sha: str = "abcdef1234") -> object:
    return release.parse_commit(release.Commit(sha, subject, body))


def test_parse_commit_sections() -> None:
    assert parsed("feat: add thing").section == "feat"
    assert parsed("fix(tmdb): handle 401").scope == "tmdb"
    assert parsed("perf: faster").section == "perf"
    assert parsed("docs: tweak").section == "other"
    assert parsed("refactor: tidy [skip release]").description == "tidy"
    other = parsed("Update readme")
    assert (other.section, other.description, other.breaking) == (
        "other",
        "Update readme",
        False,
    )


def test_parse_commit_breaking() -> None:
    assert parsed("feat!: drop option").breaking
    assert parsed("refactor(cache)!: new layout").section == "breaking"
    footer = parsed("fix: rename", "Details\n\nBREAKING CHANGE: attribute renamed")
    assert footer.breaking
    assert not parsed("fix: mention BREAKING CHANGE: inline").breaking


def test_next_version() -> None:
    assert release.next_version(None, []) == release.INITIAL_VERSION
    assert release.next_version((1, 4, 2), [parsed("fix: x")]) == (1, 4, 3)
    assert release.next_version((1, 4, 2), [parsed("fix: x"), parsed("feat: y")]) == (
        1,
        5,
        0,
    )
    assert release.next_version((1, 4, 2), [parsed("feat!: z")]) == (2, 0, 0)


def test_parse_version() -> None:
    assert release.parse_version("1.2.3") == (1, 2, 3)
    assert release.parse_version("v1.2.3") == (1, 2, 3)
    assert release.parse_version("1.2") is None
    assert release.parse_version("1.2.3", release.TAG_RE) is None


def test_release_notes_grouped() -> None:
    commits = [
        parsed("chore: tidy", sha="1111111aaa"),
        parsed("fix(tmdb): handle 401", sha="2222222bbb"),
        parsed("feat: add posters", sha="3333333ccc"),
        parsed("feat!: drop legacy option", sha="4444444ddd"),
    ]
    notes = release.release_notes(
        commits, previous_tag="v1.0.0", tag="v2.0.0", repository="owner/repo"
    )
    assert notes == (
        "## ⚠️ Breaking changes\n\n"
        "- drop legacy option (4444444)\n\n"
        "## Features\n\n"
        "- add posters (3333333)\n\n"
        "## Fixes\n\n"
        "- **tmdb:** handle 401 (2222222)\n\n"
        "## Other changes\n\n"
        "- tidy (1111111)\n\n"
        "**Full changelog:** https://github.com/owner/repo/compare/v1.0.0...v2.0.0\n"
    )


def test_release_notes_initial_and_empty() -> None:
    assert (
        release.release_notes([], previous_tag=None, tag="v0.1.0", repository="o/r")
        == "Initial release.\n"
    )
    assert (
        release.release_notes([], previous_tag="v0.1.0", tag="v0.2.0", repository=None)
        == "Maintenance release.\n"
    )
