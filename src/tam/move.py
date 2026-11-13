"""`tam move`: move the current window to another session, picked with fzf."""

from __future__ import annotations

import subprocess

from tam import paths, state
from tam.tmux import client_size, open_popup, session_names, tmux


def popup(client: str, window: str) -> int:
    name = tmux("display-message", "-p", "-t", window, "#{window_name}").strip()
    _, rows = client_size(client)
    open_popup(
        client,
        48,
        min(len(session_names()) + 4, rows),
        [paths.tam_executable(), "move", "--pick", "--client", client, "--window", window],
        title=f"move {name}",
    )
    return 0


def resolve(query: str, selection: str) -> str | None:
    """The destination session from fzf's query and selected line."""
    name = selection.strip() or query.strip()
    return name.replace(".", "_").replace(":", "_") or None


def move(client: str, window: str, destination: str) -> None:
    source = tmux("display-message", "-p", "-t", window, "#{session_name}").strip()
    if destination == source:
        return
    placeholder = None
    if destination not in session_names():
        cwd = tmux("display-message", "-p", "-t", window, "#{pane_current_path}").strip()
        placeholder = tmux(
            "new-session", "-d", "-P", "-F", "#{window_id}", "-s", destination, "-c", cwd
        ).strip()
    last_window = tmux("display-message", "-p", "-t", window, "#{session_windows}").strip() == "1"
    if last_window:
        # The source session disappears with its last window, and tmux would
        # detach the client. Follow the window instead.
        tmux("switch-client", "-c", client, "-t", f"={destination}:")
    tmux("move-window", "-s", window, "-t", f"={destination}:")
    if placeholder:
        tmux("kill-window", "-t", placeholder, check=False)
    if last_window:
        tmux("select-window", "-t", window, check=False)
    state.snapshot()


def pick(client: str, window: str) -> int:
    source = tmux("display-message", "-p", "-t", window, "#{session_name}").strip()
    others = sorted(n for n in session_names() if n != source)
    proc = subprocess.run(
        [
            "fzf",
            "--print-query",
            "--no-sort",
            "--layout=reverse",
            "--info",
            "hidden",
            "--prompt",
            "to session> ",
            "--header",
            "Enter: move · type a new name to create a session",
        ],
        input="\n".join(others),
        stdout=subprocess.PIPE,
        text=True,
    )
    if proc.returncode not in (0, 1):  # 130: cancelled
        return 0
    out = proc.stdout.split("\n")
    destination = resolve(out[0] if out else "", out[1] if len(out) > 1 else "")
    if destination:
        move(client, window, destination)
    return 0
