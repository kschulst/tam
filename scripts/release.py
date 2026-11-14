"""Make a release locally: `just release [patch|minor|major]`.

Commits follow Conventional Commits (`feat: ...`, `fix: ...`, `docs: ...`).
The next version follows from the commits since the last tag: a breaking
change (`feat!:` or `BREAKING CHANGE` in the body) gives a new major (a new
minor before 1.0), `feat` a new minor, `fix` and `perf` a new patch. Other
types (docs, chore, refactor, test, ...) make no release and are left out of
the notes. `patch`, `minor` or `major` overrides the computed version.

The script bumps __version__ in src/tam/__init__.py, puts the notes at the top
of CHANGELOG.md, commits `chore(release): X.Y.Z`, tags vX.Y.Z, pushes main and
the tag, creates a GitHub Release with the notes, and writes Formula/tam.rb in
the Homebrew tap from packaging/tam.rb.

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
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "kschulst/tam"
TAP = "kschulst/homebrew-tap"
VERSION_FILE = ROOT / "src" / "tam" / "__init__.py"
FORMULA = ROOT / "packaging" / "tam.rb"
VERSION = re.compile(r'(?m)^__version__ = "(\d+)\.(\d+)\.(\d+)"$')
CHANGELOG = ROOT / "CHANGELOG.md"
GROUPS = {"feat": "Features", "fix": "Fixes", "perf": "Performance"}
COMMIT = re.compile(r"^(?P<type>\w+)(?:\([^)]*\))?(?P<breaking>!)?: (?P<text>.+)$")


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


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str = ""

    @property
    def type(self) -> str | None:
        m = COMMIT.match(self.subject)
        return m.group("type") if m else None

    @property
    def text(self) -> str:
        m = COMMIT.match(self.subject)
        return m.group("text") if m else self.subject

    @property
    def breaking(self) -> bool:
        m = COMMIT.match(self.subject)
        return bool(m and m.group("breaking")) or "BREAKING CHANGE" in self.body


def next_version(previous: str, commits: list[Commit]) -> str | None:
    """The version after `previous`, or None if no commit calls for a release."""
    major, minor, patch = (int(n) for n in previous.split("."))
    if any(c.breaking for c in commits):
        return f"{major + 1}.0.0" if major >= 1 else f"{major}.{minor + 1}.0"
    if any(c.type == "feat" for c in commits):
        return f"{major}.{minor + 1}.0"
    if any(c.type in ("fix", "perf") for c in commits):
        return f"{major}.{minor}.{patch + 1}"
    return None


def notes(version: str, commits: list[Commit], day: date) -> str:
    lines = [f"## {version} ({day.isoformat()})", ""]
    breaking = [c for c in commits if c.breaking]
    if breaking:
        lines += ["### Breaking changes", "", *(f"- {c.text} ({c.sha[:7]})" for c in breaking), ""]
    for kind, title in GROUPS.items():
        chosen = [c for c in commits if c.type == kind and not c.breaking]
        if chosen:
            lines += [f"### {title}", "", *(f"- {c.text} ({c.sha[:7]})" for c in chosen), ""]
    return "\n".join(lines)


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


def _commits(since: str | None) -> list[Commit]:
    rng = [f"{since}..HEAD"] if since else ["HEAD"]
    out = _run("git", "log", "--format=%H%x1f%s%x1f%b%x1e", *rng)
    commits = []
    for entry in out.split("\x1e"):
        if entry.strip():
            sha, subject, body = (entry.strip("\n").split("\x1f") + ["", ""])[:3]
            commits.append(Commit(sha, subject, body))
    return commits


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
        # The clone is on the tap's default branch, which is what brew reads.
        _run("git", "push", "--quiet", "origin", "HEAD", cwd=clone)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Make a release of tam.")
    p.add_argument(
        "part",
        nargs="?",
        choices=("patch", "minor", "major"),
        help="part of the version to bump (default: computed from the commits)",
    )
    p.add_argument("--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = p.parse_args(argv)

    match = VERSION.search(VERSION_FILE.read_text())
    if not match:
        raise ReleaseError(f"No __version__ in {VERSION_FILE.name}.")
    current = ".".join(match.groups())
    last = _last_tag()
    commits = _commits(last)
    if args.part:
        version = bump(current, args.part)
    else:
        version = next_version(current, commits)
        if version is None and args.dry_run:
            print(f"{last}: nothing to release ({len(commits)} commits since, none feat/fix/perf)")
            return 0
        if version is None:
            raise ReleaseError(
                f"No feat, fix or perf commits since {last or 'the start'}. "
                "Pass patch, minor or major to release anyway."
            )
    tag = f"v{version}"
    body = notes(version, commits, date.today())

    print(f"{last or 'no release yet'} → {tag} ({len(commits)} commits since)\n")
    print(body)
    if args.dry_run:
        return 0
    _check_worktree()
    if not args.yes and input(f"\nRelease {tag}? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1

    VERSION_FILE.write_text(set_version(VERSION_FILE.read_text(), version))
    earlier = (
        CHANGELOG.read_text().removeprefix("# Changelog\n").lstrip("\n")
        if CHANGELOG.exists()
        else ""
    )
    CHANGELOG.write_text(f"# Changelog\n\n{body}\n{earlier}")
    _run("git", "add", str(VERSION_FILE), str(CHANGELOG))
    _run("git", "commit", "--quiet", "-m", f"chore(release): {version}")
    _run("git", "tag", "-a", tag, "-m", f"tam {version}")
    _run("git", "push", "--quiet", "--atomic", "origin", "main", tag)
    print(f"✓ pushed {tag}")

    # The release title already names the version, so the notes start below "## X.Y.Z".
    _run(
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        REPO,
        "--title",
        tag,
        "--notes",
        body.split("\n", 2)[2],
    )
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
