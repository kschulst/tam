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


def _state(owner: str, repo: str, number: int, now: float) -> tuple[str, str]:
    """(state, kind): ("open" | "closed", "pr" | "issue"), cached; ("", "") when gh cannot
    tell, e.g. no such issue. A merged PR is closed."""
    key = hashlib.sha1(f"{owner}/{repo}#{number}".lower().encode()).hexdigest()
    cache = _dir() / f"state-{key}.json"
    data = _read(cache)
    # An entry without "kind" is from an older tam and is asked again.
    if data and "kind" in data and now - float(data.get("checked", 0)) <= STATE_TTL:
        return str(data.get("state", "")), str(data.get("kind", ""))
    state = kind = ""
    try:
        proc = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{owner}/{repo}/issues/{number}",
                "--jq",
                '.state + " " + (if .pull_request then "pr" else "issue" end)',
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode == 0:
            state, _, kind = proc.stdout.strip().partition(" ")
    except (OSError, subprocess.TimeoutExpired):
        pass
    _write(cache, {"checked": now, "state": state, "kind": kind})
    return state, kind


# A reference in an agent's title: "app-platform-k8s sak 16941", "issue 40", "repo#12".
_TITLE_REF = re.compile(
    r"(?:\b([\w.-]+)\s+)?\b(?:sak|issue|pr)\s*#?(\d+)\b|(?:\b([\w.-]+))?#(\d+)\b", re.I
)
_GITHUB_REMOTE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")


def title_refs(title: str) -> list[tuple[str, int]]:
    """(repo name or "", number) for each reference in an agent's title."""
    out = []
    for m in _TITLE_REF.finditer(title):
        repo = m.group(1) or m.group(3) or ""
        number = m.group(2) or m.group(4)
        out.append((repo, int(number)))
    return out


def github_repos(paths_: list[str]) -> list[tuple[str, str]]:
    """(owner, repo) of the GitHub remotes of the git repositories at these paths, in order."""
    found: dict[tuple[str, str], None] = {}
    for path in paths_:
        proc = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True
        )
        m = _GITHUB_REMOTE.search(proc.stdout.strip()) if proc.returncode == 0 else None
        if m:
            found.setdefault((m.group(1), m.group(2)), None)
    return list(found)


def _candidates(repo: str, repos: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Where a title's reference may point: the named repo under each known owner, or,
    without a name, the agent's own repository (the first of repos)."""
    if not repo:
        return repos[:1]
    owners = list(dict.fromkeys(owner for owner, _ in repos))
    return [(owner, repo) for owner in owners]


# Bytes read from the end of a Claude transcript.
TRANSCRIPT_BYTES = 1024 * 1024


def claude_transcript(pane: str) -> Path | None:
    """The transcript of the Claude session in pane, if one runs there.

    The id is the pane's @tam_session_id (tam started it), else that of the Claude
    process on the pane's tty (Claude Code's ~/.claude/sessions/<pid>.json).
    """
    from tam.tmux import tmux

    out = tmux(
        "display-message",
        "-p",
        "-t",
        pane,
        "#{@tam_harness}\t#{@tam_session_id}\t#{pane_tty}",
        check=False,
    ).rstrip("\n")  # empty fields lead
    harness, sid, tty = (out.split("\t") + ["", "", ""])[:3]
    if harness != "claude" or not sid:
        from tam.adopt import claude_registry

        registry = claude_registry()
        procs = subprocess.run(
            ["ps", "-o", "pid=,tty=", "-p", ",".join(map(str, registry)) or "0"],
            capture_output=True,
            text=True,
        ).stdout
        tty = tty.removeprefix("/dev/")
        on_tty = [
            int(p)
            for p, t in (line.split() for line in procs.splitlines() if line.strip())
            if t == tty
        ]
        sid = next((registry[p]["session_id"] for p in on_tty if p in registry), "")
    if not sid:
        return None
    return next((paths.claude_dir() / "projects").glob(f"*/{sid}.jsonl"), None)


def _tail(path: Path, size: int) -> str:
    try:
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - size))
            return f.read().decode(errors="ignore")
    except OSError:
        return ""


def refresh(pane: str) -> None:
    """Scan the pane's scrollback and title; write its open issues and PRs (in the background).

    Claude Code draws on the alternate screen, which has no scrollback, so for a
    Claude pane the end of its transcript is read too: the links Claude wrote and
    the PRs it opened (`pr-link`). The transcript counts as newer than the screen.
    """
    from tam.tmux import tmux

    now = time.time()
    text = tmux("capture-pane", "-p", "-J", "-S", f"-{SCROLLBACK}", "-t", pane, check=False)
    transcript = claude_transcript(pane)
    if transcript:
        text += "\n" + _tail(transcript, TRANSCRIPT_BYTES)
    found = [(o, r, n) for o, r, _, n in links_in(text)]
    title = tmux("display-message", "-p", "-t", pane, "#{pane_title}", check=False).strip()
    references = title_refs(title)
    if references:
        # The agent's own repository first, then those of the other panes.
        own = tmux("display-message", "-p", "-t", pane, "#{pane_current_path}", check=False)
        others = tmux("list-panes", "-a", "-F", "#{pane_current_path}", check=False).split("\n")
        repos = github_repos([own.strip(), *dict.fromkeys(p for p in others if p)])
        for repo, number in references:
            for owner, name in _candidates(repo, repos):
                if _state(owner, name, number, now)[0]:  # exists there
                    found.insert(0, (owner, name, number))
                    break
    refs: list[dict] = []
    for owner, repo, number in found:
        if len(refs) == MAX:
            break
        state, kind = _state(owner, repo, number, now)
        if state != "open":
            continue
        url = f"https://github.com/{owner}/{repo}/{'pull' if kind == 'pr' else 'issues'}/{number}"
        # GitHub's owner and repo names ignore case: App-platform-k8s is app-platform-k8s.
        if all(r["url"].lower() != url.lower() for r in refs):
            refs.append({"url": url, "number": number, "kind": kind})
    _write(_pane_file(pane), {"scanned": now, "refs": refs})
    _pane_file(pane).with_suffix(".pending").unlink(missing_ok=True)
