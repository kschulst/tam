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


def _hold(key: str) -> str:
    """Ctrl+b -> "Ctrl and press b"."""
    *mods, base = key.split("+")
    return f"{' and '.join(mods)} and press {base}" if mods else f"press {base}"


def _prefix() -> str:
    raw = global_option("prefix") if server_running() else ""
    return humanize(raw or "C-b")


def _key(prefix: str, key: str) -> str:
    """How to press a tam key, or "" if the user has bound the key to something else."""
    from tam.setup import tam_key

    if server_running() and tam_key(key) is False:
        return ""
    return f"{prefix} → {key}"


def text(closing_hint: bool = False) -> str:
    p = _prefix()
    new_window = _key(p, "a") or "tam new"
    new_split = _key(p, "A") or "tam new --split h"
    sheet = _key(p, "h") or "tam help"
    rows = [
        ("Agents", None),
        (new_window, "new Claude in a new window, pick directory"),
        (new_split, "new Claude in a split"),
        ("tam new codex", "another agent (see harnesses.toml)"),
        ("tam adopt", "take over a Claude running outside tmux"),
        ("! tam adopt", "same, typed inside that Claude session"),
        ("Agent pane after exit or reboot", None),
        ("Enter", "resume the conversation"),
        ("q", "close the pane"),
        ("tmux", None),
        (_key(p, "s") or "tam sidebar", "sessions and agents, switch with Enter"),
        (f"{p} → w", "tmux tree of sessions and windows"),
        (f"{p} → n / p", "next / previous window"),
        (f"{p} → arrow", "move between panes"),
        (f"{p} → z", "zoom the pane (again to unzoom)"),
        (f"{p} → d", "detach (tmux keeps running)"),
        ("After a reboot", None),
        ("tam", "restore and attach"),
    ]
    lines = [
        f" \033[1mtam cheat sheet\033[0m   {_versions()}",
        "",
        f" {p} → a  =  hold {_hold(p)}, let go, then press a",
    ]
    for left, right in rows:
        if right is None:
            lines += ["", f" \033[1m{left}\033[0m"]
        else:
            lines.append(f"   {left:<20} {right}")
    lines += [
        "",
        f" Open this sheet again: {sheet}",
        " Copy text: hold ⌥ Option while selecting with the mouse",
    ]
    if closing_hint:
        lines.append(" Press any key to close.")
    return "\n".join(lines)


_ANSI = re.compile(r"\033\[[0-9;]*m")


def popup(client: str | None) -> int:
    """Open the cheat sheet in a popup sized to its content."""
    from tam.tmux import open_popup

    if not client:
        client = tmux("display-message", "-p", "#{client_name}").strip()
    lines = text(closing_hint=True).splitlines()
    width = max(len(_ANSI.sub("", line)) for line in lines) + 4
    open_popup(
        client,
        width,
        len(lines) + 2,
        [paths.tam_executable(), "help", "--print", "--wait"],
        title="cheat sheet",
    )
    return 0


def show(*, print_only: bool, wait: bool, client: str | None) -> int:
    if not print_only and (client or os.environ.get("TMUX")):
        return popup(client)
    print(text(closing_hint=wait))
    if wait:
        from tam.run import _key as read_key

        read_key()
    return 0
