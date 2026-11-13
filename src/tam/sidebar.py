"""`tam sidebar`: a session and window list in a popup along the left edge."""

from __future__ import annotations

import subprocess

from tam import paths
from tam.tmux import client_size, open_popup, query, tmux

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


def pick(client: str) -> int:
    rows = query(["list-panes", "-a"], FIELDS)
    cur = tmux("display-message", "-p", "-c", client, "#{session_name}\t#{window_index}")
    session, index = cur.strip().split("\t")
    items, selected = lines(rows, (session, index))
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
            "--bind",
            f"load:pos({selected + 1})",
        ],
        input="\n".join(items),
        stdout=subprocess.PIPE,
        text=True,
    )
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
