"""Directory picker for `tam new --pick`."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tam import paths, state

HOME = str(Path.home())


def _short(path: str) -> str:
    return "~" + path[len(HOME) :] if path == HOME or path.startswith(HOME + "/") else path


def _claude_dirs() -> list[tuple[float, str]]:
    """(last used, cwd) for every directory Claude has run in."""
    from tam.adopt import transcript_info

    result = []
    for project in (paths.claude_dir() / "projects").glob("*"):
        transcripts = sorted(project.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not transcripts:
            continue
        newest = transcripts[-1]
        cwd, _ = transcript_info(newest)
        if cwd:
            result.append((newest.stat().st_mtime, cwd))
    return result


def _state_dirs() -> list[str]:
    saved = state.load() or {}
    return [p["cwd"] for s in saved.get("sessions", []) for w in s["windows"] for p in w["panes"]]


def _zoxide_dirs() -> list[str]:
    if not shutil.which("zoxide"):
        return []
    out = subprocess.run(["zoxide", "query", "-l"], capture_output=True, text=True).stdout
    return out.splitlines()


def candidates(current: str) -> list[str]:
    """Current directory first, then directories used with agents, most recent first."""
    ordered = [current]
    ordered += [cwd for _, cwd in sorted(_claude_dirs(), reverse=True)]
    ordered += _state_dirs()
    ordered += _zoxide_dirs()
    seen: set[str] = set()
    result = []
    for d in ordered:
        d = d.rstrip("/") or "/"
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        result.append(d)
    return result


def resolve(query: str, selection: str) -> str | None:
    """The directory to use from fzf's query and selected line."""
    if selection:
        return os.path.expanduser(selection)
    typed = os.path.expanduser(query.strip())
    return os.path.abspath(typed) if typed and os.path.isdir(typed) else None


def pick(current: str) -> str | None:
    lines = "\n".join(_short(d) for d in candidates(current))
    proc = subprocess.run(
        [
            "fzf",
            "--print-query",
            "--no-sort",
            "--prompt",
            "new claude in> ",
            "--header",
            "Enter: use directory · type a path to use one not listed · Esc: cancel",
        ],
        input=lines,
        stdout=subprocess.PIPE,
        text=True,
    )
    if proc.returncode not in (0, 1):  # 130: cancelled
        return None
    out = proc.stdout.split("\n")
    query = out[0] if out else ""
    selection = out[1] if len(out) > 1 else ""
    return resolve(query, selection)
