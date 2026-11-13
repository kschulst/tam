"""`tam run`: the process that runs in every agent pane.

It starts or resumes the agent, and when the agent exits it waits for
Enter (resume) or q (close the pane). Restored panes start in that waiting
state, so agents only start when you go to them.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import termios
import time
import tty

from tam import harness as harness_mod
from tam import state
from tam.tmux import pane_option, set_pane_option, unset_pane_option

# A resume that exits non-zero this quickly is treated as a failed resume.
RESUME_FAIL_SECONDS = 10


def _key() -> str:
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            return "q"
        return line.strip()[:1] or "\r"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return os.read(fd, 1).decode(errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def ask(message: str) -> bool:
    """Show message and wait. True for Enter, False for q."""
    print(f"\n\033[1m{message}\033[0m  [Enter] continue  [q] close", flush=True)
    while True:
        k = _key()
        if k in ("\r", "\n"):
            return True
        if k in ("q", "Q", "\x03", "\x04"):
            return False


def ensure_cwd(name: str) -> bool:
    """Make sure the agent starts in a directory that exists.

    A renamed directory keeps working as the process cwd, but $PWD still names
    the old path, and agents such as Claude refuse to start then. A deleted
    directory cannot be used at all, so the user picks another one.
    """
    try:
        cwd = os.getcwd()
    except (FileNotFoundError, OSError):
        cwd = ""
    if cwd and os.path.isdir(cwd):
        os.environ["PWD"] = cwd
        return True
    os.chdir(os.path.expanduser("~"))
    return _pick_dir(name, os.environ.get("PWD", ""))


def _pick_dir(name: str, gone: str) -> bool:
    """Let the user pick a directory instead of `gone`. False if they close the pane."""
    from tam.dirs import pick

    if not ask(f"{name}: directory {gone} no longer exists. Pick another one?"):
        return False
    picked = pick(os.getcwd(), name)
    if picked:
        os.chdir(picked[0])
    os.environ["PWD"] = os.getcwd()
    return True


def _spawn(argv: list[str]) -> int:
    try:
        return subprocess.call(argv)
    except FileNotFoundError:
        print(f"tam: command not found: {argv[0]}", file=sys.stderr)
        return 127


def run(
    *,
    harness_name: str,
    name: str,
    args: list[str],
    session_id: str | None,
    started: bool,
    wait: bool,
    missing_cwd: str | None,
) -> int:
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        print("tam run must be started inside a tmux pane", file=sys.stderr)
        return 1

    set_pane_option(pane, "@tam_harness", harness_name)
    set_pane_option(pane, "@tam_name", name)
    set_pane_option(pane, "@tam_args", json.dumps(args))
    if session_id:
        set_pane_option(pane, "@tam_session_id", session_id)
    if started:
        set_pane_option(pane, "@tam_started", "1")
    state.snapshot()

    # Ctrl+C belongs to the agent. SIGTERM is ignored so the pane survives a
    # shutdown, which sends SIGTERM to every process; otherwise the pane would
    # close and be dropped from the saved state before tmux itself exits.
    # No-op handlers (not SIG_IGN) are reset to the default in the child on exec.
    signal.signal(signal.SIGINT, lambda *_: None)
    signal.signal(signal.SIGTERM, lambda *_: None)

    # Restore started this pane in $HOME because the saved directory is gone.
    if missing_cwd and not _pick_dir(name, missing_cwd):
        return 0

    harnesses = harness_mod.load()
    h = harnesses.get(harness_name)
    if h is None:
        ask(f"tam: unknown agent '{harness_name}'. Add it to harnesses.toml.")
        return 1

    if wait:
        set_pane_option(pane, "@tam_status", "stopped")
        if not ask(f"{name}: resume {harness_name}?"):
            return 0

    fresh = False
    while True:
        # Before the command is built: templates may use $PWD.
        if not ensure_cwd(name):
            return 0
        sid = "" if fresh else pane_option(pane, "@tam_session_id")
        was_started = not fresh and pane_option(pane, "@tam_started") == "1"
        argv, resuming = h.command(name=name, session_id=sid, started=was_started, args=args)
        if fresh:
            unset_pane_option(pane, "@tam_session_id")
            unset_pane_option(pane, "@tam_started")
            fresh = False
        t0 = time.monotonic()
        set_pane_option(pane, "@tam_started", "1")
        state.snapshot()
        set_pane_option(pane, "@tam_status", "running")
        rc = _spawn(argv)
        set_pane_option(pane, "@tam_status", "exited")
        if resuming and rc != 0 and time.monotonic() - t0 < RESUME_FAIL_SECONDS:
            if not ask(f"{name}: resume failed (exit {rc}). Start a new session?"):
                return 0
            fresh = True
            continue
        if not ask(f"{name}: {harness_name} exited (exit {rc}). Resume?"):
            return 0
