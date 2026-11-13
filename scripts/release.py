"""Make a release locally: `just release [patch|minor|major]`.

Bumps __version__ in src/tam/__init__.py, commits, tags vX.Y.Z, pushes main and
the tag, creates a GitHub Release with the commit subjects since the last tag,
and writes Formula/tam.rb in the Homebrew tap from packaging/tam.rb.

    uv run python scripts/release.py [patch|minor|major] [--dry-run] [--yes]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "kschulst/tam"
TAP = "kschulst/homebrew-tap"
VERSION_FILE = ROOT / "src" / "tam" / "__init__.py"
FORMULA = ROOT / "packaging" / "tam.rb"
VERSION = re.compile(r'(?m)^__version__ = "(\d+)\.(\d+)\.(\d+)"$')


class ReleaseError(Exception):
    pass


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(n) for n in version.split("."))
    return {
        "major": f"{major + 1}.0.0",
        "minor": f"{major}.{minor + 1}.0",
        "patch": f"{major}.{minor}.{patch + 1}",
    }[part]


def set_version(text: str, version: str) -> str:
    new, count = VERSION.subn(f'__version__ = "{version}"', text, count=1)
    if not count:
        raise ReleaseError(f"No __version__ in {VERSION_FILE.name}.")
    return new


def formula(template: str, version: str, sha256: str) -> str:
    for placeholder in ("@VERSION@", "@SHA256@"):
        if placeholder not in template:
            raise ReleaseError(f"No {placeholder} in {FORMULA.name}.")
    return template.replace("@VERSION@", version).replace("@SHA256@", sha256)


def notes(subjects: list[str]) -> str:
    lines = [s for s in subjects if not s.startswith("Release ")]
    return "\n".join(f"- {s}" for s in lines) or "- No changes listed"


def _run(*args: str, cwd: Path = ROOT) -> str:
    res = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        raise ReleaseError(f"`{' '.join(args)}` failed. {(res.stderr or res.stdout).strip()}")
    return res.stdout.strip()


def _last_tag() -> str | None:
    res = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v*"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip() if res.returncode == 0 else None


def _subjects(since: str | None) -> list[str]:
    rng = [f"{since}..HEAD"] if since else ["HEAD"]
    out = _run("git", "log", "--format=%s", *rng)
    return [line for line in out.splitlines() if line]


def _check_worktree() -> None:
    if _run("git", "branch", "--show-current") != "main":
        raise ReleaseError("Releases are made from main.")
    if _run("git", "status", "--porcelain"):
        raise ReleaseError("The working tree has uncommitted changes.")
    _run("git", "fetch", "--quiet", "origin", "main")
    if _run("git", "rev-parse", "HEAD") != _run("git", "rev-parse", "origin/main"):
        raise ReleaseError("main differs from origin/main. Pull or push first.")


def _tarball_sha256(tag: str) -> str:
    url = f"https://github.com/{REPO}/archive/refs/tags/{tag}.tar.gz"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return hashlib.sha256(resp.read()).hexdigest()
        except OSError:
            time.sleep(2 * (attempt + 1))
    raise ReleaseError(f"Could not download {url}.")


def _update_tap(version: str, sha256: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "tap"
        _run("gh", "repo", "clone", TAP, str(clone), "--", "--quiet", "--depth", "1")
        target = clone / "Formula" / "tam.rb"
        target.parent.mkdir(exist_ok=True)
        text = formula(FORMULA.read_text(), version, sha256)
        # The template's own header comment is not part of the formula.
        target.write_text(text[text.index("class Tam") :])
        _run("git", "add", "Formula/tam.rb", cwd=clone)
        _run("git", "commit", "--quiet", "-m", f"tam {version}", cwd=clone)
        _run("git", "push", "--quiet", "origin", "HEAD:main", cwd=clone)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Make a release of tam.")
    p.add_argument("part", nargs="?", default="patch", choices=("patch", "minor", "major"))
    p.add_argument("--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = p.parse_args(argv)

    match = VERSION.search(VERSION_FILE.read_text())
    if not match:
        raise ReleaseError(f"No __version__ in {VERSION_FILE.name}.")
    current = ".".join(match.groups())
    last = _last_tag()
    subjects = _subjects(last)
    version = bump(current, args.part)
    tag = f"v{version}"
    body = notes(subjects)

    print(f"{last or 'no release yet'} → {tag} ({len(subjects)} commits since)\n")
    print(body)
    if args.dry_run:
        return 0
    _check_worktree()
    if not args.yes and input(f"\nRelease {tag}? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1

    VERSION_FILE.write_text(set_version(VERSION_FILE.read_text(), version))
    _run("git", "add", str(VERSION_FILE))
    _run("git", "commit", "--quiet", "-m", f"Release {version}")
    _run("git", "tag", "-a", tag, "-m", f"tam {version}")
    _run("git", "push", "--quiet", "--atomic", "origin", "main", tag)
    print(f"✓ pushed {tag}")

    _run("gh", "release", "create", tag, "--repo", REPO, "--title", tag, "--notes", body)
    print("✓ GitHub Release")

    _update_tap(version, _tarball_sha256(tag))
    print(f"✓ Homebrew formula in {TAP}")
    print("\nUpgrade this machine with: just install")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReleaseError as e:
        print(f"release: {e}", file=sys.stderr)
        sys.exit(1)
