from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
release = importlib.util.module_from_spec(spec)
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


def test_notes_skip_release_commits():
    assert release.notes(["Add sidebar", "Release 0.1.1"]) == "- Add sidebar"
