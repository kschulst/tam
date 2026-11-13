"""`tam setup` and `tam tmux-init`."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from tam import paths
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
]

# During shutdown, panes close before the server exits. Saving after these
# events is delayed, and does nothing if the server is gone by then.
CLOSE_HOOKS = {"pane-exited", "after-kill-pane", "session-closed"}
CLOSE_DELAY = 2

# Settings Claude Code needs for keys, notifications and clipboard.
CLAUDE_SETTINGS = [
    ("extended-keys", "on"),
    ("allow-passthrough", "on"),
    ("set-clipboard", "on"),
    ("focus-events", "on"),
]

MIN_TMUX = (3, 3)


def key_bindings(exe: str) -> list[list[str]]:
    """[key, note, *tmux command] for each prefix binding tam adds."""
    here = ["run-shell", "-c", "#{pane_current_path}"]
    return [
        [
            "a",
            "tam: new Claude in a new window (pick directory)",
            "run-shell",
            "-b",
            f"{exe} new claude --popup-client '#{{client_name}}' --target '#{{pane_id}}'"
            " --dir #{q:pane_current_path}",
        ],
        [
            "A",
            "tam: new Claude in a split",
            *here,
            f"{exe} new claude --split h --target '#{{pane_id}}'",
        ],
        ["h", "tam: cheat sheet", *popup_command(exe)],
    ]


def popup_command(exe: str) -> list[str]:
    """tmux command that opens the cheat sheet popup, sized for the client."""
    return ["run-shell", "-b", f"{exe} help --client '#{{client_name}}'"]


def tam_key(key: str) -> bool | None:
    """True if tam owns the prefix key, False if the user bound it, None if unbound."""
    out = tmux("list-keys", "-N", "-T", "prefix", key, check=False).strip()
    if out:
        return "tam:" in out
    bound = tmux("list-keys", "-T", "prefix", key, check=False).strip()
    if not bound:
        return None
    # Bindings from tam versions that did not set a note.
    return "tam" in bound and any(c in bound for c in (" new claude", " help --"))


def _bind(key: str, note: str, command: list[str]) -> None:
    """Bind a prefix key unless the user already bound it to something else.

    tam's line in tmux.conf runs after the user's own bindings.
    """
    if tam_key(key) is False:
        return
    tmux("bind-key", "-N", note, key, *command)


def tmux_init() -> int:
    """Apply tam's hooks, bindings and settings to the running tmux server."""
    if not server_running():
        print("tam: no tmux server running; tmux.conf applies this on the next start")
        return 0
    exe = shlex.quote(paths.tam_executable())

    for option, value in CLAUDE_SETTINGS:
        tmux("set-option", "-g", option, value)
    features = tmux("show-options", "-s", "terminal-features", check=False)
    if "xterm*:extkeys" not in features:
        tmux("set-option", "-as", "terminal-features", "xterm*:extkeys")

    for hook in SNAPSHOT_HOOKS:
        delay = f"sleep {CLOSE_DELAY}; " if hook in CLOSE_HOOKS else ""
        tmux("set-hook", "-g", f"{hook}[{HOOK_INDEX}]", f"run-shell -b '{delay}{exe} snapshot'")

    for key, note, *command in key_bindings(exe):
        _bind(key, note, command)

    # Without saved state there is nothing to restore, so saving can start now.
    if not paths.state_file().exists():
        tmux("set-option", "-g", RESTORED_FLAG, "1")
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


def install_claude_hook(settings_path: Path) -> bool:
    """Add the SessionStart hook. Returns True if the file changed."""
    command = "command -v tam >/dev/null 2>&1 && tam hook claude || true"
    data = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    groups = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    for group in groups:
        for h in group.get("hooks", []):
            if "tam hook claude" in h.get("command", ""):
                return False
    groups.append({"hooks": [{"type": "command", "command": command}]})
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


def setup() -> int:
    ok = check_tools()
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
        print(f"✓ added SessionStart hook to {settings}")
    else:
        print(f"✓ {settings} has the tam hook")

    if not shutil.which("tam"):
        print("✗ tam is not on PATH. tmux and the Claude hook call `tam` by name.")
        ok = False

    if server_running():
        tmux_init()
        print("✓ applied to the running tmux server")
    return 0 if ok else 1
