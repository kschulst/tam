# Changelog

## 0.3.0 (2026-11-19)

### Features

- ⌥ / runs a tam command typed in tmux's prompt, such as mcp install (66a1ddc)
- tam mcp lets agents list, open, close, rename and type into tam's windows (24d2b73)
- sidebar cards list open issues and PRs as underlined "Issue: #n  PR: #n" links, also those named in the agent's title (429b06c)
- sidebar cards name the agent's topic and link its open issues and PRs; one click switches window (e1226f1)
- the sidebar is a pane in every window; ⌥ S shows or hides it (a66d4c7)
- tam adopt shows Claude's own name and status for running sessions (7e0f4aa)
- ⌥ q quits tam; tmux and the agents keep running (9615059)
- tam --version shows the commit when run from a git checkout (dec9677)
- tam --version shows the installed version (5aeba92)
- the terminal title shows tam:<session> (c4f7f93)

### Fixes

- ⌥ / also works where / needs Shift, such as on a Norwegian keyboard (721e12d)
- the cheat sheet says "new agent" and names the default agent where it is set (6f727a8)
- help text for tam adopt and tam setup speaks of agents, not only Claude (26cd95e)
- tam setup and the cheat sheet tell which Option key the ⌥ shortcuts need in iTerm (073f8e7)

## 0.2.0 (2026-11-14)

### Features

- ⌥ t / Ctrl+b → t / `tam term`: open a plain terminal (no agent) with the directory picker
- In the directory picker (⌥ a, ⌥ t): Enter opens a new window, Ctrl+v splits right, Ctrl+s splits below

## 0.1.0 (2026-11-13)

First release: agent sessions in tmux that survive a reboot, with the sidebar, directory picker, cheat sheet, `tam adopt`, `tam move` and eleven built-in agents.
