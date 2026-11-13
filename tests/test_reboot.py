"""End-to-end test against an isolated tmux server.

Each test gets its own TMUX_TMPDIR (socket), HOME, XDG_STATE_HOME and
XDG_CONFIG_HOME. A fake agent stands in for Claude.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

TAM = str(Path(sys.executable).parent / "tam")

FAKE_HARNESS = """
[harness.fake]
start = "sh -c 'echo STARTED {name}; exec sleep 1000'"
resume = "sh -c 'echo RESUMED {id}; exec sleep 1000'"
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    home = tmp_path / "home"
    (home / ".config" / "tam").mkdir(parents=True)
    (home / ".config" / "tam" / "harnesses.toml").write_text(FAKE_HARNESS)
    sock = Path("/tmp") / f"tam-test-{os.getpid()}-{tmp_path.name}"
    sock.mkdir()
    for k in ("TMUX", "TMUX_PANE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMUX_TMPDIR", str(sock))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("PATH", f"{Path(TAM).parent}:{os.environ['PATH']}")
    for d in ("a", "b", "c"):
        (tmp_path / d).mkdir()
    yield tmp_path
    subprocess.run(["tmux", "kill-server"], capture_output=True)
    shutil.rmtree(sock, ignore_errors=True)


def tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=True).stdout


def tam(*args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [TAM, *args], capture_output=True, text=True, env={**os.environ, **env}, check=True
    )


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def shape(sessions: list[dict]) -> list:
    """Structure of the saved state, ignoring names that automatic-rename changes."""
    return [
        (
            s["name"],
            [(w["index"], [(p["cwd"], p["agent"]) for p in w["panes"]]) for w in s["windows"]],
        )
        for s in sessions
    ]


def state_file(root: Path) -> Path:
    return root / "home" / ".local" / "state" / "tam" / "state.json"


def load_state(root: Path) -> dict:
    return json.loads(state_file(root).read_text())


def capture(target: str) -> str:
    return tmux("capture-pane", "-p", "-t", target)


def test_reboot_roundtrip(env):
    a, b, c = env / "a", env / "b", env / "c"

    # A session with a shell window split in two, plus an agent window.
    tmux("new-session", "-d", "-s", "work", "-x", "200", "-y", "50", "-c", str(a))
    tam("tmux-init")
    tmux("split-window", "-h", "-t", "=work:", "-c", str(b))
    first_pane = tmux("list-panes", "-t", "=work:", "-F", "#{pane_id}").split()[0]
    tam("new", "fake", "-n", "bot", "--target", first_pane)

    agent_pane = tmux("list-panes", "-a", "-F", "#{pane_id} #{window_name}")
    agent_pane = next(ln.split()[0] for ln in agent_pane.splitlines() if ln.endswith("bot"))
    wait_for(lambda: "STARTED bot" in capture(agent_pane))

    # The agent's hook reports its session id.
    tam("hook", "session", "sid-123", TMUX_PANE=agent_pane)
    tam("snapshot")

    st = load_state(env)
    work = next(s for s in st["sessions"] if s["name"] == "work")
    assert len(work["windows"]) == 2
    assert [p["cwd"] for p in work["windows"][0]["panes"]] == [str(a), str(b)]
    agent = work["windows"][1]["panes"][0]["agent"]
    assert agent["harness"] == "fake" and agent["session_id"] == "sid-123"
    saved = state_file(env).read_text()

    # Reboot: the server goes away without anyone closing panes.
    tmux("kill-server")
    time.sleep(0.5)
    assert load_state(env)["sessions"] == st["sessions"], "kill-server changed the state"

    # A plain tmux started after the reboot must not overwrite the state.
    tmux("new-session", "-d", "-s", "scratch", "-c", str(c))
    tam("tmux-init")
    tmux("new-window", "-t", "=scratch:")
    tam("snapshot")
    assert json.loads(state_file(env).read_text())["sessions"] == json.loads(saved)["sessions"]

    # Restore.
    tam("--no-attach")
    names = tmux("list-sessions", "-F", "#{session_name}").split()
    assert sorted(names) == ["scratch", "work"]
    panes = tmux("list-panes", "-s", "-t", "=work", "-F", "#{window_name}|#{pane_current_path}")
    lines = panes.splitlines()
    assert len(lines) == 3
    assert lines[1].endswith(str(b))
    widths = tmux("list-panes", "-t", "=work:0", "-F", "#{pane_width}").split()
    saved_widths = [str(c["w"]) for c in json.loads(work["windows"][0]["layout"])["L"]["c"]]
    assert widths == saved_widths

    # The agent pane waits, then resumes the same session on Enter.
    restored = tmux("list-panes", "-a", "-F", "#{pane_id} #{window_name}")
    restored_agent = next(ln.split()[0] for ln in restored.splitlines() if ln.endswith("bot"))
    wait_for(lambda: "resume fake?" in capture(restored_agent))
    tmux("send-keys", "-t", restored_agent, "Enter")
    wait_for(lambda: "RESUMED sid-123" in capture(restored_agent))

    # Saving is enabled again after restore and includes the scratch session.
    tam("snapshot")
    assert {s["name"] for s in load_state(env)["sessions"]} == {"scratch", "work"}


def test_agent_exit_prompts_and_resumes(env):
    tmux("new-session", "-d", "-s", "w", "-c", str(env / "a"))
    tam("tmux-init")
    pane = tmux("list-panes", "-t", "=w:", "-F", "#{pane_id}").strip()
    tam("new", "fake", "-n", "x", "--target", pane)
    agent = next(
        ln.split()[0]
        for ln in tmux("list-panes", "-a", "-F", "#{pane_id} #{window_name}").splitlines()
        if ln.endswith(":x")
    )
    wait_for(lambda: "STARTED x" in capture(agent))
    tam("hook", "session", "abc", TMUX_PANE=agent)
    tmux("send-keys", "-t", agent, "C-c")
    wait_for(lambda: "exited" in capture(agent))
    tmux("send-keys", "-t", agent, "Enter")
    wait_for(lambda: "RESUMED abc" in capture(agent))


def test_hooks_save_without_explicit_snapshot(env):
    tmux("new-session", "-d", "-s", "h", "-c", str(env / "a"))
    tam("tmux-init")
    tmux("new-window", "-t", "=h:", "-c", str(env / "b"))
    wait_for(
        lambda: state_file(env).exists() and len(load_state(env)["sessions"][0]["windows"]) == 2
    )
    tmux("split-window", "-t", "=h:", "-c", str(env / "c"))
    wait_for(lambda: sum(len(w["panes"]) for w in load_state(env)["sessions"][0]["windows"]) == 3)


def test_sigterm_to_server_keeps_state(env):
    tmux("new-session", "-d", "-s", "t", "-c", str(env / "a"))
    tam("tmux-init")
    tmux("new-window", "-t", "=t:")
    tam("snapshot")
    before = load_state(env)["sessions"]
    pid = int(tmux("display-message", "-p", "#{pid}").strip())
    os.kill(pid, 15)
    time.sleep(1.0)
    assert shape(load_state(env)["sessions"]) == shape(before)


def test_shutdown_sigterm_to_everything_keeps_agent_panes(env):
    """macOS shutdown sends SIGTERM to all processes, not only the tmux server."""
    tmux("new-session", "-d", "-s", "s", "-c", str(env / "a"))
    tam("tmux-init")
    pane = tmux("list-panes", "-t", "=s:", "-F", "#{pane_id}").strip()
    tam("new", "fake", "-n", "bot", "--target", pane)
    agent = next(
        ln.split()[0]
        for ln in tmux("list-panes", "-a", "-F", "#{pane_id} #{window_name}").splitlines()
        if ln.endswith(":bot")
    )
    wait_for(lambda: "STARTED bot" in capture(agent))
    tam("snapshot")
    before = load_state(env)["sessions"]

    server = int(tmux("display-message", "-p", "#{pid}").strip())
    pane_pids = tmux("list-panes", "-a", "-F", "#{pane_pid}").split()
    for pid in pane_pids:
        subprocess.run(["pkill", "-TERM", "-P", pid])
        subprocess.run(["kill", "-TERM", pid])
    time.sleep(0.3)
    os.kill(server, 15)
    time.sleep(3.0)
    assert shape(load_state(env)["sessions"]) == shape(before)
