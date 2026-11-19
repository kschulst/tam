"""`tam mcp`: an MCP server (stdio) with tools that let an agent work with tam's windows.

An agent starts `tam mcp` as a child process from inside its tmux pane, so the
server talks to the same tmux server and knows the caller's pane ($TMUX_PANE).
The tools use the same functions as the sidebar and the CLI; search_sessions and
reopen are `tam reopen`'s (tam.history). Closing the
caller's own window or session, or typing into its own pane, needs self=true.

`tam mcp install` registers the server with every installed agent that has an
`mcp add` command; `tam mcp uninstall` removes it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from tam import paths

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

# Sent with initialize. Claude Code puts a server's instructions in the system
# prompt, so the agent knows when the tools apply without being told.
INSTRUCTIONS = """\
The user runs coding agents in tam: tmux sessions and windows, one agent or \
terminal per window, listed in a sidebar. Use these tools when the user talks \
about windows, sessions, tabs, the sidebar, other agents, or an earlier or \
closed conversation:
- list_windows: what runs where, which agents are working or waiting, and their \
issues and PRs.
- new_agent / new_terminal: start an agent or a shell in a directory, in a new \
window or a split.
- send_text: tell or ask another agent something (it is typed into its pane).
- search_sessions, then reopen: find an earlier conversation (by title, \
directory, or with content=true what was said) and open it again.
- rename, switch_to, close_window, close_session: tidy up. Closing stops the \
agent in it; ask the user first unless they asked for it.
"""


class ToolError(Exception):
    """A tool call that cannot be done; the message goes back to the agent."""


# Tools


def _caller_window() -> str:
    from tam.tmux import tmux

    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return ""
    return tmux("display-message", "-p", "-t", pane, "#{window_id}", check=False).strip()


def _window(target: str) -> dict[str, str]:
    """window id, session and index for a window id (@12), =session:index or session:index."""
    from tam.tmux import tmux

    if not target:
        raise ToolError("window is required (an id like @12 from list_windows)")
    fmt = "#{window_id}\t#{session_name}\t#{window_index}"
    t = target if target.startswith(("@", "=")) else f"={target}"
    out = tmux("display-message", "-p", "-t", t, fmt, check=False).strip()
    if not out:
        raise ToolError(f"no window {target}")
    wid, session, index = out.split("\t")
    return {"id": wid, "session": session, "index": index}


def list_windows() -> dict:
    from tam import pr, refs, sidebar

    me = _caller_window()
    shown = {r["window_id"] for r in _query(["list-clients"], ["window_id"]) if r.get("window_id")}
    windows = []
    for session, ws in sidebar._windows(sidebar.pane_rows()).items():
        for index, w in sorted(ws.items(), key=lambda kv: int(kv[0])):
            pane = w["agent_pane"] or w["pane"]
            path = pane.get("pane_current_path", "")
            root, branch = sidebar.repo_branch(path)
            links = []
            found = pr.lookup(root, branch) if branch else None
            if found:
                links.append({"kind": "pr", "number": found["number"], "url": found["url"]})
            if w["agent_pane"]:
                links += refs.lookup(pane.get("pane_id", ""))
            entry = {
                "window": w["id"],
                "session": session,
                "index": int(index),
                "name": w["name"],
                "path": path,
                "branch": branch,
                "panes": w["panes"],
                "shown": w["id"] in shown,
                "self": w["id"] == me,
                "links": links,
            }
            if w["agent_pane"]:
                entry["agent"] = pane.get("@tam_harness", "")
                entry["status"] = sidebar.window_status(w["statuses"])
                entry["topic"] = sidebar.title(
                    pane.get("pane_title", ""), pane.get("host_short", "")
                )
            windows.append(entry)
    return {"windows": windows}


def _query(cmd: list[str], fields: list[str]) -> list[dict[str, str]]:
    from tam.tmux import query

    try:
        return query(cmd, fields)
    except Exception:  # no clients attached
        return []


def _split(value: str | None) -> str | None:
    if value in (None, "", "none", "window"):
        return None
    if value in ("right", "h"):
        return "h"
    if value in ("below", "v"):
        return "v"
    raise ToolError(f"split must be right, below or none, not {value!r}")


def _directory(value: str | None) -> str:
    cwd = os.path.abspath(os.path.expanduser(value)) if value else os.getcwd()
    if not os.path.isdir(cwd):
        raise ToolError(f"no such directory: {value}")
    return cwd


def new_agent(
    dir: str = "",  # noqa: A002
    agent: str = "",
    name: str = "",
    split: str = "",
    window: str = "",
) -> dict:
    from tam.harness import default_agent, load
    from tam.new import new

    agent = agent or default_agent()
    if agent not in load():
        raise ToolError(f"unknown agent {agent!r}; tam agent lists them")
    target = _window(window)["id"] if window else os.environ.get("TMUX_PANE")
    if not target:
        raise ToolError("not inside tmux: give window, an id from list_windows")
    session = new(
        tam_exe=paths.tam_executable(),
        harness=agent,
        name=name or None,
        split=_split(split),
        args=[],
        cwd=_directory(dir),
        target=target,
        select=False,
    )
    return {"started": agent, "session": session}


def new_terminal(dir: str = "", split: str = "", window: str = "") -> dict:  # noqa: A002
    from tam.new import new_terminal as open_terminal

    target = _window(window)["id"] if window else os.environ.get("TMUX_PANE")
    if not target:
        raise ToolError("not inside tmux: give window, an id from list_windows")
    session = open_terminal(cwd=_directory(dir), split=_split(split), target=target, select=False)
    return {"opened": "terminal", "session": session}


def close_window(window: str, self: bool = False) -> dict:  # noqa: A002
    from tam import sidebar

    w = _window(window)
    if w["id"] == _caller_window() and not self:
        raise ToolError("that is your own window; pass self=true to close it")
    sidebar.close("", w["id"])
    return {"closed": w["id"]}


def close_session(session: str, self: bool = False) -> dict:  # noqa: A002
    from tam import sidebar
    from tam.tmux import session_names

    if session not in session_names():
        raise ToolError(f"no session {session!r}")
    mine = _window(_caller_window())["session"] if _caller_window() else ""
    if session == mine and not self:
        raise ToolError("that is your own session; pass self=true to close it")
    sidebar.close("", f"={session}:")
    return {"closed": session}


def rename(name: str, window: str = "", session: str = "") -> dict:
    from tam import sidebar
    from tam.tmux import tmux

    name = name.strip()
    if not name:
        raise ToolError("name is required")
    if session:
        tmux("rename-session", "-t", f"={session}", "--", name)
        return {"renamed": session, "to": name}
    w = _window(window or _caller_window())
    tmux("rename-window", "-t", w["id"], "--", name)
    tmux("set-option", "-w", "-t", w["id"], sidebar.NAMED_OPTION, "1")
    return {"renamed": w["id"], "to": name}


def switch_to(window: str) -> dict:
    from tam.tmux import tmux

    w = _window(window)
    tmux("switch-client", "-t", w["id"], check=False)
    return {"switched": w["id"]}


def send_text(window: str, text: str, enter: bool = True, self: bool = False) -> dict:  # noqa: A002
    """Type text into the window's agent pane (else its active pane)."""
    from tam import sidebar
    from tam.tmux import query, tmux

    w = _window(window)
    rows = query(
        ["list-panes", "-t", w["id"]],
        ["pane_id", "pane_active", "pane_start_command", "@tam_harness"],
    )
    panes = [r for r in rows if not sidebar.is_sidebar(r["pane_start_command"])]
    pane = next((r for r in panes if r["@tam_harness"]), None) or next(
        (r for r in panes if r["pane_active"] == "1"), None
    )
    if not pane:
        raise ToolError(f"no pane to type into in {window}")
    if pane["pane_id"] == os.environ.get("TMUX_PANE") and not self:
        raise ToolError("that is your own pane; pass self=true to type into it")
    tmux("send-keys", "-t", pane["pane_id"], "-l", text)
    if enter:
        tmux("send-keys", "-t", pane["pane_id"], "Enter")
    return {"sent": len(text), "pane": pane["pane_id"]}


def search_sessions(
    query: str = "",
    dir: str = "",  # noqa: A002
    agent: str = "",
    content: bool = False,
    limit: int = 20,
) -> dict:
    from tam import history

    found = history.search(query, dir=dir, agent=agent, content=content, limit=max(1, limit))
    return {"sessions": [s.as_dict() for s in found]}


def reopen(id: str, split: str = "", window: str = "") -> dict:  # noqa: A002
    from tam import history

    target = _window(window)["id"] if window else os.environ.get("TMUX_PANE")
    try:
        return history.reopen(id, split=_split(split), target=target, select=False)
    except history.ReopenError as e:
        raise ToolError(str(e)) from e


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_STR = {"type": "string"}
_SELF = {"type": "boolean", "description": "Allow acting on your own window, session or pane."}
_WINDOW = {"type": "string", "description": "Window id (@12) from list_windows."}
_SPLIT = {"type": "string", "enum": ["none", "right", "below"], "description": "none: new window."}

TOOLS: dict[str, tuple[Callable[..., dict], str, dict]] = {
    "list_windows": (
        list_windows,
        "List tam's sessions and windows (the user's tabs and agents): agent, status "
        "(working, waiting for the user, idle), topic, directory, branch, open issue and PR "
        "links, whether a client shows it, and which one is yours (self).",
        _schema({}),
    ),
    "new_agent": (
        new_agent,
        "Start an agent in a directory, in a new window or a split of a window. The "
        "user's view does not move.",
        _schema(
            {
                "dir": {"type": "string", "description": "Directory (default: yours)."},
                "agent": {"type": "string", "description": "Agent (default: tam's default)."},
                "name": _STR,
                "split": _SPLIT,
                "window": {**_WINDOW, "description": "Window to split or add to (default: yours)."},
            }
        ),
    ),
    "new_terminal": (
        new_terminal,
        "Open a shell in a directory, in a new window or a split. The user's view does not move.",
        _schema({"dir": _STR, "split": _SPLIT, "window": _WINDOW}),
    ),
    "close_window": (
        close_window,
        "Close a window. An agent in it stops, and an answer it is writing is lost.",
        _schema({"window": _WINDOW, "self": _SELF}, ["window"]),
    ),
    "close_session": (
        close_session,
        "Close a tmux session with all its windows.",
        _schema({"session": _STR, "self": _SELF}, ["session"]),
    ),
    "rename": (
        rename,
        "Rename a window (default: yours) or, with session, a session. A renamed window's "
        "sidebar card keeps the name instead of the agent's topic.",
        _schema({"name": _STR, "window": _WINDOW, "session": _STR}, ["name"]),
    ),
    "switch_to": (
        switch_to,
        "Show a window to the user (switch the tmux client to it).",
        _schema({"window": _WINDOW}, ["window"]),
    ),
    "send_text": (
        send_text,
        "Tell or ask another agent something: type text into a window's agent (or its active "
        "pane), then Enter unless enter=false.",
        _schema(
            {"window": _WINDOW, "text": _STR, "enter": {"type": "boolean"}, "self": _SELF},
            ["window", "text"],
        ),
    ),
    "search_sessions": (
        search_sessions,
        'Find an earlier or closed conversation, newest first ("the session about X from '
        'last week"): windows closed in tam, and Claude and Codex conversations, also those '
        "started outside tam. Every word of query must be "
        "in the title or directory, or with content=true in the messages (slower). A "
        "session still open shows its window; one running outside tam shows its pid.",
        _schema(
            {
                "query": _STR,
                "dir": {"type": "string", "description": "Only sessions in or below it."},
                "agent": {"type": "string", "description": "Only this agent (claude, codex, …)."},
                "content": {"type": "boolean", "description": "Also search the messages."},
                "limit": {"type": "integer", "description": "Most results (default 20)."},
            }
        ),
    ),
    "reopen": (
        reopen,
        "Open an earlier session (an id from search_sessions) in a new tam window, resuming "
        "the same conversation, in the tmux session it was closed in or its project's. The "
        "user's view does not move. A session already open is not opened twice.",
        _schema(
            {
                "id": {"type": "string", "description": "id from search_sessions."},
                "split": _SPLIT,
                "window": {**_WINDOW, "description": "Window to split (with split)."},
            },
            ["id"],
        ),
    ),
}


# Protocol: JSON-RPC 2.0, one message per line on stdin and stdout.


def handle(message: dict) -> dict | None:
    """The response to one JSON-RPC message, or None for a notification."""
    from tam.paths import tam_version

    method, mid = message.get("method", ""), message.get("id")
    if mid is None:
        return None
    if method == "initialize":
        asked = (message.get("params") or {}).get("protocolVersion")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tam", "version": tam_version()},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": name, "description": desc, "inputSchema": schema}
                for name, (_, desc, schema) in TOOLS.items()
            ]
        }
    elif method == "tools/call":
        params = message.get("params") or {}
        result = call(params.get("name", ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def call(name: str, arguments: dict) -> dict:
    if name not in TOOLS:
        return _text(f"unknown tool {name!r}", error=True)
    fn = TOOLS[name][0]
    try:
        return _text(json.dumps(fn(**arguments), ensure_ascii=False))
    except ToolError as e:
        return _text(str(e), error=True)
    except TypeError as e:  # arguments the tool does not take
        return _text(f"bad arguments: {e}", error=True)
    except Exception as e:  # tmux or gh failed; the agent sees why
        return _text(f"{type(e).__name__}: {e}", error=True)


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse"}}
        else:
            response = handle(message) if isinstance(message, dict) else None
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


# Installing


# How each agent registers a stdio MCP server, and removes it. Agents without
# an `mcp add` command are left out.
AGENT_COMMANDS = {
    "claude": (
        ["claude", "mcp", "add", "--scope", "user", "tam", "--"],
        ["claude", "mcp", "remove", "--scope", "user", "tam"],
    ),
    "codex": (["codex", "mcp", "add", "tam", "--"], ["codex", "mcp", "remove", "tam"]),
    "gemini": (
        ["gemini", "mcp", "add", "--scope", "user", "tam"],
        ["gemini", "mcp", "remove", "--scope", "user", "tam"],
    ),
}


# A note in each agent's own instruction file, so it uses tam's tools in every
# session. Optional: the server's instructions say the same where an agent reads
# them. Between these markers, so uninstall and a later install replace it.
NOTE_START, NOTE_END = "<!-- tam mcp -->", "<!-- /tam mcp -->"
NOTE = (
    "You run inside tam (tmux). For windows, sessions, other agents, and earlier or "
    "closed conversations, use the tools of the `tam` MCP server (list_windows, "
    "new_agent, send_text, search_sessions, reopen, …) instead of tmux commands."
)


def note_files() -> dict[str, Path]:
    """Each agent's user-wide instruction file."""
    from tam.history import codex_dir

    return {
        "claude": paths.claude_dir() / "CLAUDE.md",
        "codex": codex_dir() / "AGENTS.md",
        "gemini": Path.home() / ".gemini" / "GEMINI.md",
    }


def set_note(path: Path, on: bool) -> bool:
    """Add (on) or remove tam's note in an instruction file. True if the file changed."""
    text = path.read_text() if path.exists() else ""
    start, end = text.find(NOTE_START), text.find(NOTE_END)
    if start == -1 or end < start:
        if not on:
            return False
        base = text
    else:
        before, after = text[:start].rstrip("\n"), text[end + len(NOTE_END) :].lstrip("\n")
        base = before + ("\n\n" if before and after else "") + after
    base = base.rstrip("\n") + "\n" if base.strip() else ""
    block = f"{NOTE_START}\n{NOTE}\n{NOTE_END}\n"
    new = base + ("\n" if base else "") + block if on else base
    if new == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    return True


def _ask_note(files: list[Path]) -> bool:
    if not sys.stdin.isatty():
        return False
    shown = ", ".join(str(f).replace(str(Path.home()), "~") for f in files)
    answer = input(f"Also add a line to {shown} telling the agents to use tam's tools? [y/N] ")
    return answer.strip().lower() in ("y", "yes", "j", "ja")


def install(uninstall: bool = False, note: bool | None = None) -> int:
    """Register tam's MCP server with every installed agent (or remove it).

    note: also add tam's note to their instruction files; None asks (no when not
    a terminal). uninstall always removes the note.
    """
    server = [paths.tam_executable(), "mcp"]
    found = False
    failed = False
    done: list[str] = []
    for agent, (add, remove) in AGENT_COMMANDS.items():
        if not shutil.which(add[0]):
            continue
        found = True
        if uninstall:
            cmd = remove
        else:
            # Replace an earlier registration, e.g. one pointing at another tam.
            subprocess.run(remove, capture_output=True, text=True)
            cmd = add + server
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            done.append(agent)
            print(f"✓ {agent}: {'removed' if uninstall else 'added'} tam's MCP server")
        else:
            failed = True
            reason = (proc.stderr or proc.stdout).strip().splitlines()
            print(f"✗ {agent}: {reason[-1] if reason else 'failed'}")
    if not found:
        print("tam: none of these agents is installed: " + ", ".join(AGENT_COMMANDS))
    files = {a: f for a, f in note_files().items() if a in done}
    if uninstall:
        note = False
    elif files and note is None:
        note = _ask_note(list(files.values()))
    for agent, f in files.items():
        if set_note(f, bool(note)):
            print(f"✓ {agent}: {'added' if note else 'removed'} tam's note in {f}")
    return 1 if failed else 0
