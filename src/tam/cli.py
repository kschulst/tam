from __future__ import annotations

import argparse
import os
import sys

from tam import paths


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
    os.execvp("tmux", ["tmux", "attach-session", "-t", f"={session}"])


def _cheatsheet_popup() -> list[str]:
    import shlex

    from tam.setup import popup_command

    return popup_command(shlex.quote(paths.tam_executable()))


def cmd_default(a: argparse.Namespace) -> int:
    from tam import state

    session = state.ensure_tmux(paths.tam_executable(), os.getcwd())
    if a.no_attach:
        return 0
    return _attach(session, _cheatsheet_popup())


def cmd_new(a: argparse.Namespace) -> int:
    from tam.new import new

    cwd = os.path.abspath(os.path.expanduser(a.dir)) if a.dir else None
    if cwd and not os.path.isdir(cwd):
        print(f"tam: no such directory: {a.dir}", file=sys.stderr)
        return 1
    if a.popup_client:
        # Run the directory picker in a popup; it calls `tam new --pick` with literal values.
        from tam.tmux import client_size, open_popup

        cols, rows = client_size(a.popup_client)
        argv = [paths.tam_executable(), "new", a.agent, "--pick", "--dir", cwd or os.getcwd()]
        if a.target:
            argv += ["--target", a.target]
        open_popup(
            a.popup_client,
            min(100, cols * 9 // 10),
            max(12, rows * 6 // 10),
            argv,
            title=f"new {a.agent}",
        )
        return 0
    if a.pick:
        from tam.dirs import pick

        cwd = pick(cwd or os.getcwd())
        if cwd is None:
            return 0

    new(
        cwd=cwd,
        tam_exe=paths.tam_executable(),
        harness=a.agent,
        name=a.name,
        split=a.split,
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


def cmd_snapshot(_: argparse.Namespace) -> int:
    from tam import state

    state.snapshot()
    return 0


def cmd_hook(a: argparse.Namespace) -> int:
    from tam import hook

    if a.kind == "claude":
        return hook.claude()
    if a.kind == "session" and a.id:
        return hook.session(a.id)
    print("usage: tam hook claude | tam hook session <id>", file=sys.stderr)
    return 2


def cmd_adopt(a: argparse.Namespace) -> int:
    from tam.adopt import adopt, finish

    if a.finish:
        pid, session_id, cwd = a.finish
        return finish(paths.tam_executable(), int(pid), session_id, cwd)
    return adopt(paths.tam_executable())


def cmd_help(a: argparse.Namespace) -> int:
    from tam.cheatsheet import show

    return show(print_only=a.print_only, wait=a.wait, client=a.client)


def cmd_sidebar(a: argparse.Namespace) -> int:
    from tam import sidebar
    from tam.tmux import tmux

    client = a.client or tmux("display-message", "-p", "#{client_name}").strip()
    return sidebar.pick(client) if a.pick else sidebar.popup(client)


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
    p.add_argument("--no-attach", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_default)
    sub = p.add_subparsers(metavar="command")

    s = sub.add_parser("new", help="open an agent in a new window or split")
    s.add_argument("agent", nargs="?", default="claude")
    s.add_argument("-n", "--name")
    s.add_argument("--split", choices=["h", "v"])
    s.add_argument("-C", "--dir", help="directory to start in (default: current)")
    s.add_argument("--pick", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--popup-client", help=argparse.SUPPRESS)
    s.add_argument("--target", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("adopt", help="move a Claude session from outside tam into tam")
    s.add_argument("--finish", nargs=3, help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_adopt)

    s = sub.add_parser("help", help="show the cheat sheet")
    s.add_argument("--print", dest="print_only", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--wait", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--client", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_help)

    s = sub.add_parser("sidebar", help="list sessions and agents, switch with Enter")
    s.add_argument("--client", help=argparse.SUPPRESS)
    s.add_argument("--pick", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_sidebar)

    s = sub.add_parser("setup", help="install the tmux config line and the Claude hook")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("run")
    s.add_argument("--harness", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--session-id")
    s.add_argument("--started", action="store_true")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--missing-cwd")
    s.set_defaults(func=cmd_run)

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
