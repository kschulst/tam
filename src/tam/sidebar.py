"""`tam sidebar`: a session and window list in a pane along the left edge of each window.

The sidebar is shown or hidden for all windows at once (`sidebar` in config.toml,
mirrored in the global tmux option SHOWN_OPTION so hooks can read it). A sidebar
pane is recognised by its start command, so it is known before its process runs.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from tam import paths
from tam.pr import link
from tam.pr import lookup as pr_lookup
from tam.tmux import global_option, query, session_names, tmux

WIDTH = 44
PANE_COMMAND = "sidebar --pane"
SHOWN_OPTION = "@tam_sidebar_shown"
# Set on the sidebar pane by its own process; automatic-rename-format reads it.
PANE_OPTION = "@tam_sidebar"
# How often a visible sidebar redraws, for agent status set by hooks.
REFRESH_SECONDS = 2

# Agent status, set by tam run (running, exited, stopped) and by agent hooks
# (working, waiting, idle; see setup.CLAUDE_HOOKS). Order is urgency: the most
# urgent pane decides a window's status. waiting means the agent cannot go on
# without you, and its card stands out.
WAITING = "\033[1;33m"
STATUS = {
    "waiting": (f"{WAITING}◆\033[0m", "waiting for you"),
    "working": ("\033[32m●\033[0m", "working"),
    "running": ("\033[32m●\033[0m", "running"),
    "idle": ("\033[2m○\033[0m", "idle"),
    "exited": ("\033[2m◌\033[0m", "exited"),
    "stopped": ("\033[2m◌\033[0m", "stopped"),
}

FIELDS = [
    "session_name",
    "window_index",
    "window_id",
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
    "pane_id",
    "@tam_harness",
    "@tam_status",
    "@tam_named",
]
# Set on a window renamed with Ctrl+r: its card keeps that name instead of the
# agent's topic. Saved in state.json.
NAMED_OPTION = "@tam_named"

# A window named after one of these, or a pane running one in front, is a shell
# waiting for input: the card shows its directory instead.
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "nu"}

DIM = "\033[2m"
BOLD = "\033[1m"
ACCENT = "\033[36m"  # the active window's bar and name
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
    return repo_branch(path)[1]


def repo_branch(path: str) -> tuple[str, str]:
    """(the work tree's top directory, its branch) for path, or ("", "") outside git."""
    if not path:
        return "", ""
    for d in (Path(path), *Path(path).parents):
        git = d / ".git"
        try:
            if git.is_file():  # a worktree or submodule: "gitdir: <path>"
                text = git.read_text().strip()
                if not text.startswith("gitdir:"):
                    return "", ""
                git = (d / text.removeprefix("gitdir:").strip()).resolve()
            if git.is_dir():
                head = (git / "HEAD").read_text().strip()
                ref = head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:7]
                return str(d), ref
        except OSError:
            return "", ""
    return "", ""


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


def _agent_programs() -> dict[str, str]:
    from tam.harness import programs

    global _PROGRAMS
    if _PROGRAMS is None:
        _PROGRAMS = programs()
    return _PROGRAMS


_PROGRAMS: dict[str, str] | None = None


def _windows(rows: list[dict[str, str]]) -> dict[str, dict[str, dict]]:
    """session -> window index -> window, in tmux order.

    A pane counts as an agent when tam started it (@tam_harness), or when a known
    agent's program runs in it: a `claude` started by hand gets an agent's card
    too, with the status its hooks set.
    """
    sessions: dict[str, dict[str, dict]] = {}
    for r in rows:
        if is_sidebar(r.get("pane_start_command", "")):
            continue
        if not r.get("@tam_harness"):
            found = _agent_programs().get(r.get("pane_current_command", ""))
            if found:
                r = {**r, "@tam_harness": found}
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
                "id": r.get("window_id", ""),
                "named": r.get(NAMED_OPTION) == "1",
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


def card(
    w: dict,
    now: int,
    commands: dict[str, str] | None = None,
    pr_of: Callable[[str, str], dict | None] | None = None,
    active: bool = False,
    refs_of: Callable[[str], list[dict]] | None = None,
) -> str:
    """A window's card: name, what runs in it, status · panes · age · branch, and links.

    An agent's card is named after the topic its title gives (Claude keeps it up to
    date), unless the window was renamed with Ctrl+r. The links line lists up to
    refs.MAX open PRs and issues: the branch's PR (pr_of, i.e. pr.lookup), then
    the ones the agent has shown (refs_of, i.e. refs.lookup). Cmd+click opens one.

    The active window (the one the sidebar is in) has a coloured bar and name; the
    marked line, which the arrow keys move, is fzf's highlighted line.

    commands is foreground_commands(); without it, terminals show tmux's program name.
    """
    pane = w["agent_pane"] or w["pane"]
    path = pane.get("pane_current_path", "")
    name = w["name"]
    if w.get("auto_named") and name in SHELLS and path:
        name = os.path.basename(path) if path != str(Path.home()) else "~"
    root, b = repo_branch(path)
    found: list[dict] = []
    pr = pr_of(root, b) if pr_of and b else None
    if pr:
        found.append({"url": pr["url"], "number": pr["number"], "kind": "pr"})
    if refs_of and w["agent_pane"]:
        found += refs_of(pane.get("pane_id", ""))
    links_line, linked = _links_line(found)
    if w["agent_pane"]:
        status = window_status(w["statuses"])
        mark, label = STATUS[status]
        topic = title(pane.get("pane_title", ""), pane.get("host_short", ""))
        harness = pane.get("@tam_harness", "")
        if topic.lower() == harness.lower():  # the program's name, not yet a topic
            topic = ""
        topic = _without_refs(topic, linked)
        if topic and not w.get("named"):
            name, what = topic, f"{harness} · {short_path(path, TEXT_WIDTH - len(harness) - 3)}"
        else:
            what = topic or harness
    else:
        mark, label = f"{DIM}›{RESET}", "terminal"
        what = _terminal_what(pane, commands or {})
    activity = pane.get("window_activity", "")
    meta = [label]
    if w.get("panes", 1) > 1:
        meta.append(f"{w['panes']} panes")
    if activity.isdigit():
        meta.append(ago(max(0, now - int(activity))))
    if b:
        meta.append(b)
    edge = f"{ACCENT}┃{RESET}" if active else " "
    waiting = w["agent_pane"] and label == STATUS["waiting"][1]
    name_style = f"{BOLD}{ACCENT}" if active else BOLD
    meta_text = _fit(" · ".join(meta))
    if waiting:  # the status in yellow, the rest dim
        meta_line = f"{WAITING}{meta_text[: len(label)]}{RESET}{DIM}{meta_text[len(label) :]}"
    else:
        meta_line = f"{DIM}{meta_text}"
    out = (
        f"{edge}{mark} {name_style}{_fit(name)}{RESET}\n"
        f"{edge}  {_fit(what)}\n"
        f"{edge}  {meta_line}{RESET}"
    )
    return f"{out}\n{edge}  {links_line}" if links_line else out


UNDERLINE, NO_UNDERLINE = "\033[4m", "\033[24m"
# "sak 16941", "issue #40", "PR 12": a reference in an agent's title.
_REF_IN_TEXT = re.compile(r"\s*\b(?:sak|issue|pr|pull request)\s*#?(\d+)\b|\s*#(\d+)\b", re.I)


def _links_line(found: list[dict]) -> tuple[str, set[int]]:
    """The card's links line, `Issue: #40, #41  PR: #12`, and the numbers on it.

    Up to refs.MAX distinct links, as many as fit; each number is an underlined
    OSC 8 hyperlink.
    """
    from tam.refs import MAX

    chosen: list[dict] = []
    for ref in found:
        if len(chosen) == MAX or any(c["url"].lower() == ref["url"].lower() for c in chosen):
            continue
        trial = chosen + [ref]
        if len(_links_text(trial, plain=True)) > TEXT_WIDTH:
            break
        chosen = trial
    return _links_text(chosen), {int(c["number"]) for c in chosen}


def _links_text(chosen: list[dict], plain: bool = False) -> str:
    groups = []
    for kind, label in (("issue", "Issue"), ("pr", "PR")):
        refs = [c for c in chosen if (c.get("kind") == "pr") == (kind == "pr")]
        if not refs:
            continue
        numbers = [
            f"#{c['number']}"
            if plain
            else link(f"{UNDERLINE}#{c['number']}{NO_UNDERLINE}", c["url"])
            for c in refs
        ]
        groups.append(f"{label}: " + ", ".join(numbers))
    return "  ".join(groups)


def _without_refs(topic: str, numbers: set[int]) -> str:
    """topic without references to the issues and PRs the links line already has."""
    if not numbers:
        return topic

    def drop(m: re.Match) -> str:
        return "" if int(m.group(1) or m.group(2)) in numbers else m.group(0)

    return _REF_IN_TEXT.sub(drop, topic).strip()


def lines(
    rows: list[dict[str, str]],
    current: tuple[str, str],
    now: int | None = None,
    commands: dict[str, str] | None = None,
    pr_of: Callable[[str, str], dict | None] | None = None,
    refs_of: Callable[[str], list[dict]] | None = None,
) -> tuple[list[str], int]:
    """fzf items (`target<TAB>text`, the text on several lines) and the current window's index.

    commands, pr_of and refs_of default to foreground_commands(), pr.lookup and
    refs.lookup.
    """
    import time

    from tam import refs

    now = int(time.time()) if now is None else now
    commands = foreground_commands() if commands is None else commands
    pr_of = pr_lookup if pr_of is None else pr_of
    refs_of = refs.lookup if refs_of is None else refs_of
    out: list[str] = []
    selected = 0
    for session, windows in _windows(rows).items():
        out.append(f"={session}:\t{session_line(session)}")
        for index, w in sorted(windows.items(), key=lambda kv: int(kv[0])):
            active = (session, index) == current
            if active:
                selected = len(out)
            # The window id stays right when windows are added or moved; a list
            # reloaded every few seconds can be older than the indexes.
            target = w.get("id") or f"={session}:{index}"
            out.append(f"{target}\t{card(w, now, commands, pr_of, active, refs_of)}")
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
    waiting = statuses.count("waiting")
    waiting_text = f"{WAITING}{waiting} waiting{RESET}" if waiting else "0 waiting"
    return (
        f"{len(windows)} total · {statuses.count('working')} working · "
        f"{waiting_text}\n"
        f"{DIM}Ctrl+r rename · Ctrl+x close{RESET}"
    )


def line_click(client: str, target: str, exe: str) -> str:
    """fzf actions for a click on a line (fzf has already moved the cursor there).

    A window's card goes to the window. A session's line only moves the cursor:
    its + terminal is handled by mouse(), which knows where on the line the click
    was. While renaming, a click only moves the cursor.
    """
    if rename_mode() or target.endswith(":"):
        return ""
    _go(client, target)
    return ""


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


def _go(client: str, target: str) -> None:
    """Switch to target; the sidebar's own window moves focus back into that window."""
    if pane_mode() and _is_own_window(target):
        _back_to_window()
    else:
        tmux("switch-client", *_client(client), "-t", target, check=False)


def _is_own_window(target: str) -> bool:
    """True if target (a window id, or =session:index) is the sidebar pane's window."""
    own = tmux(
        "display-message",
        "-p",
        "-t",
        os.environ["TMUX_PANE"],
        "#{window_id}\t=#{session_name}:#{window_index}",
    )
    return target in own.strip().split("\t")


def enter(client: str, target: str, name: str, exe: str) -> str:
    """fzf actions for Enter: rename in rename mode, otherwise go to the line.

    In a sidebar pane, the line of the sidebar's own window moves focus back into it.
    """
    if not rename_mode():
        if not pane_mode():
            return "accept"
        if target:
            _go(client, target)
        return "clear-query"
    name = name.strip()
    if name and target:
        if target.endswith(":"):
            tmux("rename-session", "-t", target[:-1], "--", name, check=False)
        else:
            tmux("rename-window", "-t", target, "--", name, check=False)
            tmux("set-option", "-w", "-t", target, NAMED_OPTION, "1", check=False)
    return f"{_SEARCH_MODE}+{_reload(client, exe)}"


def escape() -> str:
    """fzf actions for Esc: leave rename mode, otherwise leave the sidebar."""
    if rename_mode():
        return _SEARCH_MODE
    if pane_mode():
        _back_to_window()
        return "clear-query"
    return "abort"


# Closing from the sidebar: Ctrl+x on the focused line arms, a second Ctrl+x
# closes. Moving to another line disarms it. The armed target is kept in a file
# because each fzf action runs a new process. Outside the sidebar, ⌥ w closes
# the current window (tmux's confirm-before, see setup.key_bindings).


def _armed_file() -> Path:
    return paths.state_dir() / "sidebar-armed"


def _armed() -> str:
    try:
        return _armed_file().read_text()
    except OSError:
        return ""


def _describe(target: str) -> str:
    if target.endswith(":"):
        session = target[1:-1]
        n = len(tmux("list-windows", "-t", target[:-1], "-F", "x", check=False).split())
        return f"session {session} ({n} window{'s' if n != 1 else ''})"
    return tmux("display-message", "-p", "-t", target, "#{window_name}", check=False).strip()


def footer(target: str) -> str:
    """Footer text for the focused line (empty). Moving to another line disarms closing."""
    if _armed() != target:
        _armed_file().unlink(missing_ok=True)
    return " "


def click(client: str, target: str, exe: str) -> str:
    """fzf actions for Ctrl+x: arm on the first, close on the second."""
    armed = _armed_file()
    if _armed() != target:
        armed.parent.mkdir(parents=True, exist_ok=True)
        armed.write_text(target)
        return f"change-footer([ ✕ Ctrl+x again to close {_describe(target)} ])"
    armed.unlink()
    close(client, target)
    return _reload(client, exe)


def close(client: str, target: str) -> None:
    """Close a window or session (`=name:`).

    The other sidebars get the list without it first, so the window tmux moves
    the client to does not show it. A sidebar closing it from its own fzf
    (Ctrl+x) reloads itself afterwards.
    """
    from tam import state

    if shown():
        own = os.environ.get("TMUX_PANE", "") if pane_mode() else ""
        refresh(skip=target, besides=own)
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
    # In the background, so fzf does not block clicks and keys while the list is
    # built; it then reloads only when something changed (see tick).
    refresh = f"{_VISIBLE} && {exe} sidebar --tick || true"
    # The footer only says something while Ctrl+x is armed; no Python otherwise.
    armed = shlex.quote(str(_armed_file()))
    footer_text = f"[ -e {armed} ] && {exe} sidebar --footer {{1}} || echo ' '"
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
        f"focus:bg-transform-footer:{footer_text}",
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
        f"every({REFRESH_SECONDS}):bg-transform:{refresh}",
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
    args = fzf_args(exe, selected, header(rows)) + [f"--listen={socket_path(pane)}"]
    subprocess.run(args, input="\0".join(items), text=True)
    for f in _tick_files(pane):
        f.unlink(missing_ok=True)
    return 0


def _tick_files(pane: str) -> tuple[Path, Path]:
    """Where tick keeps the last list and header it gave this sidebar."""
    d = paths.state_dir() / "sidebar"
    name = pane.lstrip("%")
    return d / f"{name}.list", d / f"{name}.header"


def tick() -> str:
    """The visible sidebar's refresh tick: refresh every sidebar (see refresh).

    Runs as fzf's bg-transform, so building the lists does not block the sidebar.
    The tick covers what no tmux hook reports: agent status, titles, time since
    activity, links.
    """
    refresh()
    return ""


def _skipped(rows: list[dict[str, str]], skip: str) -> list[dict[str, str]]:
    """rows without the window or session (`=name:`) skip names, which is about to close."""
    if not skip:
        return rows
    if skip.endswith(":"):
        session = skip.removeprefix("=")[:-1]
        return [r for r in rows if r["session_name"] != session]
    window = tmux("display-message", "-p", "-t", skip, "#{window_id}", check=False).strip()
    return [r for r in rows if r["window_id"] != window]


def refresh(skip: str = "", besides: str = "") -> None:
    """Give every sidebar the current list, also those in windows nobody looks at.

    The data is gathered once; each sidebar gets its own rendering, with its own
    window as the current one, and a reload over its socket only when that
    changed. A window you switch to then already shows the current list.

    Runs from tmux hooks and from the tick, often several at once: one refresh
    runs at a time, and requests meanwhile make it run once more. skip leaves out
    a window or session that is being closed (see close). besides is a sidebar
    pane that is sent nothing: one whose fzf is waiting for this process.
    """
    import fcntl

    lock = paths.state_dir() / "sidebar-refresh.lock"
    again = lock.with_suffix(".again")
    lock.parent.mkdir(parents=True, exist_ok=True)
    while True:
        with open(lock, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                again.touch()  # the running refresh goes once more
                return
            again.unlink(missing_ok=True)
            _refresh_once(skip, besides)
            skip = besides = ""
        if not again.exists():
            return


def _refresh_once(skip: str, besides: str) -> None:
    import shlex

    rows = _skipped(pane_rows(), skip)
    commands = foreground_commands()
    head = header(rows)
    where = {r["window_id"]: (r["session_name"], r["window_index"]) for r in rows}
    for r in rows:
        if not is_sidebar(r["pane_start_command"]) or r["pane_id"] == besides:
            continue
        pane = r["pane_id"]
        items, _ = lines(rows, where[r["window_id"]], commands=commands)
        text = "\0".join(items)
        list_file, header_file = _tick_files(pane)
        try:
            if list_file.read_text() == text and header_file.read_text() == head:
                continue
        except OSError:
            pass
        list_file.parent.mkdir(parents=True, exist_ok=True)
        for f, content in ((list_file, text), (header_file, head)):
            tmp = f.with_name(f"{f.name}.{os.getpid()}")
            tmp.write_text(content)
            os.replace(tmp, f)
        lf, hf = shlex.quote(str(list_file)), shlex.quote(str(header_file))
        if not _post(socket_path(pane), f"reload-sync(cat {lf})+transform-header(cat {hf})"):
            list_file.unlink(missing_ok=True)  # not delivered: the next refresh sends it


def socket_path(pane: str) -> str:
    """Where a sidebar's fzf listens: next to tmux's socket, in a directory only the
    user can open, and short enough for a unix socket path."""
    tmux_socket = tmux("display-message", "-p", "#{socket_path}").strip()
    return os.path.join(os.path.dirname(tmux_socket), f"tam-sidebar-{pane.lstrip('%')}.sock")


def refocus(window: str) -> None:
    """Move the cursor of window's sidebar to the window's own card.

    Runs from a tmux hook when a window becomes the current one. Each window has
    its own sidebar, which otherwise still points at the line it was on.
    """
    sidebars = _by_window().get(window, {}).get("sidebars", [])
    if not sidebars:
        return
    where = tmux("display-message", "-p", "-t", window, "#{session_name}\t#{window_index}")
    session, index = where.strip().split("\t")
    _, selected = lines(pane_rows(), (session, index), commands={}, pr_of=lambda *_: None)
    _post(socket_path(sidebars[0]["pane_id"]), f"clear-query+pos({selected + 1})")


def _post(sock: str, actions: str) -> bool:
    """Send fzf actions to an fzf listening on the unix socket sock (--listen).

    False when nothing listens there.
    """
    return _request(sock, "POST", actions) is not None


def marked(pane: str) -> str:
    """The target of the line marked (under the cursor) in a sidebar pane, or ""."""
    import json

    try:
        state = json.loads(_request(socket_path(pane), "GET", "") or "{}")
        return str(state["current"]["text"]).split("\t", 1)[0]
    except (ValueError, KeyError, TypeError):
        return ""


def _request(sock: str, method: str, body: str) -> str | None:
    """One HTTP request to fzf's --listen unix socket. None when nothing listens there."""
    import http.client
    import socket

    class Connection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(2)
            self.sock.connect(sock)

    conn = Connection("localhost")
    path = "/?limit=0" if method == "GET" else "/"
    try:
        conn.request(method, path, body=body.encode() if body else None)
        return conn.getresponse().read().decode()
    except OSError:
        return None  # the sidebar has not started listening yet, or has just closed
    finally:
        conn.close()


def mouse(pane: str, word: str, line: str) -> None:
    """A click in a sidebar pane, also passed on to fzf: + terminal opens a terminal.

    tmux gives the word and the line under the mouse; fzf, which moves its cursor
    to the clicked line, gives which session that line belongs to.
    """
    import time

    if word not in ("+", "terminal") or TERMINAL_BUTTON not in line:
        return
    # The line as shown: fzf's pointer and gutter, the session's name, the button.
    name = line.split(TERMINAL_BUTTON)[0].strip().lstrip("▌").strip()
    for _ in range(20):  # until fzf has handled the click
        target = marked(pane)
        if target.endswith(":") and target[1:-1].upper().startswith(name.rstrip("…")):
            terminal_in_session("", target[1:-1])
            return
        time.sleep(0.05)


def close_marked(client: str) -> None:
    """⌥ w: ask, then close the line marked in the client's sidebar.

    Without a sidebar (hidden) or a marked line, the client's current window.
    """
    import shlex

    window = tmux("display-message", "-p", *_client(client), "#{window_id}").strip()
    sidebars = _by_window().get(window, {}).get("sidebars", [])
    target = marked(sidebars[0]["pane_id"]) if sidebars else ""
    if not target.startswith(("=", "@")):  # a session's line, or a window's
        session, index = _current(client)
        target = f"={session}:{index}"
    exe = shlex.quote(paths.tam_executable())
    prompt = f"close {_describe(target)}? (y/n)".replace("#", "##")
    run = f"{exe} sidebar --close {shlex.quote(target)}"
    if client:
        run += f" --client {shlex.quote(client)}"
    tmux(
        "confirm-before",
        *(["-t", client] if client else []),
        "-p",
        prompt,
        f"run-shell -b {shlex.quote(run)}",
    )


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


def restart() -> None:
    """Restart every sidebar's process, so a new tam's fzf bindings take effect.

    fzf gets its bindings when it starts; a sidebar started by an older tam keeps
    the old ones. respawn-pane keeps the pane's place and size.
    """
    command = f"{paths.tam_executable()} {PANE_COMMAND}"
    for w in _by_window().values():
        for s in w["sidebars"]:
            tmux("respawn-pane", "-k", "-t", s["pane_id"], command, check=False)


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
