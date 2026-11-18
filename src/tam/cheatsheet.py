"""`tam help`: the cheat sheet."""

from __future__ import annotations

import os
import re
import subprocess

from tam import paths
from tam.tmux import global_option, server_running, tmux

_MODIFIERS = {"C": "Ctrl", "M": "Alt", "S": "Shift"}


def humanize(key: str) -> str:
    """tmux key notation to what is printed on the keyboard: C-b -> Ctrl+b."""
    parts = key.split("-")
    mods, base = parts[:-1], parts[-1] or "-"
    if not all(m in _MODIFIERS for m in mods):
        return key
    return "+".join([*(_MODIFIERS[m] for m in mods), base])


def _versions() -> str:
    out = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout.split()
    tmux_version = out[1] if len(out) > 1 else "?"
    return f"tam {paths.tam_version()} · tmux {tmux_version}"


def _prefix() -> str:
    raw = global_option("prefix") if server_running() else ""
    return humanize(raw or "C-b")


def _key(prefix: str, key: str) -> str:
    """How to press a tam key, or "" if the user has bound the key to something else."""
    from tam.setup import tam_key

    if server_running() and tam_key(key) is False:
        return ""
    return f"{prefix} → {key}"


def _keys(prefix: str, key: str) -> str:
    """Prefix key plus the Option shortcut when tam owns it: "⌥ a · Ctrl+b → a"."""
    from tam.setup import tam_key

    with_prefix = _key(prefix, key)
    if server_running() and tam_key(f"M-{key}", "root"):
        return f"⌥ {key} · {with_prefix}" if with_prefix else f"⌥ {key}"
    return with_prefix


def _quit_keys(prefix: str) -> str:
    """⌥ q when tam owns it, plus tmux's own prefix + d."""
    from tam.setup import tam_key

    detach = f"{prefix} → d"
    if server_running() and tam_key("M-q", "root"):
        return f"⌥ q · {detach}"
    return detach


def _option_note(client: str | None) -> str:
    """Which Option key the ⌥ shortcuts need in iTerm2, when it is not the left one."""
    from tam.setup import iterm_option_keys

    session = None
    if client:
        session = tmux("display-message", "-p", "-c", client, "#{client_session}", check=False)
    keys = iterm_option_keys(session=session.strip() if session else None)
    if keys is None or keys[1]:
        return ""
    if keys[2]:
        return "⌥ = the right Option key"
    return "⌥ shortcuts need Option as Esc+ in iTerm (see tam setup)"


def text(closing_hint: bool = False, client: str | None = None) -> str:
    from tam.harness import default_agent

    p = _prefix()
    agent = default_agent()
    new_window = _keys(p, "a") or "tam new"
    new_split = _key(p, "A") or "tam new --split h"
    sheet = _keys(p, "h") or "tam help"
    rows = [
        ("Agents", None),
        (new_window, f"new {agent} in a new window, pick directory"),
        ("Ctrl+v / Ctrl+s", "in the picker: split right / below"),
        (new_split, f"new {agent} in a split"),
        (_keys(p, "t") or "tam term", "new terminal, pick directory"),
        ("Tab in the picker", "use another agent this time"),
        ("tam agent <name>", "change the default agent"),
        ("tam adopt", "take over a Claude running outside tmux"),
        ("! tam adopt", "same, typed inside that Claude session"),
        ("tmux", None),
        (_quit_keys(p), "quit tam (agents keep running; tam attaches again)"),
        (_keys(p, "s") or "tam sidebar", "sessions and agents, switch with Enter"),
        (_key(p, ".") or "tam move", "move window to another session"),
        (f"{p} → w", "tmux tree of sessions and windows"),
        (f"{p} → n / p", "next / previous window"),
        (f"{p} → arrow", "move between panes"),
        (f"{p} → z", "zoom the pane (again to unzoom)"),
        ("After a reboot", None),
        ("tam", "restore and attach"),
    ]
    lines = [
        f" \033[1mtam cheat sheet\033[0m   {_versions()}",
    ]
    for left, right in rows:
        if right is None:
            lines += ["", f" \033[1m{left}\033[0m"]
        else:
            lines.append(f"   {left:<20} {right}")
    lines += [
        "",
        f" Open this sheet again: {sheet}",
    ]
    option = _option_note(client)
    if option:
        lines.append(f" {option}")
    if closing_hint:
        lines.append(" Press any key to close.")
    return "\n".join(lines)


_ANSI = re.compile(r"\033\[[0-9;]*m")


def popup(client: str | None) -> int:
    """Open the cheat sheet in a popup sized to its content."""
    from tam.tmux import open_popup

    if not client:
        client = tmux("display-message", "-p", "#{client_name}").strip()
    lines = text(closing_hint=True, client=client).splitlines()
    width = max(len(_ANSI.sub("", line)) for line in lines) + 4
    open_popup(
        client,
        width,
        len(lines) + 2,
        [paths.tam_executable(), "help", "--print", "--wait", "--client", client],
        title="cheat sheet",
    )
    return 0


def show(*, print_only: bool, wait: bool, client: str | None) -> int:
    if not print_only and (client or os.environ.get("TMUX")):
        return popup(client)
    print(text(closing_hint=wait, client=client))
    if wait:
        from tam.run import _key as read_key

        read_key()
    return 0
