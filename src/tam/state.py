"""Saving the tmux server to state.json and recreating it after a reboot."""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path

from tam import paths
from tam.tmux import query, session_names, tmux

RESTORED_FLAG = "@tam_restored"

PANE_FIELDS = [
    "session_name",
    "session_last_attached",
    "session_active",
    "window_index",
    "window_name",
    "window_layout",
    "window_active",
    "automatic-rename",
    "pane_index",
    "pane_active",
    "pane_current_path",
    "@tam_harness",
    "@tam_name",
    "@tam_args",
    "@tam_session_id",
    "@tam_started",
]


# Saving


def snapshot_enabled() -> bool:
    from tam.tmux import global_option

    return global_option(RESTORED_FLAG) == "1"


def capture() -> dict:
    """Read the running tmux server into the state structure."""
    rows = query(["list-panes", "-a"], PANE_FIELDS)
    sessions: dict[str, dict] = {}
    last_attached: dict[str, int] = {}
    for r in rows:
        s = sessions.setdefault(r["session_name"], {"name": r["session_name"], "windows": {}})
        last_attached[r["session_name"]] = int(r["session_last_attached"] or 0)
        w = s["windows"].setdefault(
            r["window_index"],
            {
                "index": int(r["window_index"]),
                "name": r["window_name"],
                "layout": r["window_layout"],
                "active": r["window_active"] == "1",
                "auto_rename": r["automatic-rename"] == "1",
                "panes": [],
            },
        )
        pane: dict = {
            "index": int(r["pane_index"]),
            "cwd": r["pane_current_path"],
            "active": r["pane_active"] == "1",
            "agent": None,
        }
        if r["@tam_harness"]:
            pane["agent"] = {
                "harness": r["@tam_harness"],
                "name": r["@tam_name"],
                "args": json.loads(r["@tam_args"] or "[]"),
                "session_id": r["@tam_session_id"],
                "started": r["@tam_started"] == "1",
            }
        w["panes"].append(pane)

    out_sessions = []
    for s in sessions.values():
        windows = sorted(s["windows"].values(), key=lambda w: w["index"])
        for w in windows:
            w["panes"].sort(key=lambda p: p["index"])
        out_sessions.append({"name": s["name"], "windows": windows})
    last = max(last_attached, key=lambda k: last_attached[k]) if last_attached else None
    return {
        "version": 1,
        "saved_at": int(time.time()),
        "last_session": last,
        "sessions": out_sessions,
    }


def write(state: dict) -> None:
    path = paths.state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        new_text = json.dumps(state, indent=2)
        if path.exists():
            old = path.read_text()
            if _without_timestamp(old) == _without_timestamp(new_text):
                return
            path.with_name("state.prev.json").write_text(old)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(new_text)
        os.replace(tmp, path)


def _without_timestamp(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return text
    data.pop("saved_at", None)
    return json.dumps(data, sort_keys=True)


def snapshot() -> None:
    if not snapshot_enabled():
        return
    write(capture())


def load(path: Path | None = None) -> dict | None:
    path = path or paths.state_file()
    if not path.exists():
        return None
    return json.loads(path.read_text())


# Restoring


def restore(tam_exe: str, fallback_session: str, cwd: str) -> str:
    """Create every saved session that does not exist. Returns the session to attach to.

    With nothing saved and no server running, a session named `fallback_session`
    is created in `cwd`, so the tmux server exists before saving is enabled.
    """
    state = load()
    existing = session_names()
    last = None
    if state:
        for s in state["sessions"]:
            if s["name"] in existing:
                continue
            _create_session(s, tam_exe)
        last = state.get("last_session")
    names = session_names()
    if not names:
        tmux("new-session", "-d", "-s", fallback_session, "-c", cwd)
        names = {fallback_session}
    tmux("set-option", "-g", RESTORED_FLAG, "1")
    snapshot()
    if last in names:
        return last
    return min(names)


_LAYOUT_SIZE = re.compile(r"^[0-9a-f]{4},(\d+)x(\d+)")


def layout_size(layout: str) -> tuple[int, int] | None:
    """Window size from a layout. tmux 3.8 uses JSON, older versions `csum,WxH,...`."""
    if layout.startswith("{"):
        try:
            root = json.loads(layout)["L"]
            return int(root["w"]), int(root["h"])
        except (ValueError, KeyError, TypeError):
            return None
    m = _LAYOUT_SIZE.match(layout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _pane_command(pane: dict, tam_exe: str, cwd_missing: str | None) -> list[str]:
    agent = pane.get("agent")
    if not agent:
        return []
    cmd = [tam_exe, "run", "--wait", "--harness", agent["harness"], "--name", agent["name"]]
    if agent.get("session_id"):
        cmd += ["--session-id", agent["session_id"]]
    if agent.get("started"):
        cmd.append("--started")
    if cwd_missing:
        cmd += ["--missing-cwd", cwd_missing]
    return cmd + ["--", *agent.get("args", [])]


def _cwd(pane: dict) -> tuple[str, str | None]:
    cwd = pane["cwd"]
    if cwd and os.path.isdir(cwd):
        return cwd, None
    return str(Path.home()), cwd


def _create_session(s: dict, tam_exe: str) -> None:
    name = s["name"]
    windows = s["windows"]
    if not windows:
        return
    first = windows[0]
    dims = layout_size(first["layout"])
    size = ["-x", str(dims[0]), "-y", str(dims[1])] if dims else []
    cwd, missing = _cwd(first["panes"][0])
    created_index = tmux(
        "new-session",
        "-d",
        "-P",
        "-F",
        "#{window_index}",
        "-s",
        name,
        *size,
        "-c",
        cwd,
        "-n",
        first["name"],
        *_pane_command(first["panes"][0], tam_exe, missing),
    ).strip()
    target = f"={name}:"
    if created_index != str(first["index"]):
        tmux("move-window", "-s", f"{target}{created_index}", "-t", f"{target}{first['index']}")
    _fill_window(name, first, tam_exe)

    for w in windows[1:]:
        cwd, missing = _cwd(w["panes"][0])
        tmux(
            "new-window",
            "-d",
            "-t",
            f"{target}{w['index']}",
            "-c",
            cwd,
            "-n",
            w["name"],
            *_pane_command(w["panes"][0], tam_exe, missing),
        )
        _fill_window(name, w, tam_exe)

    active = next((w for w in windows if w["active"]), windows[0])
    tmux("select-window", "-t", f"{target}{active['index']}")


def _fill_window(session: str, w: dict, tam_exe: str) -> None:
    win = f"={session}:{w['index']}"
    # Pane ids, in saved order. Indexes are not used because pane-base-index may differ.
    pane_ids = [tmux("list-panes", "-t", win, "-F", "#{pane_id}").split()[0]]
    for pane in w["panes"][1:]:
        cwd, missing = _cwd(pane)
        # Keep room for the next split; the saved layout is applied afterwards.
        tmux("select-layout", "-t", win, "tiled")
        pane_ids.append(
            tmux(
                "split-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                pane_ids[-1],
                "-c",
                cwd,
                *_pane_command(pane, tam_exe, missing),
            ).strip()
        )
    if len(pane_ids) > 1:
        tmux("select-layout", "-t", win, w["layout"], check=False)
    if w.get("auto_rename"):
        tmux("set-option", "-w", "-t", win, "automatic-rename", "on")
    active = next((i for i, p in enumerate(w["panes"]) if p["active"]), 0)
    tmux("select-pane", "-t", pane_ids[active])
