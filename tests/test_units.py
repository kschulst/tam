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
        '[harness.myagent]\nstart = "myagent"\n'
    )
    h = load()
    assert h["claude"].start.startswith("claude --permission-mode")
    assert h["claude"].resume == "claude --resume {id}"
    assert h["myagent"].resume is None


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


def test_user_bindings_are_kept_and_cheatsheet_follows(env):  # noqa: F811
    tmux("new-session", "-d", "-s", "b")
    tmux("bind-key", "h", "select-pane", "-L")
    subprocess.run([TAM, "tmux-init"], check=True)
    assert "select-pane -L" in tmux("list-keys", "-T", "prefix", "h")
    assert "tam: new agent" in tmux("list-keys", "-N", "-T", "prefix", "a")

    sheet = subprocess.run(
        [TAM, "help", "--print"],
        capture_output=True,
        text=True,
        check=True,
        env={k: v for k, v in os.environ.items() if k != "TMUX"},
    ).stdout
    assert "Ctrl+b → a" in sheet
    assert "Open this sheet again: ⌥ h\n" in sheet + "\n"

    # Running tmux-init again keeps tam's own bindings.
    subprocess.run([TAM, "tmux-init"], check=True)
    assert "tam: new agent" in tmux("list-keys", "-N", "-T", "prefix", "a")


def test_humanize_prefix():
    from tam.cheatsheet import humanize

    assert humanize("C-b") == "Ctrl+b"
    assert humanize("C-a") == "Ctrl+a"
    assert humanize("M-Space") == "Alt+Space"
    assert humanize("C-M-x") == "Ctrl+Alt+x"
    assert humanize("`") == "`"


def test_dir_candidates_order_and_resolve(tmp_path, monkeypatch):
    from tam import dirs

    claude = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(dirs, "_zoxide_dirs", lambda: [])
    old, recent, here = (tmp_path / n for n in ("old", "recent", "here"))
    for d in (old, recent, here):
        d.mkdir()
    for name, cwd, mtime in (
        ("p1", old, 1000),
        ("p2", recent, 2000),
        ("p3", tmp_path / "gone", 3000),
    ):
        f = claude / "projects" / name / "s.jsonl"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"type": "user", "cwd": str(cwd), "message": {"content": "x"}}))
        os.utime(f, (mtime, mtime))

    # Current first, then by last use; directories that no longer exist are left out.
    assert dirs.candidates(str(here)) == [str(here), str(recent), str(old)]

    assert dirs.resolve("", str(recent)) == str(recent)
    assert dirs.resolve(str(old), "") == str(old)
    assert dirs.resolve(str(tmp_path / "nope"), "") is None


def test_new_with_dir(env):  # noqa: F811
    tmux("new-session", "-d", "-s", "d", "-c", str(env / "a"))
    pane = tmux("list-panes", "-t", "=d:", "-F", "#{pane_id}").strip()
    subprocess.run(
        [TAM, "new", "fake", "-n", "x", "-C", str(env / "c"), "--target", pane], check=True
    )
    wait_for(lambda: str(env / "c") in tmux("list-panes", "-a", "-F", "#{pane_current_path}"))
    bad = subprocess.run([TAM, "new", "fake", "-C", str(env / "nope"), "--target", pane])
    assert bad.returncode == 1


def test_brewfile_lists_required_tools():
    from tam.setup import REQUIRED_TOOLS, brewfile, brewfile_tools

    names = [name for name, _ in brewfile_tools(brewfile())]
    assert set(names) >= REQUIRED_TOOLS
    assert all(desc for _, desc in brewfile_tools(brewfile()))


def test_claude_hooks_all_events_idempotent_and_upgrade_old_install(tmp_path):
    from tam.setup import CLAUDE_HOOKS

    settings = tmp_path / "settings.json"
    # An install from an older tam version: only SessionStart.
    old = "command -v tam >/dev/null 2>&1 && tam hook claude || true"
    settings.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"command": old}]}]}}))
    assert install_claude_hook(settings) is True
    assert install_claude_hook(settings) is False
    hooks = json.loads(settings.read_text())["hooks"]
    for event, command in CLAUDE_HOOKS.items():
        commands = [h["command"] for g in hooks[event] for h in g["hooks"]]
        assert commands == [command], event


def test_sidebar_lines():
    from tam.sidebar import lines

    def row(s, w, name, harness="", status=""):
        return {
            "session_name": s,
            "window_index": w,
            "window_name": name,
            "@tam_harness": harness,
            "@tam_status": status,
        }

    rows = [
        row("api", "1", "api:claude", "claude", "working"),
        row("api", "1", "api:claude", "claude", "waiting"),  # split: most urgent wins
        row("api", "2", "zsh"),
        row("web", "1", "web:codex", "codex", ""),
    ]
    out, selected = lines(rows, ("api", "2"))
    targets = [ln.split("\t")[0] for ln in out]
    assert targets == ["=api:", "=api:1", "=api:2", "=web:", "=web:1"]
    assert "waiting" in out[1] and "running" in out[4]
    assert selected == 2


def test_tmux_default_binding_is_replaced(env):  # noqa: F811
    tmux("new-session", "-d", "-s", "d")
    assert "choose-tree" in tmux("list-keys", "-T", "prefix", "s")
    subprocess.run([TAM, "tmux-init"], check=True)
    assert "tam: sessions" in tmux("list-keys", "-N", "-T", "prefix", "s")


def test_option_shortcuts_in_root_table(env):  # noqa: F811
    tmux("new-session", "-d", "-s", "o")
    tmux("bind-key", "-n", "M-a", "display-message", "mine")
    subprocess.run([TAM, "tmux-init"], check=True)
    assert "tam: sessions" in tmux("list-keys", "-N", "-T", "root", "M-s")
    assert "display-message mine" in tmux("list-keys", "-T", "root", "M-a")
    sheet = subprocess.run(
        [TAM, "help", "--print"],
        capture_output=True,
        text=True,
        check=True,
        env={k: v for k, v in os.environ.items() if k != "TMUX"},
    ).stdout
    assert "⌥ s · Ctrl+b → s" in sheet
    assert "Open this sheet again: ⌥ h · Ctrl+b → h" in sheet
    assert "⌥ a ·" not in sheet  # the user's own M-a binding is kept


def test_sidebar_close_needs_two_clicks(env, monkeypatch):  # noqa: F811
    from tam import sidebar

    tmux("new-session", "-d", "-s", "s1")
    tmux("new-window", "-t", "s1:", "-n", "victim")
    tmux("new-session", "-d", "-s", "s2")
    monkeypatch.setattr(sidebar, "_current", lambda client: ("s1", "0"))
    target = "=s1:1"

    assert "victim" in sidebar.footer(target)
    assert sidebar.click("c", target, "tam").startswith("change-footer(")
    assert "victim" in tmux("list-windows", "-t", "s1", "-F", "#{window_name}")
    # Focusing another line disarms the button.
    sidebar.footer("=s2:")
    assert sidebar.click("c", target, "tam").startswith("change-footer(")
    assert sidebar.click("c", target, "tam").startswith("reload(")
    assert "victim" not in tmux("list-windows", "-t", "s1", "-F", "#{window_name}")

    assert "session s2 (1 window)" in sidebar.footer("=s2:")
    sidebar.click("c", "=s2:", "tam")
    sidebar.click("c", "=s2:", "tam")
    assert tmux("list-sessions", "-F", "#{session_name}").split() == ["s1"]


def test_move_window(env, monkeypatch):  # noqa: F811
    from tam import move

    tmux("new-session", "-d", "-s", "src")
    tmux("new-window", "-t", "src:", "-n", "mover")
    tmux("new-session", "-d", "-s", "dst")
    win = tmux("display-message", "-p", "-t", "src:mover", "#{window_id}").strip()

    move.move("c", win, "dst")
    assert "mover" in tmux("list-windows", "-t", "dst", "-F", "#{window_name}")

    # A new destination name creates the session, without an extra empty window.
    move.move("c", win, "fresh")
    assert tmux("list-windows", "-t", "fresh", "-F", "#{window_name}").split() == ["mover"]

    assert move.resolve("", "dst") == "dst"
    assert move.resolve("my.new", "") == "my_new"
    assert move.resolve("", "") is None


def test_default_agent_detection_and_switch(tmp_path, monkeypatch):
    from tam import config, harness

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("codex", "aider"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    assert harness.installed_names() == ["codex", "aider"]
    assert harness.default_agent() == "codex"  # claude is not installed
    config.set("default_agent", "aider")
    assert harness.default_agent() == "aider"
    config.set("default_agent", "claude")  # not installed: fall back
    assert harness.default_agent() == "codex"


def test_picker_tab_cycles_installed_agents(tmp_path, monkeypatch):
    from tam import dirs, harness

    monkeypatch.setattr(harness, "installed_names", lambda: ["claude", "codex"])
    state = tmp_path / "agent"
    state.write_text("claude")
    assert dirs.cycle_agent(str(state)) == "change-prompt(new codex in> )"
    assert dirs.cycle_agent(str(state)) == "change-prompt(new claude in> )"


def test_picker_split_keys(tmp_path, monkeypatch):
    from tam import dirs

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(dirs, "candidates", lambda current: [current])
    target = tmp_path / "proj"
    target.mkdir()

    def fzf_output(text):
        class Proc:
            returncode = 0
            stdout = text

        return lambda *a, **k: Proc()

    for key, split in (("", None), ("ctrl-v", "h"), ("ctrl-s", "v")):
        monkeypatch.setattr(dirs.subprocess, "run", fzf_output(f"\n{key}\n{target}\n"))
        assert dirs.pick(str(tmp_path), None) == (str(target), None, split)


def test_term_opens_shell_window_and_split(env):  # noqa: F811
    tmux("new-session", "-d", "-s", "t", "-c", str(env / "a"))
    pane = tmux("list-panes", "-t", "t:", "-F", "#{pane_id}").strip()
    subprocess.run([TAM, "term", "-C", str(env / "b"), "--target", pane], check=True)
    subprocess.run([TAM, "term", "--split", "v", "--target", pane], check=True, cwd=env / "c")
    panes = tmux("list-panes", "-s", "-t", "t", "-F", "#{window_index} #{@tam_harness}").split("\n")
    assert len([p for p in panes if p]) == 3  # shell, split shell, new window shell
    assert all(not p.split(" ", 1)[1:] or not p.split(" ", 1)[1] for p in panes if p)
    windows = tmux("list-windows", "-t", "t", "-F", "#{window_panes}").split()
    assert windows == ["2", "1"]


def test_terminal_title_shows_session(env):  # noqa: F811
    tmux("new-session", "-d", "-s", "api")
    subprocess.run([TAM, "tmux-init"], check=True)
    assert tmux("show-options", "-gv", "set-titles").strip() == "on"
    title = tmux("show-options", "-gv", "set-titles-string").strip()
    assert tmux("display-message", "-p", "-t", "api:", title).strip() == "tam:api"


def test_iterm_option_keys(tmp_path):
    import plistlib

    from tam.setup import iterm_option_keys

    plist = tmp_path / "iterm.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Default Bookmark Guid": "b",
                "New Bookmarks": [
                    {"Name": "Default", "Guid": "a", "Option Key Sends": 2},
                    {
                        "Name": "Mine",
                        "Guid": "b",
                        "Option Key Sends": 0,
                        "Right Option Key Sends": 2,
                    },
                ],
            }
        )
    )
    assert iterm_option_keys(plist, "Default") == ("Default", True, False)
    assert iterm_option_keys(plist, None) == ("Mine", False, True)  # default profile
    assert iterm_option_keys(tmp_path / "missing.plist", None) is None
    assert iterm_option_keys(plist, "Dynamic") is None  # not in the plist


def test_iterm_profile_follows_the_session(env):  # noqa: F811
    from tam.setup import iterm_profile

    tmux("new-session", "-d", "-s", "api")
    subprocess.run([TAM, "tmux-init"], check=True)
    assert "ITERM_PROFILE" in tmux("show-options", "-gv", "update-environment").split()
    assert iterm_profile("api") is None  # no client has attached from iTerm2
    tmux("set-environment", "-t", "api", "ITERM_PROFILE", "Mine")
    assert iterm_profile("api") == "Mine"
    tmux("set-environment", "-t", "api", "-r", "ITERM_PROFILE")  # client outside iTerm2
    assert iterm_profile("api") is None
