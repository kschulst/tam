"""`tam sidebar`: a session and window list in a pane along the left edge of each window.

The sidebar is shown or hidden for all windows at once (`sidebar` in config.toml,
mirrored in the global tmux option SHOWN_OPTION so hooks can read it). A sidebar
pane is recognised by its start command, so it is known before its process runs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tam import paths
from tam.tmux import global_option, query, session_names, tmux

WIDTH = 44
PANE_COMMAND = "sidebar --pane"
SHOWN_OPTION = "@tam_sidebar_shown"
# Set on the sidebar pane by its own process; automatic-rename-format reads it.
PANE_OPTION = "@tam_sidebar"
# How often a visible sidebar redraws, for agent status set by hooks.
REFRESH_SECONDS = 2

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
    "window_activity",
    "session_attached",
    "pane_active",
    "pane_title",
    "host_short",
    "pane_current_command",
    "pane_current_path",
    "pane_start_command",
    "pane_tty",
    "automatic-rename",
    "@tam_harness",
    "@tam_status",
]

# A window named after one of these, or a pane running one in front, is a shell
# waiting for input: the card shows its directory instead.
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "nu"}

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
# Room for the text on a card: fzf's pointer, gutter and scrollbar, and the indent.
LINE_WIDTH = WIDTH - 3
TEXT_WIDTH = LINE_WIDTH - 3


def is_sidebar(start_command: str) -> bool:
    return PANE_COMMAND in start_command


def window_status(statuses: list[str]) -> str:
    for s in STATUS:
        if s in statuses:
            return s
    return ""


def ago(seconds: int) -> str:
    if seconds < 60:
        return "now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "now"


def branch(path: str) -> str:
    """The git branch checked out in path (short commit when detached), or ""."""
    if not path:
        return ""
    for d in (Path(path), *Path(path).parents):
        git = d / ".git"
        if git.is_file():  # a worktree or submodule: "gitdir: <path>"
            text = git.read_text().strip()
            if not text.startswith("gitdir:"):
                return ""
            git = (d / text.removeprefix("gitdir:").strip()).resolve()
        if git.is_dir():
            try:
                head = (git / "HEAD").read_text().strip()
            except OSError:
                return ""
            return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:7]
    return ""


def title(raw: str, host: str = "") -> str:
    """What the pane's program says it is doing, from the pane title.

    Drops the status glyph agents put in front (Claude: ✳, ⠂, ...). The titles
    tmux and shells set themselves (the host name, user@host:dir) say nothing
    about the window and give "".
    """
    if host and (raw == host or raw.startswith(f"{host}.") or f"@{host}" in raw):
        return ""
    words = raw.split(maxsplit=1)
    if len(words) == 2 and len(words[0]) == 1 and not words[0].isalnum():
        return words[1]
    return raw


def _fit(text: str, width: int = TEXT_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def short_path(path: str, width: int = TEXT_WIDTH) -> str:
    """path with ~ for home, cut from the left (…/src/tam) when it is too long."""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        path = "~" + path[len(home) :]
    if len(path) <= width:
        return path
    parts = path.split(os.sep)
    while len(parts) > 1 and len("…/" + os.sep.join(parts)) > width:
        parts.pop(0)
    return _fit("…/" + os.sep.join(parts), width)


def foreground_commands() -> dict[str, str]:
    """tty name (ttys003) -> command line of the first process in the foreground there.

    tmux gives only the program name of a pane (node, vim); ps gives its arguments.
    One ps call covers every pane.
    """
    proc = subprocess.run(
        ["ps", "-A", "-o", "tty=,stat=,args="], capture_output=True, text=True, check=False
    )
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and "+" in parts[1] and parts[0] not in out:
            out[parts[0]] = parts[2]
    return out


def _program(args: str) -> str:
    """The command line with only the program's name, not its path: node vite, -zsh -> zsh."""
    first, _, rest = args.partition(" ")
    name = os.path.basename(first).lstrip("-")
    return f"{name} {rest}".strip()


def _windows(rows: list[dict[str, str]]) -> dict[str, dict[str, dict]]:
    """session -> window index -> window, in tmux order."""
    sessions: dict[str, dict[str, dict]] = {}
    for r in rows:
        if is_sidebar(r.get("pane_start_command", "")):
            continue
        windows = sessions.setdefault(r["session_name"], {})
        w = windows.setdefault(
            r["window_index"],
            {
                "name": r["window_name"],
                "auto_named": r.get("automatic-rename") == "1",
                "statuses": [],
                "pane": None,
                "agent_pane": None,
                "panes": 0,
            },
        )
        w["panes"] += 1
        if r.get("@tam_harness"):
            w["statuses"].append(r.get("@tam_status") or "running")
            w["agent_pane"] = w["agent_pane"] or r
        if r.get("pane_active") == "1" or w["pane"] is None:
            w["pane"] = r
    return sessions


def _terminal_what(pane: dict[str, str], commands: dict[str, str]) -> str:
    """The command line running in a terminal pane, or its directory when the shell waits."""
    running = commands.get(os.path.basename(pane.get("pane_tty", "")), "")
    command = _program(running) if running else pane.get("pane_current_command", "")
    if not command or command.split()[0] in SHELLS:
        return short_path(pane.get("pane_current_path", ""))
    return command


def card(w: dict, now: int, commands: dict[str, str] | None = None) -> str:
    """Three lines for a window: name, what runs in it, status · panes · age · branch.

    commands is foreground_commands(); without it, terminals show tmux's program name.
    """
    pane = w["agent_pane"] or w["pane"]
    path = pane.get("pane_current_path", "")
    name = w["name"]
    if w.get("auto_named") and name in SHELLS and path:
        name = os.path.basename(path) if path != str(Path.home()) else "~"
    if w["agent_pane"]:
        status = window_status(w["statuses"])
        mark, label = STATUS[status]
        raw = pane.get("pane_title", "")
        what = title(raw, pane.get("host_short", "")) or pane.get("@tam_harness", "")
    else:
        mark, label = f"{DIM}›{RESET}", "terminal"
        what = _terminal_what(pane, commands or {})
    activity = pane.get("window_activity", "")
    meta = [label]
    if w.get("panes", 1) > 1:
        meta.append(f"{w['panes']} panes")
    if activity.isdigit():
        meta.append(ago(max(0, now - int(activity))))
    b = branch(path)
    if b:
        meta.append(b)
    return (
        f" {mark} {BOLD}{_fit(name)}{RESET}\n"
        f"   {_fit(what)}\n"
        f"   {DIM}{_fit(' · '.join(meta))}{RESET}"
    )


def lines(
    rows: list[dict[str, str]],
    current: tuple[str, str],
    now: int | None = None,
    commands: dict[str, str] | None = None,
) -> tuple[list[str], int]:
    """fzf items (`target<TAB>text`, the text on several lines) and the current window's index."""
    import time

    now = int(time.time()) if now is None else now
    commands = foreground_commands() if commands is None else commands
    out: list[str] = []
    selected = 0
    for session, windows in _windows(rows).items():
        out.append(f"={session}:\t{session_line(session)}")
        for index, w in sorted(windows.items(), key=lambda kv: int(kv[0])):
            if (session, index) == current:
                selected = len(out)
            out.append(f"={session}:{index}\t{card(w, now, commands)}")
    return out, selected


TERMINAL_BUTTON = "+ terminal"


def session_line(session: str) -> str:
    """The session's line: its name, and on the right the button a click on the line runs."""
    name = _fit(session.upper(), LINE_WIDTH - 1 - len(TERMINAL_BUTTON))
    gap = " " * max(1, LINE_WIDTH - len(name) - len(TERMINAL_BUTTON))
    return f"{DIM}{name}{RESET}{gap}{TERMINAL_BUTTON}"


def header(rows: list[dict[str, str]]) -> str:
    """Windows in total, agents working, agents waiting for you, and the keys."""
    windows = [w for ws in _windows(rows).values() for w in ws.values()]
    statuses = [window_status(w["statuses"]) for w in windows if w["agent_pane"]]
    return (
        f"{len(windows)} total · {statuses.count('working')} working · "
        f"{statuses.count('waiting')} waiting\n"
        f"{DIM}Ctrl+r rename · Ctrl+x close{RESET}"
    )


def line_click(client: str, target: str, exe: str) -> str:
    """fzf actions for a click on a line: a session's line opens a terminal.

    fzf has already moved the cursor to the clicked line; a window's line needs
    nothing more.
    """
    if not target.endswith(":"):
        return ""
    terminal_in_session(client, target[1:-1])
    return _reload(client, exe)


def terminal_in_session(client: str, session: str) -> None:
    """A shell in a new window in the agent's directory, right after the agent's window.

    The agent is the one in the session's current window, else the first one in
    the session. Without agents, the current window's active pane is used. The
    client moves to the new window.
    """
    rows = query(
        ["list-panes", "-s", "-t", f"={session}"],
        [
            "window_id",
            "window_active",
            "pane_active",
            "pane_current_path",
            "pane_start_command",
            "@tam_harness",
        ],
    )
    panes = [r for r in rows if not is_sidebar(r["pane_start_command"])]
    if not panes:
        return
    agents = [r for r in panes if r["@tam_harness"]]
    current = [r for r in panes if r["window_active"] == "1"]
    pane = (
        next((r for r in agents if r["window_active"] == "1"), None)
        or (agents[0] if agents else None)
        or next((r for r in current if r["pane_active"] == "1"), None)
        or panes[0]
    )
    new = tmux(
        "new-window",
        "-a",
        "-t",
        pane["window_id"],
        "-c",
        pane["pane_current_path"] or str(Path.home()),
        "-P",
        "-F",
        "#{window_id}",
    ).strip()
    tmux("switch-client", *_client(client), "-t", new, check=False)


def pane_rows() -> list[dict[str, str]]:
    return query(["list-panes", "-a"], FIELDS)


def pane_mode() -> bool:
    """True in the fzf actions of a sidebar pane (set by run_pane)."""
    return os.environ.get("TAM_SIDEBAR_PANE") == "1"


def _client(client: str) -> list[str]:
    return ["-c", client] if client else []


def _current(client: str) -> tuple[str, str]:
    """The window the sidebar belongs to: its own pane's window, or the client's."""
    where = ["-t", os.environ["TMUX_PANE"]] if pane_mode() else _client(client)
    cur = tmux("display-message", "-p", *where, "#{session_name}\t#{window_index}")
    session, index = cur.strip().split("\t")
    return session, index


def list_lines(client: str) -> tuple[list[str], int]:
    return lines(pane_rows(), _current(client))


def _reload(client: str, exe: str) -> str:
    """fzf actions that redraw the list and the header after a change."""
    import shlex

    exe = shlex.quote(exe)
    c = f" --client {shlex.quote(client)}" if client else ""
    return f"reload-sync({exe} sidebar --list{c})+transform-header({exe} sidebar --header)"


# Renaming: Ctrl+r switches the prompt to RENAME_PROMPT, turns search off (typing
# would otherwise filter the focused line away) and fills in the current name.
# Enter and Esc then check the prompt, which fzf passes as $FZF_PROMPT.
PROMPT = "> "
RENAME_PROMPT = "new name> "
_SEARCH_MODE = f"change-prompt({PROMPT})+enable-search+clear-query"


def _name(target: str) -> str:
    fmt = "#{session_name}" if target.endswith(":") else "#{window_name}"
    return tmux("display-message", "-p", "-t", target, fmt, check=False).strip()


def rename_start(target: str) -> str:
    """fzf actions for Ctrl+r."""
    return (
        f"change-prompt({RENAME_PROMPT})+disable-search"
        f"+change-footer([ Enter: rename {_describe(target)} · Esc: cancel ])"
        f"+change-query:{_name(target)}"
    )


def rename_mode() -> bool:
    return os.environ.get("FZF_PROMPT") == RENAME_PROMPT


def _back_to_window() -> None:
    """Move focus from the sidebar pane to the pane that had it before."""
    pane = os.environ["TMUX_PANE"]
    tmux("last-pane", "-t", pane, check=False)
    active = tmux("display-message", "-p", "-t", pane, "#{pane_active}", check=False)
    if active.strip() == "1":  # no last pane yet
        tmux("select-pane", "-R", "-t", pane, check=False)


def enter(client: str, target: str, name: str, exe: str) -> str:
    """fzf actions for Enter: rename in rename mode, otherwise go to the line.

    In a sidebar pane, the line of the sidebar's own window moves focus back into it.
    """
    if not rename_mode():
        if not pane_mode():
            return "accept"
        if target:
            session, _, index = target[1:].partition(":")
            if (session, index) == _current(client):
                _back_to_window()
            else:
                tmux("switch-client", *_client(client), "-t", target, check=False)
        return "clear-query"
    name = name.strip()
    if name and target:
        if target.endswith(":"):
            tmux("rename-session", "-t", target[:-1], "--", name, check=False)
        else:
            tmux("rename-window", "-t", target, "--", name, check=False)
    return f"{_SEARCH_MODE}+{_reload(client, exe)}"


def escape() -> str:
    """fzf actions for Esc: leave rename mode, otherwise leave the sidebar."""
    if rename_mode():
        return _SEARCH_MODE
    if pane_mode():
        _back_to_window()
        return "clear-query"
    return "abort"


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
    return _reload(client, exe)


def close(client: str, target: str) -> None:
    from tam import state

    session = target[1:].split(":", 1)[0]
    if target.endswith(":"):
        if _current(client)[0] == session:
            # tmux detaches a client whose session is destroyed; move it first.
            others = sorted(n for n in session_names() if n != session)
            if others:
                tmux("switch-client", *_client(client), "-t", f"={others[0]}:", check=False)
        tmux("kill-session", "-t", f"={session}", check=False)
    else:
        tmux("kill-window", "-t", target, check=False)
    state.snapshot()


# The sidebar pane

# Redraw only while the window is on screen. A sidebar in a window nobody looks
# at costs one tmux call per tick.
_VISIBLE = (
    '[ "$(tmux display-message -p -t "$TMUX_PANE"'
    " '#{&&:#{window_active},#{session_attached}}')\" = 1 ]"
)


def fzf_args(exe: str, selected: int, header_text: str) -> list[str]:
    import shlex

    close_action = f"transform({exe} sidebar --click {{1}})"
    enter_action = f"transform({exe} sidebar --enter {{1}} --name={{q}})"
    refresh = f"{_VISIBLE} && echo {shlex.quote(_reload('', exe))} || true"
    return [
        "fzf",
        "--ansi",
        "--read0",
        "--no-sort",
        "--layout=reverse",
        "--delimiter",
        "\t",
        "--with-nth",
        "2..",
        # Keep the cursor on the same line when the list is reloaded.
        "--track",
        "--id-nth",
        "1",
        "--prompt",
        PROMPT,
        "--info",
        "hidden",
        "--no-separator",
        "--header",
        header_text,
        "--header-first",
        "--gap",
        "--gap-line",
        " ",
        "--gutter",
        " ",
        "--highlight-line",
        "--pointer",
        "▌",
        "--footer",
        " ",
        "--bind",
        f"load:pos({selected + 1})+unbind(load)",
        "--bind",
        f"focus:transform-footer({exe} sidebar --footer {{1}})",
        "--bind",
        f"click-footer:{close_action}",
        "--bind",
        f"left-click:transform({exe} sidebar --line-click {{1}})",
        "--bind",
        f"ctrl-x:{close_action}",
        "--bind",
        f"ctrl-r:transform({exe} sidebar --rename {{1}})",
        "--bind",
        f"enter:{enter_action}",
        "--bind",
        f"double-click:{enter_action}",
        "--bind",
        f"esc:transform({exe} sidebar --escape)",
        # The pane stays until the sidebar is hidden or the window closes.
        "--bind",
        "ctrl-c:ignore,ctrl-g:ignore,ctrl-q:ignore",
        "--bind",
        f"every({REFRESH_SECONDS}):transform:{refresh}",
    ]


def _rename_format() -> str:
    """automatic-rename-format that keeps the window name while the sidebar has focus."""
    fmt = global_option("automatic-rename-format") or "#{pane_current_command}"
    return f"#{{?#{{{PANE_OPTION}}},#{{window_name}},{fmt}}}"


def run_pane() -> int:
    """The process in a sidebar pane: fzf until the pane is closed."""
    import shlex
    import sys

    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        print("tam: tam sidebar --pane runs inside a tmux pane", file=sys.stderr)
        return 1
    tmux("set-option", "-p", "-t", pane, PANE_OPTION, "1", check=False)
    tmux("set-option", "-w", "-t", pane, "automatic-rename-format", _rename_format(), check=False)
    os.environ["TAM_SIDEBAR_PANE"] = "1"
    rows = pane_rows()
    items, selected = lines(rows, _current(""))
    exe = shlex.quote(paths.tam_executable())
    subprocess.run(fzf_args(exe, selected, header(rows)), input="\0".join(items), text=True)
    return 0


# Showing and hiding


def shown() -> bool:
    from tam import config

    return config.get("sidebar") != "hidden"


def sync_option() -> None:
    """Mirror the setting in config.toml to the tmux option the hooks read."""
    tmux("set-option", "-g", SHOWN_OPTION, "1" if shown() else "0")


def _by_window() -> dict[str, dict[str, list[dict[str, str]]]]:
    """window id -> {"panes": [...], "sidebars": [...]}."""
    rows = query(["list-panes", "-a"], ["window_id", "pane_id", "pane_width", "pane_start_command"])
    windows: dict[str, dict[str, list[dict[str, str]]]] = {}
    for r in rows:
        w = windows.setdefault(r["window_id"], {"panes": [], "sidebars": []})
        w["sidebars" if is_sidebar(r["pane_start_command"]) else "panes"].append(r)
    return windows


def ensure() -> None:
    """Make every window match the setting: one sidebar WIDTH columns wide, or none.

    A window with only a sidebar left is closed. Runs from tmux hooks, which can
    fire at the same time, so it holds a lock.
    """
    import fcntl

    lock = paths.state_dir() / "sidebar.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    on = shown()
    exe = paths.tam_executable()
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        for window, w in _by_window().items():
            if not w["panes"]:
                tmux("kill-window", "-t", window, check=False)
                continue
            keep = w["sidebars"][:1] if on else []
            for extra in w["sidebars"][len(keep) :]:
                tmux("kill-pane", "-t", extra["pane_id"], check=False)
            if not on:
                tmux("set-option", "-wu", "-t", window, "automatic-rename-format", check=False)
            elif not keep:
                tmux(
                    "split-window",
                    "-hbfd",
                    "-l",
                    str(WIDTH),
                    "-t",
                    window,
                    f"{exe} {PANE_COMMAND}",
                    check=False,
                )
            elif keep[0]["pane_width"] != str(WIDTH):
                tmux("resize-pane", "-t", keep[0]["pane_id"], "-x", str(WIDTH), check=False)


def toggle() -> None:
    """Show the sidebar in every window, or hide it everywhere."""
    from tam import config

    config.set("sidebar", "hidden" if shown() else "shown")
    sync_option()
    ensure()


def focus(client: str) -> None:
    """Move focus to the sidebar of the client's window, or back from it.

    A hidden sidebar is shown first.
    """
    if not shown():
        toggle()
    window = tmux("display-message", "-p", *_client(client), "#{window_id}").strip()
    sidebars = _by_window().get(window, {}).get("sidebars", [])
    if not sidebars:
        return
    pane = sidebars[0]["pane_id"]
    if tmux("display-message", "-p", "-t", pane, "#{pane_active}").strip() == "1":
        tmux("last-pane", "-t", window, check=False)
    else:
        tmux("select-pane", "-t", pane, check=False)
