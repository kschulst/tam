from __future__ import annotations

import argparse
import os
import sys

from tam import paths


class _Version(argparse.Action):
    """--version, read only when asked: in a checkout it runs git."""

    def __call__(self, parser, namespace, values, option_string=None):
        print(f"tam {paths.tam_version()}")
        parser.exit()


def _attach(session: str, then: list[str]) -> int:
    """Attach (or switch) to session, then run the tmux command `then` for that client."""
    import shlex

    from tam.tmux import tmux

    if os.environ.get("TMUX"):
        tmux("switch-client", "-t", f"={session}")
        tmux(*then, check=False)
        return 0
    # Outside tmux there is no client yet: run the command once, when it attaches.
    hook = "client-attached[43]"
    command = " ".join(shlex.quote(part) for part in then)
    tmux("set-hook", "-g", hook, f"set-hook -gu {hook} ; {command}")
    from tam.tmux import attach

    attach(session)
    return 0


def _cheatsheet_popup() -> list[str]:
    import shlex

    from tam.setup import popup_command

    return popup_command(shlex.quote(paths.tam_executable()))


def cmd_default(a: argparse.Namespace) -> int:
    from tam import state
    from tam.new import project_root, session_name
    from tam.tmux import global_option, server_running, session_names, tmux

    cwd = os.getcwd()
    adopted = server_running() and global_option(state.ATTACH_TO)
    session = state.ensure_tmux(paths.tam_executable(), cwd)
    # In a project directory, go to that project's session. In ~, the last one.
    if not adopted and os.path.realpath(cwd) != os.path.realpath(os.path.expanduser("~")):
        session = session_name(cwd)
        if session not in session_names():
            tmux("new-session", "-d", "-s", session, "-c", project_root(cwd))
    if a.no_attach:
        return 0
    return _attach(session, _cheatsheet_popup())


def cmd_new(a: argparse.Namespace) -> int:
    from tam.harness import default_agent
    from tam.new import new

    if a.cycle_agent:
        from tam.dirs import cycle_agent

        print(cycle_agent(a.cycle_agent))
        return 0
    agent = a.agent or default_agent()

    cwd = os.path.abspath(os.path.expanduser(a.dir)) if a.dir else None
    if cwd and not os.path.isdir(cwd):
        print(f"tam: no such directory: {a.dir}", file=sys.stderr)
        return 1
    terminal = getattr(a, "terminal", False)
    if a.popup_client:
        # Run the directory picker in a popup; it calls `tam new --pick` with literal values.
        from tam.tmux import client_size, open_popup

        cols, rows = client_size(a.popup_client)
        command = ["term"] if terminal else ["new", agent]
        argv = [paths.tam_executable(), *command, "--pick", "--dir", cwd or os.getcwd()]
        if a.target:
            argv += ["--target", a.target]
        open_popup(
            a.popup_client,
            min(100, cols * 9 // 10),
            max(12, rows * 6 // 10),
            argv,
            title="new terminal" if terminal else "new agent",
        )
        return 0
    split = a.split
    if a.pick:
        from tam.dirs import pick

        picked = pick(cwd or os.getcwd(), None if terminal else agent)
        if picked is None:
            return 0
        cwd, picked_agent, split = picked
        agent = picked_agent or agent

    if terminal:
        from tam.new import new_terminal

        new_terminal(cwd=cwd or os.getcwd(), split=split, target=a.target)
        return 0
    new(
        cwd=cwd,
        tam_exe=paths.tam_executable(),
        harness=agent,
        name=a.name,
        split=split,
        args=a.args,
        target=a.target,
    )
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    from tam.run import run

    return run(
        harness_name=a.harness,
        name=a.name,
        args=a.args,
        session_id=a.session_id,
        started=a.started,
        wait=a.wait,
        missing_cwd=a.missing_cwd,
    )


def cmd_prompt(a: argparse.Namespace) -> int:
    """⌥ /: run what was typed in tmux's command prompt as `tam <that>`, in a popup.

    Waits for a key at the end, so the output can be read before the popup closes.
    """
    import subprocess
    import termios
    import tty

    # Already split by the popup's shell, quotes and all.
    words = list(a.words) or ["--help"]
    if words[0] == "tam":  # typed with the program's name
        words = words[1:] or ["--help"]
    code = subprocess.run([paths.tam_executable(), *words]).returncode
    print(f"\n(tam exited with {code}) " if code else "\n", end="")
    print("press a key to close", end="", flush=True)
    fd = sys.stdin.fileno()
    if os.isatty(fd):
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            os.read(fd, 1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return code


def cmd_mcp(a: argparse.Namespace) -> int:
    from tam import mcp

    if a.action in ("install", "uninstall"):
        return mcp.install(uninstall=a.action == "uninstall", note=a.note)
    return mcp.serve()


def cmd_snapshot(_: argparse.Namespace) -> int:
    from tam import state

    state.snapshot()
    return 0


def cmd_hook(a: argparse.Namespace) -> int:
    from tam import hook

    if a.kind == "claude":
        return hook.claude()
    if a.kind == "claude-stop":
        return hook.claude_stop()
    if a.kind == "session" and a.id:
        return hook.session(a.id)
    print("usage: tam hook claude | claude-stop | session <id>", file=sys.stderr)
    return 2


def cmd_adopt(a: argparse.Namespace) -> int:
    from tam.adopt import adopt, finish

    if a.finish:
        pid, session_id, cwd = a.finish
        return finish(paths.tam_executable(), int(pid), session_id, cwd)
    return adopt(paths.tam_executable())


def cmd_reopen(a: argparse.Namespace) -> int:
    from tam.history import pick

    return pick(" ".join(a.words), content=a.content, agent=a.agent, dir=a.dir)


def cmd_help(a: argparse.Namespace) -> int:
    from tam.cheatsheet import show

    return show(print_only=a.print_only, wait=a.wait, client=a.client)


def cmd_sidebar(a: argparse.Namespace) -> int:
    from tam import sidebar
    from tam.tmux import tmux

    # The fzf actions pass the focused line as {1}, which is '' when no line
    # matches. Such a call prints no action instead of falling through to popup().
    if a.footer is not None:
        print(sidebar.footer(a.footer) if a.footer else "")
        return 0
    if a.header:
        print(sidebar.header(sidebar.pane_rows()))
        return 0
    if a.pane:
        return sidebar.run_pane()
    if a.pr_refresh:
        from tam import pr

        pr.refresh(*a.pr_refresh)
        return 0
    if a.refs_refresh:
        from tam import refs

        refs.refresh(a.refs_refresh)
        return 0
    if a.ensure:
        sidebar.ensure()
        return 0
    if a.refocus:
        sidebar.refocus(a.refocus)
        return 0
    if a.tick:
        print(sidebar.tick())
        return 0
    if a.refresh:
        sidebar.refresh()
        return 0
    if a.mouse:
        pane, word, line = a.mouse
        sidebar.mouse(pane, word.removeprefix("w"), line.removeprefix("l"))
        return 0
    if a.toggle:
        sidebar.toggle()
        return 0
    # A sidebar pane has no client of its own: tmux picks the one showing its session.
    client = a.client or tmux("display-message", "-p", "#{client_name}", check=False).strip()
    if a.click is not None:
        if a.click:
            print(sidebar.click(client, a.click, paths.tam_executable()))
        return 0
    if a.close_marked:
        sidebar.close_marked(client)
        return 0
    if a.close:
        sidebar.close(client, a.close)
        return 0
    if a.line_click is not None:
        if a.line_click:
            print(sidebar.line_click(client, a.line_click, paths.tam_executable()))
        return 0
    if a.rename is not None:
        if a.rename:
            print(sidebar.rename_start(a.rename))
        return 0
    if a.enter is not None:
        print(sidebar.enter(client, a.enter, a.name or "", paths.tam_executable()))
        return 0
    if a.escape:
        print(sidebar.escape())
        return 0
    if a.list:
        sys.stdout.write("\0".join(sidebar.list_lines(client)[0]))
        return 0
    sidebar.focus(client)
    return 0


def cmd_move(a: argparse.Namespace) -> int:
    from tam import move
    from tam.tmux import tmux

    client = a.client or tmux("display-message", "-p", "#{client_name}").strip()
    window = a.window or tmux("display-message", "-p", "#{window_id}").strip()
    if a.to:
        move.move(client, window, a.to)
        return 0
    return move.pick(client, window) if a.pick else move.popup(client, window)


def cmd_agent(a: argparse.Namespace) -> int:
    from tam import config
    from tam.harness import default_agent, installed_names, load

    names = installed_names()
    if a.name:
        if a.name not in load():
            print(f"tam: unknown agent '{a.name}'. Add it to {paths.config_dir()}/harnesses.toml")
            return 1
        if a.name not in names:
            print(f"tam: '{a.name}' is not installed (not on PATH)")
            return 1
        config.set("default_agent", a.name)
        print(f"tam: default agent is now {a.name}")
        return 0
    default = default_agent()
    for name in names:
        print(f"{'*' if name == default else ' '} {name}")
    print("\nChange the default: tam agent <name>")
    print(f"Add an agent: a [harness.<name>] block in {paths.config_dir()}/harnesses.toml,")
    print('  e.g.  [harness.myagent]  start = "myagent"  resume = "myagent --continue"')
    return 0


def cmd_setup(_: argparse.Namespace) -> int:
    from tam.setup import setup

    return setup()


def cmd_tmux_init(_: argparse.Namespace) -> int:
    from tam.setup import tmux_init

    return tmux_init()


def _split_extra(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1 :]
    return argv, []


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    argv, extra = _split_extra(argv)

    p = argparse.ArgumentParser(
        prog="tam", description="Agent sessions in tmux that survive a reboot."
    )
    p.add_argument("--version", action=_Version, nargs=0, help="show the version and exit")
    p.add_argument("--no-attach", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_default)
    sub = p.add_subparsers(metavar="command")

    s = sub.add_parser("new", help="open an agent in a new window or split")
    s.add_argument("agent", nargs="?", help="agent to start (default: see tam agent)")
    s.add_argument("--cycle-agent", help=argparse.SUPPRESS)
    s.add_argument("-n", "--name")
    s.add_argument("--split", choices=["h", "v"])
    s.add_argument("-C", "--dir", help="directory to start in (default: current)")
    s.add_argument("--pick", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--popup-client", help=argparse.SUPPRESS)
    s.add_argument("--target", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("term", help="open a terminal (no agent) in a new window or split")
    s.add_argument("-C", "--dir", help="directory to start in (default: current)")
    s.add_argument("--split", choices=["h", "v"])
    s.add_argument("--target", help=argparse.SUPPRESS)
    s.add_argument("--pick", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--popup-client", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_new, terminal=True, agent=None, name=None, cycle_agent=None)

    s = sub.add_parser("adopt", help="move an agent session from outside tam into tam")
    s.add_argument("--finish", nargs=3, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_adopt)

    s = sub.add_parser(
        "reopen",
        help="find an earlier agent session and open it again",
        description="Pick an earlier session: windows closed in tam, and Claude and Codex"
        " conversations. Words filter on the title and directory; --content also searches"
        " the messages.",
    )
    s.add_argument("words", nargs="*", help="words the session must have")
    s.add_argument("--content", action="store_true", help="also search the messages")
    s.add_argument("--agent", default="", help="only this agent's sessions")
    s.add_argument("--dir", default="", help="only sessions in or below this directory")
    s.set_defaults(func=cmd_reopen)

    s = sub.add_parser("help", help="show the cheat sheet")
    s.add_argument("--print", dest="print_only", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--client", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_help)

    s = sub.add_parser("sidebar", help="move focus to the sidebar, or back from it")
    s.add_argument(
        "--toggle", action="store_true", help="show the sidebar in every window, or hide it"
    )
    s.add_argument("--client", help=argparse.SUPPRESS)
    s.add_argument("--pane", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--ensure", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--refocus", help=argparse.SUPPRESS)
    s.add_argument("--tick", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--mouse", nargs=3, help=argparse.SUPPRESS)
    s.add_argument("--close-marked", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--close", help=argparse.SUPPRESS)
    s.add_argument("--list", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--footer", help=argparse.SUPPRESS)
    s.add_argument("--click", help=argparse.SUPPRESS)
    s.add_argument("--rename", help=argparse.SUPPRESS)
    s.add_argument("--enter", help=argparse.SUPPRESS)
    s.add_argument("--escape", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--header", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--line-click", help=argparse.SUPPRESS)
    s.add_argument("--pr-refresh", nargs=2, help=argparse.SUPPRESS)
    s.add_argument("--refs-refresh", help=argparse.SUPPRESS)
    s.add_argument("--name", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_sidebar)

    s = sub.add_parser("move", help="move the current window to another session")
    s.add_argument("to", nargs="?", help="destination session (default: pick)")
    s.add_argument("--client", help=argparse.SUPPRESS)
    s.add_argument("--window", help=argparse.SUPPRESS)
    s.add_argument("--pick", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_move)

    s = sub.add_parser("agent", help="list installed agents, or set the default: tam agent <name>")
    s.add_argument("name", nargs="?")
    s.set_defaults(func=cmd_agent)

    s = sub.add_parser("setup", help="install the tmux config line and the agent hooks")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("run")
    s.add_argument("--harness", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--session-id")
    s.add_argument("--started", action="store_true")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--missing-cwd")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser(
        "mcp",
        help="MCP server that lets agents open, close, rename and type into tam's windows,"
        " and reopen earlier sessions",
        description="Without an action: run the MCP server on stdin/stdout (agents start it). "
        "install / uninstall: register it with every installed agent.",
    )
    s.add_argument("action", nargs="?", choices=["serve", "install", "uninstall"], default="serve")
    s.add_argument(
        "--note",
        action=argparse.BooleanOptionalAction,
        help="install: also add a line to each agent's instruction file (CLAUDE.md, AGENTS.md,"
        " GEMINI.md) telling it to use tam's tools (default: ask)",
    )
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("prompt")  # ⌥ /: tmux's command prompt runs this in a popup
    s.add_argument("words", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_prompt)

    s = sub.add_parser("snapshot")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("hook")
    s.add_argument("kind")
    s.add_argument("id", nargs="?")
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("tmux-init")
    s.set_defaults(func=cmd_tmux_init)

    a = p.parse_args(argv)
    a.args = extra
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
