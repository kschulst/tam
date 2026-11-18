"""The open pull request for a branch, from gh, cached so the sidebar never waits.

lookup() reads the cache and, when the entry is missing or older than TTL,
starts `tam sidebar --pr-refresh` in the background. That runs `gh pr list` and
writes the cache, also when there is no PR or gh fails, so a reload every few
seconds does not call GitHub. Without gh on PATH there are no PRs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from tam import paths

TTL = 300
# A refresh still running after this long is taken to have died.
PENDING_TIMEOUT = 60
# Branches a PR is not opened from, and a detached HEAD (a short commit).
_NO_PR = re.compile(r"main|master|[0-9a-f]{7}")


def _cache_dir() -> Path:
    return paths.state_dir() / "pr"


def _key(root: str, branch: str) -> str:
    return hashlib.sha1(f"{root}\0{branch}".encode()).hexdigest()


def _read(root: str, branch: str) -> dict | None:
    try:
        data = json.loads((_cache_dir() / f"{_key(root, branch)}.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lookup(root: str, branch: str, now: float | None = None) -> dict | None:
    """{"number", "url", "draft"} of the open PR for branch in the repo at root, or None."""
    if not root or not branch or _NO_PR.fullmatch(branch) or not shutil.which("gh"):
        return None
    now = time.time() if now is None else now
    data = _read(root, branch)
    if data is None or now - float(data.get("checked", 0)) > TTL:
        _refresh_in_background(root, branch, now)
    pr = data.get("pr") if data else None
    return pr if isinstance(pr, dict) else None


def _refresh_in_background(root: str, branch: str, now: float) -> None:
    pending = _cache_dir() / f"{_key(root, branch)}.pending"
    pending.parent.mkdir(parents=True, exist_ok=True)
    try:
        if now - pending.stat().st_mtime < PENDING_TIMEOUT:
            return
        pending.unlink()
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return
    subprocess.Popen(
        [paths.tam_executable(), "sidebar", "--pr-refresh", root, branch],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def refresh(root: str, branch: str) -> None:
    """Ask gh for the open PR from branch and write the cache."""
    pr = None
    try:
        proc = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number,url,isDraft",
                "--limit",
                "1",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        found = json.loads(proc.stdout) if proc.returncode == 0 else []
        if found:
            pr = {
                "number": int(found[0]["number"]),
                "url": str(found[0]["url"]),
                "draft": bool(found[0].get("isDraft")),
            }
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        pass
    key = _key(root, branch)
    cache = _cache_dir() / f"{key}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({"checked": time.time(), "pr": pr}))
    os.replace(tmp, cache)
    (_cache_dir() / f"{key}.pending").unlink(missing_ok=True)


def link(text: str, url: str) -> str:
    """text as an OSC 8 hyperlink: Cmd+click (iTerm2) opens url."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"
