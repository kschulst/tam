from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
    rows[0] |= {"pane_title": "✳ Fix the login form", "window_activity": "1000"}
    rows[2] |= {"pane_current_command": "vim", "pane_active": "1"}
    out, selected = lines(rows, ("api", "2"), now=1000 + 20 * 60, commands={})
    targets = [item.split("\t")[0] for item in out]
    assert targets == ["=api:", "=api:1", "=api:2", "=web:", "=web:1"]
    cards = [item for item, t in zip(out, targets, strict=True) if not t.endswith(":")]
    assert [len(c.splitlines()) for c in cards] == [3, 3, 3]
    name, what, meta = out[1].split("\t", 1)[1].splitlines()
    # An agent's card is named after its topic; the agent is on the second line.
    assert "Fix the login form" in name and what.strip().startswith("claude")
    assert "waiting" in meta and "2 panes" in meta and "20m ago" in meta
    assert "terminal" in out[2] and "vim" in out[2] and "panes" not in out[2]
    assert "running" in out[4]
    assert selected == 2


def test_sidebar_terminal_card_names_directory_and_command():
    from tam.sidebar import card, short_path

    home = str(Path.home())
    pane = {
        "pane_current_path": f"{home}/git/tam/src",
        "pane_current_command": "zsh",
        "pane_tty": "/dev/ttys007",
    }
    w = {"name": "zsh", "auto_named": True, "statuses": [], "pane": pane, "agent_pane": None}
    name, what, _ = card(w, 0, {"ttys007": "-zsh"}).splitlines()
    assert "src" in name and "zsh" not in name  # the shell's name says nothing
    assert what.strip() == "~/git/tam/src"  # a waiting shell shows its directory
    _, what, _ = card(w, 0, {"ttys007": "/opt/homebrew/bin/node vite --port 3000"}).splitlines()
    assert what.strip() == "node vite --port 3000"
    named = {**w, "name": "zsh", "auto_named": False}  # named by the user: kept
    assert "zsh" in card(named, 0, {}).splitlines()[0]

    assert short_path(f"{home}/a/b/c", 40) == "~/a/b/c"
    assert short_path("/very/long/path/to/some/project/dir", 20) == "…/some/project/dir"


def test_pr_refresh_and_lookup_use_a_cache(tmp_path, monkeypatch):
    from tam import pr

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'echo "$@" >> "$(dirname "$0")/calls"\n'
        'echo \'[{"number": 42, "url": "https://example.com/pr/42", "isDraft": false}]\'\n'
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    started = []
    monkeypatch.setattr(pr, "_refresh_in_background", lambda *a: started.append(a[:2]))

    assert pr.lookup("/repo", "main") is None and not started  # no PRs from main
    assert pr.lookup("/repo", "feat/x") is None  # nothing cached: a refresh starts
    assert started == [("/repo", "feat/x")]
    pr.refresh(str(tmp_path), "feat/x")  # what the background process runs
    assert "--head feat/x --state open" in (bin_dir / "calls").read_text()
    started.clear()
    found = pr.lookup(str(tmp_path), "feat/x")
    assert found == {"number": 42, "url": "https://example.com/pr/42", "draft": False}
    assert not started  # fresh cache: no new refresh
    pr.lookup(str(tmp_path), "feat/x", now=time.time() + pr.TTL + 1)
    assert started == [(str(tmp_path), "feat/x")]  # stale: refresh, still answers

    gh.write_text("#!/bin/sh\nexit 1\n")  # gh fails: cached as no PR
    pr.refresh(str(tmp_path), "feat/y")
    assert pr.lookup(str(tmp_path), "feat/y") is None


def test_pr_lookup_without_gh(tmp_path, monkeypatch):
    from tam import pr

    monkeypatch.setenv("PATH", str(tmp_path))
    assert pr.lookup("/repo", "feat/x") is None


def test_sidebar_card_links_the_pr(tmp_path):
    from tam.sidebar import card

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/cards\n")
    pane = {"pane_current_path": str(repo), "pane_current_command": "vim"}
    w = {"name": "w", "statuses": [], "pane": pane, "agent_pane": None}
    seen = []

    def pr_of(root, branch):
        seen.append((root, branch))
        return {"number": 7, "url": "https://example.com/pr/7", "draft": False}

    lines = card(w, 0, {}, pr_of).splitlines()
    assert seen == [(str(repo), "feat/cards")]
    assert "feat/cards" in lines[2]
    # A fourth line with the link: Cmd+click opens it.
    assert "\033]8;;https://example.com/pr/7\033\\PR #7\033]8;;\033\\" in lines[3]
    assert len(card(w, 0, {}).splitlines()) == 3  # no pr_of: no links line


def test_sidebar_agent_card_links_open_issues_and_prs_and_follows_the_topic():
    from tam.sidebar import card

    agent = {
        "pane_id": "%5",
        "pane_title": "✳ Fix the login form",
        "pane_current_path": str(Path.home() / "git" / "app"),
        "@tam_harness": "claude",
    }
    w = {"name": "app:claude", "statuses": ["waiting"], "pane": agent, "agent_pane": agent}
    refs = [
        {"url": "https://github.com/o/r/pull/12", "number": 12, "kind": "pr"},
        {"url": "https://github.com/o/r/issues/40", "number": 40, "kind": "issue"},
        {"url": "https://github.com/o/r/issues/41", "number": 41, "kind": "issue"},
        {"url": "https://github.com/o/r/issues/42", "number": 42, "kind": "issue"},
    ]
    asked = []

    def refs_of(pane):
        asked.append(pane)
        return refs

    name, what, _, links = card(w, 0, {}, None, False, refs_of).splitlines()
    assert asked == ["%5"]
    assert "Fix the login form" in name and "app:claude" not in name  # the topic names it
    assert what.strip() == "claude · ~/git/app"
    assert "PR #12" in links and "#40" in links and "#41" in links and "#42" not in links
    # Renamed with Ctrl+r: the name stays, the topic moves to the second line.
    name, what, *_ = card({**w, "named": True}, 0, {}, None, False, refs_of).splitlines()
    assert "app:claude" in name and what.strip() == "Fix the login form"
    # A title that is only the program's name is not a topic yet.
    fresh = {**agent, "pane_title": "claude"}
    name, *_ = card({**w, "pane": fresh, "agent_pane": fresh}, 0, {}).splitlines()
    assert "app:claude" in name


def test_sidebar_a_claude_started_by_hand_gets_an_agent_card(monkeypatch):
    from tam import sidebar

    monkeypatch.setattr(sidebar, "_PROGRAMS", {"claude": "claude"})
    rows = [
        {
            "session_name": "inc",
            "window_index": "2",
            "window_name": "claude",
            "pane_current_command": "claude",
            "pane_title": "INC0016411 i ServiceNow",
            "pane_current_path": str(Path.home() / "kenneth" / "inc"),
            "@tam_harness": "",
            "@tam_status": "waiting",
        }
    ]
    out, _ = sidebar.lines(rows, ("inc", "9"), commands={}, pr_of=lambda *_: None)
    name, what, meta = out[1].split("\t", 1)[1].splitlines()
    assert "INC0016411 i ServiceNow" in name
    assert what.strip() == "claude · ~/kenneth/inc"
    assert "waiting" in meta and "terminal" not in meta


def test_harness_programs_map_commands_to_agents():
    from tam.harness import programs

    found = programs()
    assert found["claude"] == "claude" and found["cursor-agent"] == "cursor"


def test_refs_from_scrollback_keep_only_open_ones(env, monkeypatch):  # noqa: F811
    from tam import refs

    bin_dir = env / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    # Issue 2 and PR 3 are open; issue 1 is closed.
    gh.write_text('#!/bin/sh\ncase "$2" in *issues/1) echo closed;; *) echo open;; esac\n')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    pane = tmux(
        "new-session",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "printf 'see https://github.com/o/r/issues/1 and https://github.com/o/r/issues/2\\n"
        "opened https://github.com/o/r/pull/3\\n'; sleep 100",
    ).strip()
    wait_for(lambda: "pull/3" in tmux("capture-pane", "-p", "-t", pane))
    started = []
    monkeypatch.setattr(refs, "_refresh_in_background", lambda *a: started.append(a[0]))
    assert refs.lookup(pane) == [] and started == [pane]  # nothing cached yet
    refs.refresh(pane)
    found = refs.lookup(pane)
    assert [(r["kind"], r["number"]) for r in found] == [("pr", 3), ("issue", 2)]  # newest first
    assert found[0]["url"] == "https://github.com/o/r/pull/3"


def test_sidebar_foreground_commands_reads_ps():
    import os

    from tam.sidebar import foreground_commands

    tty = os.ttyname(0) if os.isatty(0) else None
    commands = foreground_commands()
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in commands.items())
    if tty:
        assert os.path.basename(tty) in commands


def test_adopt_reads_claude_code_session_files(tmp_path, monkeypatch):
    from tam import adopt

    claude = tmp_path / "claude"
    sessions = claude / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (sessions / "111.json").write_text(
        json.dumps(
            {"pid": 111, "sessionId": "sid-a", "cwd": "/w", "name": "fix login", "status": "busy"}
        )
    )
    (sessions / "222.json").write_text("{not json")
    (sessions / "333.json").write_text(json.dumps({"pid": 333, "cwd": "/w"}))  # no sessionId
    registry = adopt.claude_registry()
    assert registry == {
        111: {"session_id": "sid-a", "cwd": "/w", "name": "fix login", "status": "busy"}
    }
    # The session file wins over tam's own seen file.
    seen = tmp_path / "state" / "tam" / "seen"
    seen.mkdir(parents=True)
    (seen / "111").write_text(json.dumps({"session_id": "sid-old", "cwd": "/old"}))
    assert adopt._session_of(111) == ("sid-a", "/w")
    (seen / "444").write_text(json.dumps({"session_id": "sid-seen", "cwd": "/s"}))
    assert adopt._session_of(444) == ("sid-seen", "/s")  # no session file: as before


def test_adopt_without_claude_session_files(tmp_path, monkeypatch):
    from tam import adopt

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "missing"))
    assert adopt.claude_registry() == {}


def test_version_in_a_checkout_names_the_commit(tmp_path):
    from tam import __version__
    from tam.paths import tam_version

    assert tam_version(tmp_path) == __version__  # no .git: a release install

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "f").write_text("1")
    git("add", "f")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    commit = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert tam_version(tmp_path) == f"{__version__}+dev.{commit}"
    (tmp_path / "f").write_text("2")
    assert tam_version(tmp_path) == f"{__version__}+dev.{commit}.dirty"


def test_option_q_detaches(env):  # noqa: F811
    tmux("new-session", "-d", "-s", "q")
    subprocess.run([TAM, "tmux-init"], check=True)
    assert "detach-client" in tmux("list-keys", "-T", "root", "M-q")


def test_sidebar_title():
    from tam.sidebar import title

    assert title("✳ Fix the login form", "mac") == "Fix the login form"
    assert title("⠂ Fix the login form", "mac") == "Fix the login form"
    assert title("mac", "mac") == "" and title("mac.local", "mac") == ""
    assert title("me@mac:~/git/tam", "mac") == ""
    assert title("A plain title", "mac") == "A plain title"


def test_sidebar_branch(tmp_path):
    from tam.sidebar import branch

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feat/cards\n")
    (repo / "src").mkdir()
    assert branch(str(repo / "src")) == "feat/cards"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    gitdir = repo / ".git" / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("0123456789abcdef\n")
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")
    assert branch(str(worktree)) == "0123456"  # detached
    assert branch(str(tmp_path)) == ""


def test_sidebar_rename(env, monkeypatch):  # noqa: F811
    from tam import sidebar

    tmux("new-session", "-d", "-s", "s1", "-n", "old")
    start = sidebar.rename_start("=s1:0")
    assert "disable-search" in start and start.endswith("change-query:old")
    monkeypatch.delenv("FZF_PROMPT", raising=False)
    assert sidebar.enter("c", "=s1:0", "x", "tam") == "accept"  # not renaming: switch
    assert sidebar.escape() == "abort"
    monkeypatch.setenv("FZF_PROMPT", sidebar.RENAME_PROMPT)
    assert "reload-sync(" in sidebar.enter("c", "=s1:0", "new", "tam")
    assert tmux("list-windows", "-t", "s1", "-F", "#{window_name}").split() == ["new"]
    sidebar.enter("c", "=s1:", "api", "tam")
    assert tmux("list-sessions", "-F", "#{session_name}").split() == ["api"]
    assert "enable-search" in sidebar.escape()


def test_sidebar_plus_terminal_opens_a_terminal_where_the_agent_is(env, monkeypatch):  # noqa: F811
    from tam import sidebar

    # Window 0: a shell in a (current). Window 1: an agent in b. Window 2: a shell in c.
    tmux("new-session", "-d", "-s", "s1", "-c", str(env / "a"))
    agent = tmux("new-window", "-d", "-t", "s1:", "-c", str(env / "b"), "-P", "-F", "#{pane_id}")
    tmux("set-option", "-p", "-t", agent.strip(), "@tam_harness", "fake")
    tmux("new-window", "-d", "-t", "s1:", "-c", str(env / "c"))
    line = sidebar.session_line("s1")
    assert line.endswith(sidebar.TERMINAL_BUTTON)
    shown = "S1" + line.split("S1", 1)[1].replace(sidebar.RESET, "")  # as tmux has it

    def count():
        return len(tmux("list-windows", "-t", "s1", "-F", "x").split())

    # A click elsewhere on the session's line does nothing.
    assert sidebar.line_click("", "=s1:", "tam") == ""
    monkeypatch.setattr(sidebar, "marked", lambda pane: "=s1:")  # fzf's cursor after the click
    sidebar.mouse("%9", "S1", shown)
    assert count() == 3
    # On the button. The line as tmux reads it starts with fzf's pointer when marked.
    sidebar.mouse("%9", "terminal", f"▌ {shown}")
    windows = tmux("list-windows", "-t", "s1", "-F", "#{pane_current_path}").split()
    # Right after the agent's window, in the agent's directory.
    assert [os.path.realpath(p) for p in windows] == [
        os.path.realpath(env / d) for d in ("a", "b", "b", "c")
    ]


def test_sidebar_actions_without_a_focused_line(env):  # noqa: F811
    # fzf passes '' for {1} when no line matches the query.
    tmux("new-session", "-d", "-s", "s1")
    for flags in (["--footer", ""], ["--click", ""], ["--rename", ""]):
        out = subprocess.run(
            [TAM, "sidebar", *flags, "--client", "c"], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "", flags
    out = subprocess.run(
        [TAM, "sidebar", "--enter", "", "--name=x", "--client", "c"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "FZF_PROMPT": "> "},
    )
    assert out.stdout.strip() == "accept"


def test_sidebar_rename_to_a_name_starting_with_a_dash(env):  # noqa: F811
    tmux("new-session", "-d", "-s", "s1", "-n", "old")
    from tam.sidebar import RENAME_PROMPT

    subprocess.run(
        [TAM, "sidebar", "--enter", "=s1:0", "--name=-wip", "--client", "c"],
        check=True,
        env={**os.environ, "FZF_PROMPT": RENAME_PROMPT},
    )
    assert tmux("list-windows", "-t", "s1", "-F", "#{window_name}").split() == ["-wip"]


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


def test_sidebar_ctrl_x_closes_on_the_second_press(env, monkeypatch):  # noqa: F811
    from tam import sidebar

    tmux("new-session", "-d", "-s", "s1")
    tmux("new-window", "-t", "s1:", "-n", "victim")
    tmux("new-session", "-d", "-s", "s2")
    monkeypatch.setattr(sidebar, "_current", lambda client: ("s1", "0"))
    target = "=s1:1"

    armed = sidebar.click("c", target, "tam")
    assert armed.startswith("change-footer(") and "victim" in armed
    assert "victim" in tmux("list-windows", "-t", "s1", "-F", "#{window_name}")
    # Moving to another line disarms it.
    sidebar.footer("=s2:")
    assert sidebar.click("c", target, "tam").startswith("change-footer(")
    sidebar.footer(target)  # focus on the same line keeps it armed
    assert sidebar.click("c", target, "tam").startswith("reload-sync(")
    assert "victim" not in tmux("list-windows", "-t", "s1", "-F", "#{window_name}")

    assert "session s2 (1 window)" in sidebar.click("c", "=s2:", "tam")
    sidebar.click("c", "=s2:", "tam")
    assert tmux("list-sessions", "-F", "#{session_name}").split() == ["s1"]


def test_option_w_closes_the_marked_window_after_asking(env, monkeypatch):  # noqa: F811
    from tam import config, paths, sidebar

    monkeypatch.setattr(paths, "tam_executable", lambda: TAM)
    tmux("new-session", "-d", "-s", "w", "-n", "here")
    tmux("new-window", "-d", "-t", "w:", "-n", "marked")
    subprocess.run([TAM, "tmux-init"], check=True)
    assert "--close-marked" in tmux("list-keys", "-T", "root", "M-w")

    asked = []
    real_tmux = sidebar.tmux

    def fake_tmux(*args, **kw):
        if args[0] == "confirm-before":
            asked.append(args)
            return ""
        return real_tmux(*args, **kw)

    monkeypatch.setattr(sidebar, "tmux", fake_tmux)
    monkeypatch.setattr(sidebar, "_current", lambda client: ("w", "0"))
    # Hidden sidebar: no marked line, so the current window.
    sidebar.close_marked("")
    assert "close here? (y/n)" in asked[-1] and "--close =w:0" in asked[-1][-1]
    # With a sidebar: the line marked in it.
    config.set("sidebar", "shown")
    sidebar.ensure()
    monkeypatch.setattr(sidebar, "marked", lambda pane: "=w:1")
    monkeypatch.setattr(
        sidebar, "_by_window", lambda: {"@0": {"panes": [], "sidebars": [{"pane_id": "%1"}]}}
    )
    monkeypatch.setattr(sidebar, "_client", lambda client: ["-t", "=w:0"])
    sidebar.close_marked("")
    assert "close marked? (y/n)" in asked[-1] and "--close =w:1" in asked[-1][-1]
    # What the confirmed prompt runs.
    sidebar.close("", "=w:1")
    assert real_tmux("list-windows", "-t", "w", "-F", "#{window_name}").split() == ["here"]


def test_sidebar_marks_the_active_window():
    from tam.sidebar import ACCENT, card

    w = {"name": "w", "statuses": [], "pane": {"pane_current_command": "vim"}, "agent_pane": None}
    active = card(w, 0, {}, None, active=True).splitlines()
    assert all(ln.startswith(f"{ACCENT}┃") for ln in active)
    assert not card(w, 0, {}).startswith(ACCENT)


def test_sidebar_click_on_a_card_switches(env, monkeypatch):  # noqa: F811
    from tam import sidebar

    tmux("new-session", "-d", "-s", "s1")
    calls = []
    monkeypatch.setattr(sidebar, "_go", lambda client, target: calls.append(target))
    assert sidebar.line_click("c", "=s1:0", "tam") == ""
    assert calls == ["=s1:0"]
    monkeypatch.setenv("FZF_PROMPT", sidebar.RENAME_PROMPT)
    assert sidebar.line_click("c", "=s1:0", "tam") == ""  # renaming: only moves the cursor
    assert calls == ["=s1:0"]


def _panes(window: str) -> list[tuple[str, bool]]:
    """(width, is sidebar) for each pane in window, left to right."""
    from tam.sidebar import is_sidebar

    out = tmux("list-panes", "-t", window, "-F", "#{pane_width}\t#{pane_start_command}")
    return [(w, is_sidebar(cmd)) for w, cmd in (ln.split("\t", 1) for ln in out.splitlines())]


def test_sidebar_ensure_toggle_and_cleanup(env):  # noqa: F811
    from tam import config, sidebar

    config.set("sidebar", "shown")
    tmux("new-session", "-d", "-s", "s1", "-x", "160", "-y", "40")
    tmux("new-window", "-t", "s1:")
    sidebar.ensure()
    sidebar.ensure()  # idempotent
    for window in ("=s1:0", "=s1:1"):
        assert _panes(window) == [(str(sidebar.WIDTH), True), (_panes(window)[1][0], False)]

    sidebar.toggle()
    assert config.get("sidebar") == "hidden"
    assert tmux("show-options", "-gv", sidebar.SHOWN_OPTION).strip() == "0"
    assert [s for _, s in _panes("=s1:0")] == [False]

    sidebar.toggle()
    assert [s for _, s in _panes("=s1:1")] == [True, False]
    # A window with only its sidebar left is closed.
    tmux("kill-pane", "-t", "=s1:1.1")
    sidebar.ensure()
    assert tmux("list-windows", "-t", "s1", "-F", "#{window_index}").split() == ["0"]


def test_tmux_init_restarts_sidebars_started_by_an_older_tam(env, monkeypatch):  # noqa: F811
    from tam import config, paths, sidebar

    monkeypatch.setattr(paths, "tam_executable", lambda: TAM)
    config.set("sidebar", "shown")
    tmux("new-session", "-d", "-s", "s1", "-x", "160", "-y", "40")
    subprocess.run([TAM, "tmux-init"], check=True)

    def sidebar_pid():
        out = tmux("list-panes", "-t", "=s1:0", "-F", "#{pane_pid} #{pane_start_command}")
        return next(ln.split()[0] for ln in out.splitlines() if sidebar.PANE_COMMAND in ln)

    before = sidebar_pid()
    subprocess.run([TAM, "tmux-init"], check=True)
    assert sidebar_pid() != before
    assert _panes("=s1:0")[0] == (str(sidebar.WIDTH), True)


def test_sidebar_tick_reloads_only_after_a_change(env, monkeypatch):  # noqa: F811
    from tam import sidebar

    tmux("new-session", "-d", "-s", "s1", "-n", "one")
    pane = tmux("list-panes", "-t", "=s1:0", "-F", "#{pane_id}").strip()
    monkeypatch.setenv("TMUX_PANE", pane)
    monkeypatch.setenv("TAM_SIDEBAR_PANE", "1")
    monkeypatch.setattr(sidebar, "foreground_commands", lambda: {})
    first = sidebar.tick()
    assert first.startswith("reload-sync(cat ") and "transform-header(cat " in first
    list_file, _ = sidebar._tick_files(pane)
    window = tmux("display-message", "-p", "-t", "=s1:0", "#{window_id}").strip()
    assert f"\0{window}\t" in list_file.read_text()  # cards name windows by id
    assert sidebar.tick() == ""  # nothing changed: no reload, nothing blocks fzf
    tmux("rename-window", "-t", "=s1:0", "renamed")
    assert sidebar.tick().startswith("reload-sync(")
    assert "renamed" in list_file.read_text()
    args = " ".join(sidebar.fzf_args("tam", 0, ""))
    assert "bg-transform:" in args and "focus:bg-transform-footer:" in args


def test_sidebar_marks_its_own_window_after_a_switch(env, monkeypatch):  # noqa: F811
    from tam import config, paths

    monkeypatch.setattr(paths, "tam_executable", lambda: TAM)
    config.set("sidebar", "shown")
    tmux("new-session", "-d", "-s", "s1", "-x", "160", "-y", "40", "-n", "one")
    tmux("new-window", "-t", "s1:", "-n", "two")
    subprocess.run([TAM, "tmux-init"], check=True)
    side = tmux("list-panes", "-t", "=s1:1", "-F", "#{pane_id}").split()[0]

    def marked() -> str:
        shot = tmux("capture-pane", "-p", "-t", side).splitlines()
        return " ".join(ln for ln in shot if ln.startswith("▌"))

    wait_for(lambda: "two" in marked())
    tmux("send-keys", "-t", side, "Up")  # the cursor moves to window one's card
    wait_for(lambda: "one" in marked())
    tmux("select-window", "-t", "=s1:0")
    tmux("select-window", "-t", "=s1:1")
    wait_for(lambda: "two" in marked(), show=side)


def test_sidebar_keeps_its_width_when_the_window_grows(env):  # noqa: F811
    from tam import config, sidebar

    config.set("sidebar", "shown")
    # Like tam adopt: created with no client, so tmux uses its default size.
    tmux("new-session", "-d", "-s", "s1", "-x", "80", "-y", "24")
    subprocess.run([TAM, "tmux-init"], check=True)
    assert _panes("=s1:0")[0] == (str(sidebar.WIDTH), True)
    tmux("set-option", "-w", "-t", "=s1:0", "window-size", "manual")
    tmux("resize-window", "-t", "=s1:0", "-x", "250", "-y", "50")
    wait_for(lambda: _panes("=s1:0")[0] == (str(sidebar.WIDTH), True))


def test_sidebar_panes_are_not_listed_and_are_saved_as_sidebars(env):  # noqa: F811
    from tam import config, sidebar, state

    config.set("sidebar", "shown")
    tmux("new-session", "-d", "-s", "s1", "-x", "160", "-y", "40")
    sidebar.ensure()
    out, _ = sidebar.lines(sidebar.pane_rows(), ("s1", "0"))
    window = tmux("display-message", "-p", "-t", "=s1:0", "#{window_id}").strip()
    assert [item.split("\t")[0] for item in out] == ["=s1:", window]
    assert "terminal" in out[1]
    panes = state.capture()["sessions"][0]["windows"][0]["panes"]
    assert [p.get("sidebar", False) for p in panes] == [True, False]
    assert state._pane_command(panes[0], "tam", None) == ["tam", "sidebar", "--pane"]


def test_sidebar_pane_marks_itself_and_keeps_the_window_name(env, monkeypatch):  # noqa: F811
    from tam import config, paths, sidebar

    # Under pytest, sys.argv[0] is pytest; the pane has to run the real tam.
    monkeypatch.setattr(paths, "tam_executable", lambda: TAM)
    config.set("sidebar", "shown")
    tmux("new-session", "-d", "-s", "s1", "-x", "160", "-y", "40", "-n", "work")
    sidebar.ensure()
    sidebar_pane = tmux("list-panes", "-t", "=s1:0", "-F", "#{pane_id}").split()[0]
    wait_for(lambda: tmux("show-options", "-pqv", "-t", sidebar_pane, "@tam_sidebar").strip())

    def fmt():
        return tmux("show-options", "-wqv", "-t", "=s1:0", "automatic-rename-format")

    wait_for(lambda: fmt().startswith("#{?#{@tam_sidebar},#{window_name},"))


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
