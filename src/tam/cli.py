from __future__ import annotations

import argparse
import os
import sys

from tam import paths


def _attach(session: str | None) -> int:
    from tam.new import project_name
    from tam.tmux import tmux

    if session is None:
        session = project_name(os.getcwd())
        tmux("new-session", "-d", "-s", session)
    if os.environ.get("TMUX"):
        tmux("switch-client", "-t", f"={session}")
        return 0
    os.execvp("tmux", ["tmux", "attach-session", "-t", f"={session}"])


def cmd_default(a: argparse.Namespace) -> int:
    from tam import state
    from tam.tmux import global_option, server_running, session_names

    if server_running() and global_option(state.RESTORED_FLAG) == "1":
        names = session_names()
        last = (state.load() or {}).get("last_session")
        session = last if last in names else (min(names) if names else None)
    else:
        session = state.restore(paths.tam_executable())
    if a.no_attach:
        return 0
    return _attach(session)


def cmd_new(a: argparse.Namespace) -> int:
    from tam.new import new

    return new(
        tam_exe=paths.tam_executable(),
        harness=a.agent,
        name=a.name,
        split=a.split,
        args=a.args,
        target=a.target,
    )


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


def cmd_adopt(_: argparse.Namespace) -> int:
    from tam.adopt import adopt

    return adopt(paths.tam_executable())


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
    s.add_argument("--target", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("adopt", help="move a Claude session from outside tam into tam")
    s.set_defaults(func=cmd_adopt)

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
