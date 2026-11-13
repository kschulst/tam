"""Thin wrapper around the tmux CLI."""

from __future__ import annotations

import subprocess

SEP = "\x1f"


class TmuxError(RuntimeError):
    pass


def tmux(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def server_running() -> bool:
    return subprocess.run(["tmux", "list-sessions"], capture_output=True).returncode == 0


def query(cmd: list[str], fields: list[str]) -> list[dict[str, str]]:
    """Run a tmux list command and return one dict per output line."""
    fmt = SEP.join("#{" + f + "}" for f in fields)
    out = tmux(*cmd, "-F", fmt)
    rows = []
    for line in out.splitlines():
        rows.append(dict(zip(fields, line.split(SEP), strict=False)))
    return rows


def session_names() -> set[str]:
    if not server_running():
        return set()
    return {r["session_name"] for r in query(["list-sessions"], ["session_name"])}


def global_option(name: str) -> str:
    return tmux("show-options", "-gqv", name, check=False).strip()


def pane_option(pane: str, name: str) -> str:
    return tmux("show-options", "-pqv", "-t", pane, name, check=False).strip()


def set_pane_option(pane: str, name: str, value: str) -> None:
    tmux("set-option", "-p", "-t", pane, name, value)


def unset_pane_option(pane: str, name: str) -> None:
    tmux("set-option", "-pu", "-t", pane, name, check=False)


def client_size(client: str) -> tuple[int, int]:
    out = tmux("display-message", "-p", "-c", client, "#{client_width} #{client_height}")
    cols, rows = (int(v) for v in out.split())
    return cols, rows


def open_popup(client: str, width: int, height: int, argv: list[str]) -> None:
    """Open a popup running argv on client, clamped to the client's size.

    tmux refuses a popup larger than the client without showing an error, and
    does not expand formats in the popup command, so callers pass literal values.
    """
    import shlex

    cols, rows = client_size(client)
    tmux(
        "display-popup",
        "-c",
        client,
        "-E",
        "-w",
        str(min(width, cols)),
        "-h",
        str(min(height, rows)),
        " ".join(shlex.quote(a) for a in argv),
    )
