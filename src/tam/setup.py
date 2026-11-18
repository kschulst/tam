"""`tam setup` and `tam tmux-init`."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from tam import paths, sidebar
from tam.state import RESTORED_FLAG
from tam.tmux import server_running, tmux

TMUX_CONF_LINE = 'run-shell "tam tmux-init"'
HOOK_INDEX = 42

# Events after which the server is saved. session-window-changed and
# window-pane-changed cover switching, so the cwd of shell panes stays fresh.
SNAPSHOT_HOOKS = [
    "after-new-window",
    "after-split-window",
    "after-kill-pane",
    "pane-exited",
    "window-renamed",
    "window-layout-changed",
    "session-window-changed",
    "window-pane-changed",
    "session-created",
    "session-renamed",
    "session-closed",
    "client-session-changed",
    "window-linked",
    "window-unlinked",
]

# During shutdown, panes close before the server exits. Saving after these
# events is delayed, and does nothing if the server is gone by then.
CLOSE_HOOKS = {"pane-exited", "after-kill-pane", "session-closed"}
CLOSE_DELAY = 2

# Events after which `tam sidebar --ensure` adds, removes or resizes sidebar panes.
# After a close it waits like the snapshot does: closing a window that has only
# its sidebar left would otherwise save that window as gone during shutdown.
SIDEBAR_HOOK_INDEX = 43
# window-resized covers a window that gets its size from the first client to show
# it, e.g. one created by tam adopt with no client attached: tmux scales the
# sidebar with the window.
SIDEBAR_HOOKS = ["after-new-window", "session-created", "client-resized", "window-resized"]
SIDEBAR_CLOSE_HOOKS = ["pane-exited", "after-kill-pane"]

# Settings Claude Code needs for keys, notifications and clipboard.
CLAUDE_SETTINGS = [
    ("extended-keys", "on"),
    ("allow-passthrough", "on"),
    ("set-clipboard", "on"),
    ("focus-events", "on"),
]

MIN_TMUX = (3, 3)


def key_bindings(exe: str) -> list[list[str]]:
    """[table, key, note, *tmux command] for each binding tam adds.

    Root-table Option keys (M-a, M-s) work without the prefix when the terminal
    sends Option as Meta (iTerm2: Profiles > Keys > Right Option key: Esc+).
    """
    here = ["run-shell", "-c", "#{pane_current_path}"]
    new_window = [
        "run-shell",
        "-b",
        f"{exe} new --popup-client '#{{client_name}}' --target '#{{pane_id}}'"
        " --dir #{q:pane_current_path}",
    ]
    sidebar = ["run-shell", "-b", f"{exe} sidebar --client '#{{client_name}}'"]
    toggle_sidebar = ["run-shell", "-b", f"{exe} sidebar --toggle"]
    new_terminal = [
        "run-shell",
        "-b",
        f"{exe} term --popup-client '#{{client_name}}' --target '#{{pane_id}}'"
        " --dir #{q:pane_current_path}",
    ]
    return [
        ["prefix", "a", "tam: new agent in a new window (pick directory)", *new_window],
        ["root", "M-a", "tam: new agent in a new window (pick directory)", *new_window],
        [
            "prefix",
            "A",
            "tam: new agent in a split",
            *here,
            f"{exe} new --split h --target '#{{pane_id}}'",
        ],
        ["prefix", "t", "tam: new terminal (pick directory)", *new_terminal],
        ["root", "M-t", "tam: new terminal (pick directory)", *new_terminal],
        ["prefix", "h", "tam: cheat sheet", *popup_command(exe)],
        ["root", "M-h", "tam: cheat sheet", *popup_command(exe)],
        ["root", "M-q", "tam: quit (detach; agents keep running)", "detach-client"],
        ["prefix", "s", "tam: sessions and agents", *sidebar],
        ["root", "M-s", "tam: sessions and agents", *sidebar],
        ["prefix", "S", "tam: show or hide the sidebar", *toggle_sidebar],
        ["root", "M-S", "tam: show or hide the sidebar", *toggle_sidebar],
        [
            "prefix",
            ".",
            "tam: move window to another session",
            "run-shell",
            "-b",
            f"{exe} move --client '#{{client_name}}' --window '#{{window_id}}'",
        ],
    ]


_ITERM_ESC_PLUS = 2


def iterm_profile(session: str | None = None) -> str | None:
    """The iTerm2 profile of the user's terminal, or None outside iTerm2 or when unknown.

    Inside tmux the process environment is the server's, from whichever terminal
    started it. The session environment has the profile of the client that last
    attached, because tmux-init adds ITERM_PROFILE to update-environment.
    """
    import os

    if not session and not os.environ.get("TMUX"):
        return os.environ.get("ITERM_PROFILE")
    target = ["-t", session] if session else []
    out = tmux("show-environment", *target, "ITERM_PROFILE", check=False).strip()
    name, sep, value = out.partition("=")
    return value if sep and name == "ITERM_PROFILE" else None


def iterm_option_keys(
    plist: Path | None = None, profile: str | None = None, session: str | None = None
) -> tuple[str, bool, bool] | None:
    """(profile, left sends Esc+, right sends Esc+) for iTerm2, or None when unknown.

    Without plist, the profile is the user's (see iterm_profile). With plist and
    no profile, it is iTerm's default profile.

    Only Esc+ makes tmux see M-a. A left Option key on Normal still types the
    characters a Norwegian or German layout puts there (@, [, ], {, }, |).
    """
    import plistlib

    if plist is None:
        profile = iterm_profile(session)
        if profile is None:
            return None
        plist = Path.home() / "Library/Preferences/com.googlecode.iterm2.plist"
    try:
        data = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return None
    profiles = data.get("New Bookmarks", [])
    # A profile that is not in the plist (a dynamic profile, for one) is unknown.
    if profile is None:
        match = [p for p in profiles if p.get("Guid") == data.get("Default Bookmark Guid")]
    else:
        match = [p for p in profiles if p.get("Name") == profile]
    if not match:
        return None
    p = match[0]
    return (
        p.get("Name", "?"),
        p.get("Option Key Sends") == _ITERM_ESC_PLUS,
        p.get("Right Option Key Sends") == _ITERM_ESC_PLUS,
    )


def option_key_report() -> tuple[bool, str] | None:
    """(ok, message) about the Option shortcuts in iTerm2, or None outside iTerm2."""
    keys = iterm_option_keys()
    if keys is None:
        return None
    profile, left, right = keys
    if left:
        return True, f"iTerm profile {profile}: ⌥ shortcuts work with the left Option key"
    if right:
        return True, f"iTerm profile {profile}: ⌥ shortcuts work with the right Option key"
    return False, (
        f"iTerm profile {profile}: no Option key sends Esc+, so ⌥ a, ⌥ s and ⌥ h do nothing."
        " Set Settings > Profiles > Keys > Right Option key to Esc+"
        " (the left one keeps typing @ [ ] { })"
    )


def popup_command(exe: str) -> list[str]:
    """tmux command that opens the cheat sheet popup, sized for the client."""
    return ["run-shell", "-b", f"{exe} help --client '#{{client_name}}'"]


# tmux's own bindings for keys tam uses. tam replaces these, but not a user's binding.
TMUX_DEFAULT_BINDINGS = {
    "s": "choose-tree -Zs",
    ".": 'command-prompt { move-window -t "%%" }',
    "t": "clock-mode",
}


def tam_key(key: str, table: str = "prefix") -> bool | None:
    """True if tam owns the key, False if the user bound it, None if free.

    A key with tmux's default binding counts as free.
    """
    if "tam:" in tmux("list-keys", "-N", "-T", table, key, check=False):
        return True
    bound = tmux("list-keys", "-T", table, key, check=False).strip()
    m = re.search(rf"-T {table}\s+\S+\s+(.*)$", bound)
    command = m.group(1).strip() if m else bound
    if not command or (table == "prefix" and command == TMUX_DEFAULT_BINDINGS.get(key)):
        return None
    # Bindings from tam versions that did not set a note.
    return "tam" in command and any(c in command for c in (" new ", " term ", " help --"))


def _bind(table: str, key: str, note: str, command: list[str]) -> None:
    """Bind a key unless the user already bound it to something else.

    tam's line in tmux.conf runs after the user's own bindings.
    """
    if tam_key(key, table) is False:
        return
    tmux("bind-key", "-N", note, "-T", table, key, *command)


def tmux_init() -> int:
    """Apply tam's hooks, bindings and settings to the running tmux server."""
    if not server_running():
        print("tam: no tmux server running; tmux.conf applies this on the next start")
        return 0
    exe = shlex.quote(paths.tam_executable())

    for option, value in CLAUDE_SETTINGS:
        tmux("set-option", "-g", option, value)
    # The terminal's title (iTerm tab and window) shows the session: tam:<session>.
    tmux("set-option", "-g", "set-titles", "on")
    tmux("set-option", "-g", "set-titles-string", "tam:#S")
    # Each attaching client sets the session's ITERM_PROFILE (see iterm_profile).
    update = tmux("show-options", "-gv", "update-environment", check=False).split()
    if "ITERM_PROFILE" not in update:
        tmux("set-option", "-ga", "update-environment", "ITERM_PROFILE")
    features = tmux("show-options", "-s", "terminal-features", check=False)
    if "xterm*:extkeys" not in features:
        tmux("set-option", "-as", "terminal-features", "xterm*:extkeys")

    for hook in SNAPSHOT_HOOKS:
        delay = f"sleep {CLOSE_DELAY}; " if hook in CLOSE_HOOKS else ""
        tmux("set-hook", "-g", f"{hook}[{HOOK_INDEX}]", f"run-shell -b '{delay}{exe} snapshot'")

    # Python runs only when the sidebar is shown and the server is restored.
    when = f"#{{&&:#{{{sidebar.SHOWN_OPTION}}},#{{{RESTORED_FLAG}}}}}"
    for hook in SIDEBAR_HOOKS + SIDEBAR_CLOSE_HOOKS:
        delay = f"sleep {CLOSE_DELAY}; " if hook in SIDEBAR_CLOSE_HOOKS else ""
        tmux(
            "set-hook",
            "-g",
            f"{hook}[{SIDEBAR_HOOK_INDEX}]",
            f"if-shell -F '{when}' \"run-shell -b '{delay}{exe} sidebar --ensure'\"",
        )

    for table, key, note, *command in key_bindings(exe):
        _bind(table, key, note, command)

    # Without saved state there is nothing to restore, so saving can start now.
    if not paths.state_file().exists():
        tmux("set-option", "-g", RESTORED_FLAG, "1")
    sidebar.sync_option()
    if tmux("show-options", "-gqv", RESTORED_FLAG, check=False).strip() == "1":
        sidebar.ensure()
    return 0


def _tmux_conf() -> Path:
    xdg = paths._xdg("XDG_CONFIG_HOME", ".config") / "tmux" / "tmux.conf"
    home = Path.home() / ".tmux.conf"
    if xdg.exists() and not home.exists():
        return xdg
    return home


def _tmux_version() -> tuple[int, int] | None:
    out = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _status_command(status: str) -> str:
    # Plain shell, no Python: these hooks run on every prompt and tool call.
    return (
        f'[ -n "$TMUX_PANE" ] && tmux set -p -t "$TMUX_PANE" @tam_status {status}'
        " 2>/dev/null || true"
    )


CLAUDE_HOOKS = {
    "SessionStart": "command -v tam >/dev/null 2>&1 && tam hook claude || true",
    "UserPromptSubmit": _status_command("working"),
    "PostToolUse": _status_command("working"),
    "Stop": _status_command("waiting"),
    "Notification": _status_command("waiting"),
}


def _is_tam_command(command: str) -> bool:
    return "tam hook claude" in command or "@tam_status" in command


def install_claude_hook(settings_path: Path) -> bool:
    """Add or update tam's Claude Code hooks. Returns True if the file changed."""
    data = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    hooks = data.setdefault("hooks", {})
    changed = False
    for event, command in CLAUDE_HOOKS.items():
        groups = hooks.setdefault(event, [])
        existing = [
            h for g in groups for h in g.get("hooks", []) if _is_tam_command(h.get("command", ""))
        ]
        if existing and all(h["command"] == command for h in existing):
            continue
        for g in groups:
            g["hooks"] = [
                h for h in g.get("hooks", []) if not _is_tam_command(h.get("command", ""))
            ]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append({"hooks": [{"type": "command", "command": command}]})
        changed = True
    if not changed:
        return False
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        shutil.copy(settings_path, settings_path.with_suffix(".json.tam-backup"))
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return True


# Tools tam cannot work without. The full list, with optional tools, is the Brewfile.
REQUIRED_TOOLS = {"tmux", "fzf"}
_BREW_LINE = re.compile(r'^brew\s+"([^"]+)"\s*(?:#\s*(.*))?$')


def brewfile() -> Path:
    from importlib import resources

    return Path(str(resources.files("tam").joinpath("Brewfile")))


def brewfile_tools(path: Path) -> list[tuple[str, str]]:
    """(formula, description) for each `brew` line."""
    tools = []
    for line in path.read_text().splitlines():
        m = _BREW_LINE.match(line.strip())
        if m:
            tools.append((m.group(1), m.group(2) or ""))
    return tools


def check_tools(install: bool | None = None) -> bool:
    """Print tool status and offer `brew bundle` for what is missing.

    Returns False if a required tool is still missing.
    """
    path = brewfile()
    tools = brewfile_tools(path)
    missing = [name for name, _ in tools if not shutil.which(name)]
    for name, purpose in tools:
        mark = "✓" if shutil.which(name) else ("✗" if name in REQUIRED_TOOLS else "·")
        print(f"{mark} {name:<8} {purpose}")
    cmd = ["brew", "bundle", "install", "--no-upgrade", f"--file={path}"]
    if missing and shutil.which("brew"):
        if install is None:
            install = sys.stdin.isatty() and input(
                f"Install {', '.join(missing)} with brew bundle? [Y/n] "
            ).strip().lower() in ("", "y", "yes")
        if install:
            subprocess.run(cmd, check=False)
        else:
            print(f"  Install later with: {' '.join(cmd)}")
    elif missing:
        print("  Homebrew not found: install the tools above with your package manager.")
    return all(shutil.which(name) for name in REQUIRED_TOOLS)


def choose_default_agent(interactive: bool) -> None:
    """Report installed agents and set a default the first time."""
    from tam import config
    from tam.harness import installed_names, suggested_default

    names = installed_names()
    if not names:
        print("✗ no agents found on PATH (claude, codex, ...). See tam agent.")
        return
    current = config.get("default_agent")
    if current in names:
        print(f"✓ agents: {', '.join(names)} (default: {current}, change with tam agent <name>)")
        return
    suggestion = suggested_default(names)
    choice = suggestion
    if len(names) > 1 and interactive:
        answer = input(f"Agents found: {', '.join(names)}\nDefault agent for ⌥ a [{suggestion}]: ")
        if answer.strip() in names:
            choice = answer.strip()
    config.set("default_agent", choice)
    print(f"✓ default agent: {choice} (change with tam agent <name>)")


def setup() -> int:
    ok = check_tools()
    choose_default_agent(sys.stdin.isatty())
    version = _tmux_version()
    if version is None:
        return 1
    if version < MIN_TMUX:
        print(
            f"✗ tmux {version[0]}.{version[1]} is too old, tam needs {MIN_TMUX[0]}.{MIN_TMUX[1]}+"
        )
        ok = False

    conf = _tmux_conf()
    text = conf.read_text() if conf.exists() else ""
    if "tam tmux-init" in text:
        print(f"✓ {conf} loads tam")
    else:
        with conf.open("a") as f:
            f.write(("\n" if text and not text.endswith("\n") else "") + TMUX_CONF_LINE + "\n")
        print(f"✓ added `{TMUX_CONF_LINE}` to {conf}")

    settings = paths.claude_dir() / "settings.json"
    if install_claude_hook(settings):
        print(f"✓ added tam's hooks to {settings}")
    else:
        print(f"✓ {settings} has tam's hooks")

    option = option_key_report()
    if option:
        print(f"{'✓' if option[0] else '✗'} {option[1]}")

    if not shutil.which("tam"):
        print("✗ tam is not on PATH. tmux and the Claude hook call `tam` by name.")
        ok = False

    if server_running():
        tmux_init()
        print("✓ applied to the running tmux server")
    return 0 if ok else 1
