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


def cycle_agent(state_file: str) -> str:
    """fzf action for Tab in the picker: switch to the next installed agent."""
    from tam.harness import installed_names

    f = Path(state_file)
    names = installed_names() or ["claude"]
    current = f.read_text().strip() if f.exists() else names[0]
    nxt = names[(names.index(current) + 1) % len(names)] if current in names else names[0]
    f.write_text(nxt)
    return f"change-prompt(new {nxt} in> )"


# Keys in the picker that choose where the new pane goes. Enter: a new window.
LAYOUT_KEYS = {"ctrl-v": "h", "ctrl-s": "v"}


def pick(current: str, agent: str | None) -> tuple[str, str | None, str | None] | None:
    """(directory, agent, split) or None if cancelled.

    agent None picks a directory for a plain terminal. split is None for a new
    window, "h" for a split to the right (Ctrl+v), "v" for one below (Ctrl+s).
    Tab switches the agent.
    """
    import shlex

    from tam.harness import installed_names

    state_file = paths.state_dir() / f"picker-agent-{os.getpid()}"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(agent or "")
    exe = shlex.quote(paths.tam_executable())
    what = agent or "terminal"
    header = "Enter: new window · Ctrl+v: split right · Ctrl+s: split below · Esc: cancel"
    bind = []
    if agent and len(installed_names()) > 1:
        header += "\nTab: switch agent (" + ", ".join(installed_names()) + ")"
        bind = ["--bind", f"tab:transform({exe} new --cycle-agent {shlex.quote(str(state_file))})"]
    header += "\nType a path to use a directory that is not listed"
    lines = "\n".join(_short(d) for d in candidates(current))
    try:
        proc = subprocess.run(
            [
                "fzf",
                "--print-query",
                "--no-sort",
                "--prompt",
                f"new {what} in> ",
                "--header",
                header,
                "--expect",
                ",".join(LAYOUT_KEYS),
                *bind,
            ],
            input=lines,
            stdout=subprocess.PIPE,
            text=True,
        )
        chosen_agent = (state_file.read_text().strip() or agent) if agent else None
    finally:
        state_file.unlink(missing_ok=True)
    if proc.returncode not in (0, 1):  # 130: cancelled
        return None
    # With --print-query and --expect: query, pressed key, selection.
    out = proc.stdout.split("\n") + ["", "", ""]
    query, key, selection = out[0], out[1], out[2]
    directory = resolve(query, selection)
    return (directory, chosen_agent, LAYOUT_KEYS.get(key)) if directory else None
