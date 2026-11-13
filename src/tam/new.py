"""`tam new`: open an agent in a new window or split."""

from __future__ import annotations

import os
import subprocess

from tam import harness as harness_mod
from tam.tmux import query, server_running, session_names, tmux


def project_name(cwd: str) -> str:
    proc = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    root = proc.stdout.strip() if proc.returncode == 0 else cwd
    return os.path.basename(root.rstrip("/")) or "root"


def _unique_name(session: str, project: str, base: str) -> str:
    names = {
        r["window_name"] for r in query(["list-windows", "-t", f"={session}"], ["window_name"])
    }
    name, n = base, 2
    while f"{project}:{name}" in names:
        name, n = f"{base}-{n}", n + 1
    return name


def run_command(
    tam_exe: str,
    harness: str,
    name: str,
    args: list[str],
    session_id: str | None = None,
) -> list[str]:
    cmd = [tam_exe, "run", "--harness", harness, "--name", name]
    if session_id:
        cmd += ["--session-id", session_id, "--started"]
    return cmd + ["--", *args]


def new(
    *,
    tam_exe: str,
    harness: str,
    name: str | None,
    split: str | None,
    args: list[str],
    cwd: str | None = None,
    target: str | None = None,
    session_id: str | None = None,
    attach: bool = True,
) -> str:
    """Create the agent window or split. Returns the tmux session it was created in."""
    if harness not in harness_mod.load():
        raise SystemExit(f"tam: unknown agent '{harness}'")
    cwd = cwd or os.getcwd()
    project = project_name(cwd)
    target = target or os.environ.get("TMUX_PANE")
    attach_after = False

    if target:
        session = tmux("display-message", "-p", "-t", target, "#{session_name}").strip()
    else:
        # Outside tmux: use a session named after the project, then attach.
        session = project
        attach_after = attach
        if not server_running() or session not in session_names():
            tmux("new-session", "-d", "-s", session, "-c", cwd)

    if split:
        name = name or harness
        flag = "-h" if split == "h" else "-v"
        tmux(
            "split-window",
            flag,
            "-t",
            target or f"={session}:",
            "-c",
            cwd,
            *run_command(tam_exe, harness, name, args, session_id),
        )
    else:
        name = name or _unique_name(session, project, harness)
        tmux(
            "new-window",
            "-t",
            f"={session}:",
            "-c",
            cwd,
            "-n",
            f"{project}:{name}",
            *run_command(tam_exe, harness, name, args, session_id),
        )

    if attach_after:
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"={session}"])
    return session
