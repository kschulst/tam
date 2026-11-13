"""Every built-in agent, end to end, with a fake binary that prints its arguments.

For each agent: it is detected when its program is on PATH, starts with the
`start` command, and after exiting resumes with the `resume` command.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from tam.harness import Harness, _parse

from .test_reboot import TAM, capture, env, tmux, wait_for  # noqa: F401  (env is a fixture)

BUILTINS = _parse(resources.files("tam").joinpath("harnesses.toml").read_text())


def fake_bin(directory: Path, program: str) -> None:
    path = directory / program
    path.write_text('#!/bin/sh\necho "ARGV: $(basename "$0") $*"\nexec sleep 1000\n')
    path.chmod(0o755)


def expected(template: str, name: str, session_id: str) -> str:
    parts = [p.replace("{name}", name).replace("{id}", session_id) for p in shlex.split(template)]
    return "ARGV: " + " ".join(parts)


def test_builtins_parse_and_name_a_program():
    assert len(BUILTINS) >= 10
    for key, value in BUILTINS.items():
        h = Harness(key, value["start"], value.get("resume"))
        program = shlex.split(h.start)[0]
        assert program and "{" not in program, key
        if h.resume:
            assert shlex.split(h.resume)[0] == program, f"{key}: resume uses another program"


def test_detection_follows_path(tmp_path, monkeypatch):
    from tam.harness import installed_names

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    assert installed_names() == []
    for program in ("cursor-agent", "goose", "qwen"):
        fake_bin(bin_dir, program)
    assert installed_names() == ["qwen", "cursor", "goose"]


@pytest.mark.parametrize("agent", sorted(BUILTINS))
def test_start_exit_resume(env, agent):  # noqa: F811
    value = BUILTINS[agent]
    bin_dir = env / "bin"
    bin_dir.mkdir()
    fake_bin(bin_dir, shlex.split(value["start"])[0])
    path = f"{bin_dir}:{os.environ['PATH']}"

    tmux("new-session", "-d", "-s", "h", "-c", str(env / "a"))
    tmux("set-environment", "-g", "PATH", path)
    pane = tmux("list-panes", "-t", "h:", "-F", "#{pane_id}").strip()
    subprocess.run(
        [TAM, "new", agent, "-n", "x", "--target", pane],
        check=True,
        env={**os.environ, "PATH": path},
    )
    agent_pane = next(
        ln.split()[0]
        for ln in tmux("list-panes", "-a", "-F", "#{pane_id} #{window_name}").splitlines()
        if ln.endswith(":x")
    )
    wait_for(lambda: expected(value["start"], "x", "") in capture(agent_pane), show=agent_pane)

    resume = value.get("resume")
    if resume and "{id}" in resume:
        subprocess.run(
            [TAM, "hook", "session", "sid-1"],
            check=True,
            env={**os.environ, "TMUX_PANE": agent_pane},
        )
    tmux("send-keys", "-t", agent_pane, "C-c")
    wait_for(lambda: "exited" in capture(agent_pane), show=agent_pane)
    tmux("send-keys", "-t", agent_pane, "Enter")
    again = expected(resume or value["start"], "x", "sid-1")
    wait_for(
        lambda: capture(agent_pane).count("ARGV:") == 2 and again in capture(agent_pane),
        show=agent_pane,
    )
