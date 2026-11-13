"""Agent hooks that report session ids back to tam."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from tam import paths, state
from tam.tmux import pane_option, set_pane_option


def _ps(pid: int, field: str) -> str:
    proc = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True)
    return proc.stdout.strip()


def find_ancestor(name: str) -> int | None:
    """Closest ancestor process whose executable name is `name`."""
    pid = os.getppid()
    for _ in range(10):
        if pid <= 1:
            return None
        if os.path.basename(_ps(pid, "comm")) == name:
            return pid
        try:
            pid = int(_ps(pid, "ppid"))
        except ValueError:
            return None
    return None


def record_session(session_id: str) -> None:
    """Attach the session id to the current tam pane, if there is one."""
    pane = os.environ.get("TMUX_PANE")
    if not pane or not session_id:
        return
    if not pane_option(pane, "@tam_harness"):
        return
    set_pane_option(pane, "@tam_session_id", session_id)
    set_pane_option(pane, "@tam_started", "1")
    set_pane_option(pane, "@tam_status", "waiting")
    state.snapshot()


def claude() -> int:
    """Claude Code SessionStart hook. Reads the hook JSON on stdin."""
    try:
        data = json.load(sys.stdin)
        session_id = data.get("session_id", "")
        pid = find_ancestor("claude")
        if pid:
            seen = paths.seen_dir()
            seen.mkdir(parents=True, exist_ok=True)
            (seen / str(pid)).write_text(
                json.dumps(
                    {
                        "pid": pid,
                        "tty": _ps(pid, "tty"),
                        "cwd": data.get("cwd", ""),
                        "session_id": session_id,
                        "time": int(time.time()),
                    }
                )
            )
        record_session(session_id)
    except Exception as e:  # a hook must never break the agent
        print(f"tam hook: {e}", file=sys.stderr)
    return 0


def session(session_id: str) -> int:
    """Generic hook for other agents: `tam hook session <id>`."""
    try:
        record_session(session_id)
    except Exception as e:
        print(f"tam hook: {e}", file=sys.stderr)
    return 0
