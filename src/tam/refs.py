"""Open GitHub issues and pull requests an agent pane has shown, for its sidebar card.

lookup() reads a per-pane cache. When it is older than SCAN_TTL, a background
`tam sidebar --refs-refresh <pane>` reads the pane's scrollback, collects the
issue and PR links in it (newest mention first) and asks gh for the state of
each, so closed issues and closed or merged PRs are left out. States are cached
per link for STATE_TTL. Without gh on PATH there are none.
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

SCAN_TTL = 30
STATE_TTL = 300
PENDING_TIMEOUT = 60
SCROLLBACK = 3000
MAX = 3
LINK = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/(pull|issues)/(\d+)")


def _dir() -> Path:
    return paths.state_dir() / "refs"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _pane_file(pane: str) -> Path:
    return _dir() / f"pane-{pane.lstrip('%')}.json"


def lookup(pane: str, now: float | None = None) -> list[dict]:
    """[{"url", "number", "kind": "pr"|"issue"}] open ones the pane has shown, newest first."""
    if not pane or not shutil.which("gh"):
        return []
    now = time.time() if now is None else now
    data = _read(_pane_file(pane))
    if data is None or now - float(data.get("scanned", 0)) > SCAN_TTL:
        _refresh_in_background(pane, now)
    refs = data.get("refs") if data else None
    return [r for r in refs if isinstance(r, dict)] if isinstance(refs, list) else []


def _refresh_in_background(pane: str, now: float) -> None:
    pending = _pane_file(pane).with_suffix(".pending")
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
        [paths.tam_executable(), "sidebar", "--refs-refresh", pane],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def links_in(text: str) -> list[tuple[str, str, str, int]]:
    """(owner, repo, "pr"|"issue", number) for each link in text, newest (last) first."""
    seen: dict[tuple[str, str, str, int], None] = {}
    for m in reversed(list(LINK.finditer(text))):
        owner, repo, kind, number = m.groups()
        key = (owner, repo, "pr" if kind == "pull" else "issue", int(number))
        seen.setdefault(key, None)
    return list(seen)


def _state(owner: str, repo: str, number: int, now: float) -> str:
    """ "open" or "closed" (a merged PR is closed), cached; "" when gh cannot tell."""
    key = hashlib.sha1(f"{owner}/{repo}#{number}".encode()).hexdigest()
    cache = _dir() / f"state-{key}.json"
    data = _read(cache)
    if data and now - float(data.get("checked", 0)) <= STATE_TTL:
        return str(data.get("state", ""))
    state = ""
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/issues/{number}", "--jq", ".state"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode == 0:
            state = proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    _write(cache, {"checked": now, "state": state})
    return state


def refresh(pane: str) -> None:
    """Scan the pane's scrollback and write its open issues and PRs (the background part)."""
    from tam.tmux import tmux

    now = time.time()
    text = tmux("capture-pane", "-p", "-J", "-S", f"-{SCROLLBACK}", "-t", pane, check=False)
    refs = []
    for owner, repo, kind, number in links_in(text):
        if len(refs) == MAX:
            break
        if _state(owner, repo, number, now) == "open":
            path = "pull" if kind == "pr" else "issues"
            url = f"https://github.com/{owner}/{repo}/{path}/{number}"
            refs.append({"url": url, "number": number, "kind": kind})
    _write(_pane_file(pane), {"scanned": now, "refs": refs})
    _pane_file(pane).with_suffix(".pending").unlink(missing_ok=True)
