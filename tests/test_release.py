from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
release = importlib.util.module_from_spec(spec)
sys.modules["release"] = release  # dataclasses look the module up here
spec.loader.exec_module(release)


def test_bump():
    assert release.bump("0.1.0", "patch") == "0.1.1"
    assert release.bump("0.1.3", "minor") == "0.2.0"
    assert release.bump("0.9.2", "major") == "1.0.0"


def test_set_version():
    assert release.set_version('__version__ = "0.1.0"\n', "0.2.0") == '__version__ = "0.2.0"\n'
    with pytest.raises(release.ReleaseError):
        release.set_version("nothing here\n", "0.2.0")


def test_formula_template_fills_in_and_keeps_ruby_interpolation():
    text = release.formula((ROOT / "packaging" / "tam.rb").read_text(), "0.2.0", "ab" * 32)
    assert 'url "https://github.com/kschulst/tam/archive/refs/tags/v0.2.0.tar.gz"' in text
    assert f'sha256 "{"ab" * 32}"' in text
    assert '#{formula_opt_bin("python@3.13")}' in text
    assert "@VERSION@" not in text and "@SHA256@" not in text


def test_version_file_matches_pattern():
    assert release.VERSION.search((ROOT / "src" / "tam" / "__init__.py").read_text())


def C(subject, body="", sha="abcdef1234"):  # noqa: N802
    return release.Commit(sha, subject, body)


def test_next_version_from_commit_types():
    assert release.next_version("0.2.0", [C("docs: readme"), C("chore: justfile")]) is None
    assert release.next_version("0.2.0", [C("fix: crash"), C("docs: x")]) == "0.2.1"
    assert release.next_version("0.2.0", [C("fix: crash"), C("feat(picker): splits")]) == "0.3.0"
    assert release.next_version("0.2.0", [C("feat!: new keys")]) == "0.3.0"  # before 1.0
    assert release.next_version("1.2.0", [C("fix: x", "BREAKING CHANGE: y")]) == "2.0.0"


def test_notes_group_user_facing_commits_only():
    from datetime import date

    commits = [
        C("feat: ⌥ t opens a terminal", sha="1111111aaa"),
        C("fix(sidebar): close button", sha="2222222bbb"),
        C("docs: explain resurrect"),
        C("chore: justfile"),
        C("Old style subject"),
    ]
    text = release.notes("0.3.0", commits, date(2026, 11, 14))
    assert text.splitlines()[0] == "## 0.3.0 (2026-11-14)"
    assert "### Features\n\n- ⌥ t opens a terminal (1111111)" in text
    assert "### Fixes\n\n- close button (2222222)" in text
    assert "resurrect" not in text and "justfile" not in text and "Old style" not in text
