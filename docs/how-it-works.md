# How it works

## Saving

`tam setup` adds `run-shell "tam tmux-init"` to `~/.tmux.conf`. `tam tmux-init` registers tmux hooks (new window, split, close, rename, layout change, switching, moving windows between sessions). Each runs `tam snapshot`, which writes the whole tmux server to `~/.local/state/tam/state.json`: sessions, windows with their layouts, and for each pane its directory and agent. The previous version is kept as `state.prev.json`.

An agent pane runs `tam run`, which starts the agent and stores on the pane:

| Pane option | Holds |
|---|---|
| `@tam_harness` | The agent |
| `@tam_session_id` | The agent's session id, set by its hook |
| `@tam_status` | `running`, `working`, `waiting`, `exited` or `stopped` |

When the agent exits, `tam run` keeps the pane and waits: Enter resumes, `q` closes.

## Restoring

After a reboot no tmux server runs. `tam` starts one and recreates every saved session that does not exist, with its windows, splits and directories, then attaches. Shell panes get a shell. Agent panes run `tam run --wait` and resume only when you press Enter in them.

Until `tam` has restored, saving is off. A tmux started some other way after a reboot therefore cannot overwrite the saved state.

## Shutdown

macOS sends SIGTERM to every process at shutdown, not only to tmux. `tam run` ignores SIGTERM so agent panes stay open until tmux exits, and saving after a pane closes waits two seconds and does nothing if tmux is gone by then. Without this, the panes closing first would be saved as removed.

## Claude Code

`tam setup` adds these hooks to `~/.claude/settings.json`:

| Event | Effect |
|---|---|
| `SessionStart` | Records the session id on the pane (and for `tam adopt`) |
| `UserPromptSubmit`, `PostToolUse` | Status `working` |
| `Stop`, `Notification` | Status `waiting` |

The status hooks are plain shell commands that set a tmux option, because `PostToolUse` runs on every tool call. Outside tmux they do nothing.

## Adopting a session

`tam adopt` lists Claude processes outside tmux, and recent conversations. The chosen one is stopped and resumed in a tam window.

Typed inside Claude Code as `! tam adopt`, it moves that session: it reads `CLAUDE_CODE_SESSION_ID`, starts a helper outside Claude's process group, prints a message, and exits. The helper stops Claude, opens the window with `claude --resume`, and the next `tam` attaches to it.

## Files

| Path | Contents |
|---|---|
| `~/.local/state/tam/state.json` | Saved tmux layout and agents |
| `~/.local/state/tam/seen/` | Claude sessions seen by the hook, for `tam adopt` |
| `~/.config/tam/config.toml` | `default_agent` |
| `~/.config/tam/harnesses.toml` | Your own agents and overrides ([Agents](agents.md)) |
