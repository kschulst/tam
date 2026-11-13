from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tam.harness import Harness, load
from tam.setup import install_claude_hook
from tam.state import layout_size

from .test_reboot import TAM, env, tmux, wait_for  # noqa: F401  (env is a fixture)

CLAUDE = Harness("claude", start="claude -n {name}", resume="claude --resume {id}")
CODEX = Harness("codex", start="codex", resume="codex resume --last")


def test_claude_starts_without_id_and_resumes_with_id():
    argv, resuming = CLAUDE.command(name="api", session_id="", started=True, args=[])
    assert (argv, resuming) == (["claude", "-n", "api"], False)
    argv, resuming = CLAUDE.command(name="api", session_id="s1", started=True, args=["--x"])
    assert (argv, resuming) == (["claude", "--resume", "s1", "--x"], True)


def test_resume_without_id_requires_a_previous_start():
    assert CODEX.command(name="c", session_id="", started=False, args=[]) == (["codex"], False)
    assert CODEX.command(name="c", session_id="", started=True, args=[]) == (
        ["codex", "resume", "--last"],
        True,
    )


def test_user_config_overrides_builtin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "tam").mkdir()
    (tmp_path / "tam" / "harnesses.toml").write_text(
        '[harness.claude]\nstart = "claude --permission-mode acceptEdits -n {name}"\n'
        '[harness.aider]\nstart = "aider"\n'
    )
    h = load()
    assert h["claude"].start.startswith("claude --permission-mode")
    assert h["claude"].resume == "claude --resume {id}"
    assert h["aider"].resume is None


def test_install_claude_hook_is_idempotent_and_keeps_other_hooks(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"model": "x", "hooks": {"SessionStart": [{"hooks": [{"command": "other"}]}]}})
    )
    assert install_claude_hook(settings) is True
    assert install_claude_hook(settings) is False
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    commands = [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]
    assert commands[0] == "other" and "tam hook claude" in commands[1]
    assert (tmp_path / "settings.json.tam-backup").exists()


def test_layout_size_both_formats():
    assert layout_size('{"V":2,"L":{"t":"p","w":80,"h":24}}') == (80, 24)
    assert layout_size("b25d,200x50,0,0{100x50,0,0,1,99x50,101,0,2}") == (200, 50)
    assert layout_size("garbage") is None


def test_claude_hook_sets_session_on_tam_pane(env):  # noqa: F811
    tmux("new-session", "-d", "-s", "k")
    subprocess.run([TAM, "tmux-init"], check=True)
    pane = tmux("list-panes", "-t", "=k:", "-F", "#{pane_id}").strip()
    payload = json.dumps({"session_id": "real-id", "cwd": "/x", "hook_event_name": "SessionStart"})

    # A pane that tam does not manage is left alone.
    subprocess.run(
        [TAM, "hook", "claude"],
        input=payload,
        text=True,
        check=True,
        env={**os.environ, "TMUX_PANE": pane},
    )
    assert tmux("show-options", "-pqv", "-t", pane, "@tam_session_id").strip() == ""

    tmux("set-option", "-p", "-t", pane, "@tam_harness", "claude")
    subprocess.run(
        [TAM, "hook", "claude"],
        input=payload,
        text=True,
        check=True,
        env={**os.environ, "TMUX_PANE": pane},
    )
    assert tmux("show-options", "-pqv", "-t", pane, "@tam_session_id").strip() == "real-id"


def test_hook_without_tmux_exits_zero():
    proc = subprocess.run(
        [str(Path(sys.executable).parent / "tam"), "hook", "claude"],
        input="not json",
        text=True,
        capture_output=True,
        env={k: v for k, v in os.environ.items() if k != "TMUX_PANE"},
    )
    assert proc.returncode == 0
