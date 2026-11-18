# How it works

```mermaid
flowchart LR
    A["⌥ a"] --> P["tmux pane<br/>tam run → agent"]
    P -- "hook: session id" --> O["pane options"]
    T["tmux hooks"] --> S["tam snapshot<br/>~/.local/state/tam/state.json"]
    O --> S
    S -- "reboot, then tam" --> R["sessions, windows,<br/>splits restored"]
    R -- "Enter" --> X["agent resumes<br/>the same session"]
```

## Why not tmux-resurrect

tmux keeps sessions, windows and running programs in the memory of its server process. It writes nothing to disk, so a reboot ends all of it. [tmux-resurrect](https://github.com/tmux-plugins/tmux-resurrect) and [tmux-continuum](https://github.com/tmux-plugins/tmux-continuum) fill that gap for layouts, and for the layout alone they would be enough. For agents they are not:

- **The conversation.** resurrect restarts a program with the command line it was started with. A pane started with `claude` gets `claude` again: an empty conversation. The session id is not on the command line, and it changes during a session (`/clear`, `/resume`). tam gets the id from the agent's hook and stores it on the pane.
- **Timing.** continuum saves every 15 minutes, so a window opened since the last save is lost. tam saves on every change.
- **Start-up.** resurrect starts every saved program at once. tam's agent panes wait for Enter.
- **Shutdown.** Panes that close before tmux exits could be saved as removed. tam ignores those closes (see [Shutdown](#shutdown)).

Saving and restoring the layout is about 250 lines in tam ([state.py](../src/tam/state.py)). The agent part is what no existing tool covers.

## Saving

`tam setup` adds `run-shell "tam tmux-init"` to `~/.tmux.conf`. `tam tmux-init` registers tmux hooks (new window, split, close, rename, layout change, switching, moving windows between sessions). Each runs `tam snapshot`, which writes the whole tmux server to `~/.local/state/tam/state.json`: sessions, windows with their layouts, and for each pane its directory and agent. The previous version is kept as `state.prev.json`.

An agent pane runs `tam run`, which starts the agent and stores on the pane:

| Pane option | Holds |
|---|---|
| `@tam_harness` | The agent |
| `@tam_session_id` | The agent's session id, set by its hook |
| `@tam_status` | `running`, `working`, `waiting`, `exited` or `stopped` |

When the agent exits, `tam run` keeps the pane and waits: Enter resumes, `q` closes.

## Sidebar

The sidebar is a pane running `tam sidebar --pane` (fzf) on the left of every window, 44 columns wide. tam recognises it by that start command. It is saved and restored like any pane, so the saved layout keeps its column.

Showing or hiding is one setting for all windows: `sidebar = "shown"` or `"hidden"` in `config.toml`, copied to the tmux option `@tam_sidebar_shown`. After a new window, a new session, a resize or a closed pane, a tmux hook runs `tam sidebar --ensure`: it adds the sidebar to windows without one, sets its width back to 44 columns, removes it everywhere when hidden, and closes a window with only its sidebar left. The hooks check the option in tmux first, so with the sidebar hidden no Python runs. After a closed pane, `--ensure` waits two seconds, for the same reason as saving does (see [Shutdown](#shutdown)).

Every 2 seconds a visible sidebar builds its list in the background (fzf's `bg-transform`) and reloads only when the list changed, from a file in `~/.local/state/tam/sidebar/`. fzf does not block clicks and keys while the list is built. A sidebar in a window no client shows checks that with one tmux call and does nothing more. The command a terminal pane runs comes from one `ps` call per list, because tmux gives only the program's name. `tam tmux-init` restarts every sidebar (`respawn-pane`), because fzf reads its bindings only when it starts.

Each sidebar's fzf listens on a unix socket next to tmux's own socket, in a directory only the user can open. When a window becomes the current one, a tmux hook runs `tam sidebar --refocus`, which sends `pos(n)` there so the sidebar marks the window's own card. `⌥ w` reads the marked line over the same socket (`GET`).

fzf reports which line a click hits, not where on it. A click in a sidebar therefore goes to tmux's `MouseDown1Pane` binding too: it passes the click on to fzf and runs `tam sidebar --mouse` with the word under the mouse (`#{mouse_word}`), so a click on `+ terminal` opens a terminal and a click elsewhere on the line does not. Other panes keep tmux's default binding, and a binding the user set is not replaced.

An agent's card is named after the pane title the agent sets, unless the window was renamed with `Ctrl+r` (window option `@tam_named`, saved in `state.json`). A pane counts as an agent when tam started it or when a known agent's program runs in it, so a `claude` started by hand gets an agent's card as well. The links line has the branch's open PR (`gh pr list`) and the open issues and PRs among the GitHub links in the last 3000 lines of the agent pane (`gh api …/issues/<n>`). Both run in a background process and are cached in `~/.local/state/tam/pr/` and `~/.local/state/tam/refs/`, so a reload never waits for GitHub.

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

The session id of a running Claude comes, in this order, from Claude Code's own session file (`~/.claude/sessions/<pid>.json`, written by recent versions), from tam's `seen` file, or from the newest transcript in the process's directory. The session file also gives the name and status the list shows. Its format is not documented, so tam skips files it cannot read.

Typed inside Claude Code as `! tam adopt`, it moves that session: it reads `CLAUDE_CODE_SESSION_ID`, starts a helper outside Claude's process group, prints a message, and exits. The helper stops Claude, opens the window with `claude --resume`, and the next `tam` attaches to it.

## Files

| Path | Contents |
|---|---|
| `~/.local/state/tam/state.json` | Saved tmux layout and agents |
| `~/.local/state/tam/seen/` | Claude sessions seen by the hook, for `tam adopt` |
| `~/.config/tam/config.toml` | `default_agent`, `sidebar` |
| `~/.config/tam/harnesses.toml` | Your own agents and overrides ([Agents](agents.md)) |
