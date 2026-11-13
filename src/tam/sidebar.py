"""`tam sidebar`: a session and window list in a popup along the left edge."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tam import paths
from tam.tmux import client_size, open_popup, query, session_names, tmux

WIDTH = 44

# Agent status, set by tam run (running, exited, stopped) and by agent hooks
# (working, waiting). Order is urgency: the most urgent pane decides a window's status.
STATUS = {
    "waiting": ("\033[33m○\033[0m", "waiting"),
    "working": ("\033[32m●\033[0m", "working"),
    "running": ("\033[32m●\033[0m", "running"),
    "exited": ("\033[2m◌\033[0m", "exited"),
    "stopped": ("\033[2m◌\033[0m", "stopped"),
}

FIELDS = [
    "session_name",
    "window_index",
    "window_name",
    "window_active",
    "session_attached",
    "pane_active",
    "@tam_harness",
    "@tam_status",
]


def window_status(statuses: list[str]) -> str:
    for s in STATUS:
        if s in statuses:
            return s
    return ""


def lines(rows: list[dict[str, str]], current: tuple[str, str]) -> tuple[list[str], int]:
    """fzf input lines (`target<TAB>text`) and the index of the current window."""
    sessions: dict[str, dict[str, dict]] = {}
    for r in rows:
        windows = sessions.setdefault(r["session_name"], {})
        w = windows.setdefault(
            r["window_index"], {"name": r["window_name"], "statuses": [], "agent": False}
        )
        if r["@tam_harness"]:
            w["agent"] = True
            w["statuses"].append(r["@tam_status"] or "running")

    out: list[str] = []
    selected = 0
    for session, windows in sessions.items():
        out.append(f"={session}:\t\033[1m{session}\033[0m")
        for index, w in sorted(windows.items(), key=lambda kv: int(kv[0])):
            if (session, index) == current:
                selected = len(out)
            status = window_status(w["statuses"]) if w["agent"] else ""
            mark, label = STATUS.get(status, (" ", ""))
            name = w["name"][: WIDTH - 16]
            out.append(f"={session}:{index}\t {mark} {name:<{WIDTH - 16}} {label}")
    return out, selected


def _current(client: str) -> tuple[str, str]:
    cur = tmux("display-message", "-p", "-c", client, "#{session_name}\t#{window_index}")
    session, index = cur.strip().split("\t")
    return session, index


def list_lines(client: str) -> tuple[list[str], int]:
    return lines(query(["list-panes", "-a"], FIELDS), _current(client))


# Closing from the sidebar: the footer is a button that closes the focused line.
# The first click (or Ctrl+x) arms it, the second closes. Moving to another line
# disarms it. The armed target is kept in a file because each fzf action runs a
# new process.


def _armed_file() -> Path:
    return paths.state_dir() / "sidebar-armed"


def _describe(target: str) -> str:
    if target.endswith(":"):
        session = target[1:-1]
        n = len(tmux("list-windows", "-t", target[:-1], "-F", "x", check=False).split())
        return f"session {session} ({n} window{'s' if n != 1 else ''})"
    return tmux("display-message", "-p", "-t", target, "#{window_name}", check=False).strip()


def footer(target: str) -> str:
    """Footer text for the focused line. Also disarms the button."""
    _armed_file().unlink(missing_ok=True)
    return f"[ ✕ close {_describe(target)} ]  (or Ctrl+x)"


def click(client: str, target: str, exe: str) -> str:
    """fzf actions for a click on the close button."""
    armed = _armed_file()
    if not armed.exists() or armed.read_text() != target:
        armed.parent.mkdir(parents=True, exist_ok=True)
        armed.write_text(target)
        return f"change-footer([ ✕ click again to close {_describe(target)} ])"
    armed.unlink()
    close(client, target)
    return f"reload({exe} sidebar --list --client {client})"


def close(client: str, target: str) -> None:
    from tam import state

    session = target[1:].split(":", 1)[0]
    if target.endswith(":"):
        if _current(client)[0] == session:
            # tmux detaches a client whose session is destroyed; move it first.
            others = sorted(n for n in session_names() if n != session)
            if others:
                tmux("switch-client", "-c", client, "-t", f"={others[0]}:", check=False)
        tmux("kill-session", "-t", f"={session}", check=False)
    else:
        tmux("kill-window", "-t", target, check=False)
    state.snapshot()


def pick(client: str) -> int:
    import shlex

    items, selected = list_lines(client)
    exe = shlex.quote(paths.tam_executable())
    c = shlex.quote(client)
    close_action = f"transform({exe} sidebar --click {{1}} --client {c})"
    proc = subprocess.run(
        [
            "fzf",
            "--ansi",
            "--no-sort",
            "--layout=reverse",
            "--delimiter",
            "\t",
            "--with-nth",
            "2..",
            "--prompt",
            "> ",
            "--info",
            "hidden",
            "--no-separator",
            "--footer",
            " ",
            "--bind",
            f"load:pos({selected + 1})+unbind(load)",
            "--bind",
            f"focus:transform-footer({exe} sidebar --footer {{1}})",
            "--bind",
            f"click-footer:{close_action}",
            "--bind",
            f"ctrl-x:{close_action}",
        ],
        input="\n".join(items),
        stdout=subprocess.PIPE,
        text=True,
    )
    _armed_file().unlink(missing_ok=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return 0
    target = proc.stdout.split("\t", 1)[0]
    tmux("switch-client", "-c", client, "-t", target)
    return 0


def popup(client: str) -> int:
    _, rows = client_size(client)
    open_popup(
        client,
        WIDTH,
        rows,
        [paths.tam_executable(), "sidebar", "--pick", "--client", client],
        title="sessions",
        x=0,
        y=0,
    )
    return 0
